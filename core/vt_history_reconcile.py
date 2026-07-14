#!/usr/bin/env python3
"""
Vibe-Trading History Reconciliation — reconcilia drift DB ↔ MT5 usando
`mt5.history_deals_get` como ground truth.

PROBLEMA REAL (Bruno 30/06/2026 16:55):
- Extrato MT5 (22 deals) vs DB SQLite (15 trades) — 7 SELLs perdidos
- Causa raiz: `log_exit()` em vt_trade_log.py não roda consistentemente
  quando posições fecham (DB lock + restart + race entre manage_position
  e log_exit). Trades ficam com `exit_time IS NULL` no DB.

SOLUÇÃO — 3 defesas (chamadas a partir de vt_autotrader.py):

1. **Startup reconciliation** — chamada em `run_daemon()` ANTES de
   `recover_open_positions()`. Para cada trade com `exit_time IS NULL`
   no DB, busca deal correspondente no MT5 history e atualiza.

2. **Periodic reconciliation** — chamada a cada 10 iterações do loop
   (~5 min com check_interval=30s). Mesma lógica. Detecta drift que
   ocorre DURANTE o dia (lock, restart, exception em log_exit).

3. **Best-effort, idempotente** — usa `close_source='HISTORY_RECONCILE_<ts>'`
   para audit trail. Pula trades já reconciliados (`close_source LIKE
   'HISTORY_RECONCILE_%'` ou já tem exit_time não-NULL).

MT5 history ground truth:
- `cmd_history(symbol=..., days=1)` retorna `{"history": [...]}` (ver
  mt5_executor.py:cmd_history)
- Cada deal tem: ticket, symbol, type ("BUY"/"SELL"), price, profit,
  commission, swap, volume, position_id, time, magic, comment
- `position_id` == `entry_ticket` do trade no DB (MT5 concept)
- Deal "out" (SELL) fecha um BUY aberto — `profit` é o PnL real do broker

NÃO TOCA:
- `vt_config.json`
- O autotrader rodando (a função é defensiva: try/except em todas
  as chamadas externas)
- `archive/`
- Scripts legados
"""

import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("vt_history_reconcile")

# Bruno 02/07: garantir import 'from mt5_orchestrator' funciona quando
# invocado standalone (fora do autotrader). Padrao igual a core/vt_autotrader.py:37.
sys.path.insert(0, str(Path(__file__).parent.parent / "mt5"))

DB_PATH = Path("/home/bruno/Projects/Vibe-Trading/vt_trades.db")


def _open_db(timeout: float = 10.0) -> sqlite3.Connection:
    """Conexão SQLite com WAL + busy_timeout (mesmo padrão de vt_trade_log).

    Failure mode: se DB está locked por >10s, propaga OperationalError
    pro caller, que aborta rápido (não trava autotrader).
    """
    conn = sqlite3.connect(str(DB_PATH), timeout=timeout)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def _get_db_open_trades(conn: sqlite3.Connection) -> list:
    """Lista trades do DB com `exit_time IS NULL` (incluindo EXCLUDED).

    Returns lista de Row com: id, entry_ticket, symbol, direction,
    entry_price, entry_time, volume, multiplier, strategy.
    """
    return conn.execute("""
        SELECT id, entry_ticket, symbol, direction, entry_price,
               entry_time, volume, multiplier, strategy
        FROM trades
        WHERE exit_time IS NULL
        AND entry_ticket IS NOT NULL
        AND entry_ticket != ''
        ORDER BY id
    """).fetchall()


def _is_already_reconciled(strategy: str) -> bool:
    """True se trade já foi processado por uma reconciliação anterior."""
    if not strategy:
        return False
    return "HISTORY_RECONCILE" in strategy


def reconcile_db_with_mt5_history(
    symbols: Optional[list] = None,
    days: int = 2,
    history_callable=None,
    notify_callable=None,
    log_callable=None,
) -> dict:
    """Reconcilia trades abertos (exit_time IS NULL) com MT5 history.

    Args:
        symbols: lista de símbolos para buscar (ex: ["WINQ26", "WDOQ26"]).
                 Se None, usa TODOS os símbolos do CONFIG do autotrader
                 (chamada dinâmica — mas defensiva: se falhar, usa
                 lista hardcoded como fallback).
        days: janela de busca no MT5 (default 2 para pegar deals de hoje
              e ontem, cobrindo EOD + restart).
        history_callable: função que recebe (symbol, days) e retorna dict
                          no formato `{"history": [...]}`. Default usa
                          `mt5.mt5_orchestrator.history`.
        notify_callable: função(msg) → notifica Telegram. Default: no-op.
        log_callable: função(msg) → log. Default: print.

    Returns:
        dict com ações tomadas:
            {
              "checked": N,           # trades abertos analisados
              "reconciled": M,        # trades atualizados com PnL real
              "still_open": K,        # trades abertos no DB E no MT5 (normal)
              "excluded_db_closed": L,# trades fantasmas: DB tem, MT5 não tem
              "errors": [...],        # erros não-fatais
            }
    """
    if log_callable is None:
        def log_callable(msg):
            if log.handlers:
                log.info(msg)
            else:
                print(f"[RECONCILE] {msg}")
    if notify_callable is None:
        def notify_callable(msg):
            return None

    if history_callable is None:
        try:
            from mt5_orchestrator import history as history_callable
        except Exception as _e:
            log_callable(f"[RECONCILE] Não foi possível importar mt5_orchestrator.history: {_e}")
            return {"checked": 0, "reconciled": 0, "still_open": 0, "excluded_db_closed": 0, "errors": [str(_e)]}

    # 1. Ler trades abertos do DB
    try:
        conn = _open_db(timeout=10.0)
    except Exception as _e:
        log_callable(f"[RECONCILE] DB connect falhou: {_e}")
        return {"checked": 0, "reconciled": 0, "still_open": 0, "excluded_db_closed": 0, "errors": [f"db_connect:{_e}"]}

    result = {
        "checked": 0,
        "reconciled": 0,
        "still_open": 0,
        "excluded_db_closed": 0,
        "errors": [],
    }

    try:
        open_trades = _get_db_open_trades(conn)

        if not open_trades:
            return result

        result["checked"] = len(open_trades)

        # 2. Determinar símbolos a consultar
        if not symbols:
            symbols = sorted({t["symbol"] for t in open_trades if t["symbol"]})
            if not symbols:
                return result

        # 3. Para cada símbolo, buscar MT5 history UMA VEZ (cache)
        deals_by_position = {}  # {position_id_str: deal_out}
        for sym in symbols:
            try:
                hist = history_callable(symbol=sym, days=days)
                if not isinstance(hist, dict):
                    continue
                # BUG FIX 30/06: mt5_executor.cmd_history retorna "history",
                # não "deals" — o código antigo em vt_autotrader.py:1826
                # tinha `_hist.get("deals", [])` que sempre dava [].
                deals = hist.get("history") or hist.get("deals") or []
                for d in deals:
                    pos_id = str(d.get("position_id") or d.get("entry_id") or "")
                    if not pos_id:
                        continue
                    # Para reconciliação, só nos interessam deals "out"
                    # (type=1=SELL, ou string "SELL") — mas aceitamos BUY
                    # também se for o único deal disponível.
                    d_type = d.get("type")
                    if d_type in (1, "SELL", "SELL_OUT"):
                        deals_by_position[pos_id] = d
                    elif pos_id not in deals_by_position:
                        # Fallback: guarda mesmo assim (pode ser BUY do
                        # próximo trade do mesmo ticket)
                        deals_by_position[pos_id] = d
            except Exception as _e:
                _err = f"history({sym}): {_e}"
                result["errors"].append(_err)
                log_callable(f"[RECONCILE] {sym}: {_err}")
                continue

        # 4. Para cada trade aberto no DB, ver se tem deal correspondente
        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        reconcile_tag = f"HISTORY_RECONCILE_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        for trade in open_trades:
            tid = trade["id"]
            entry_ticket = str(trade["entry_ticket"] or "")

            if not entry_ticket:
                continue

            # Pula trades já reconciliados (idempotência)
            if _is_already_reconciled(trade["strategy"] or ""):
                continue

            deal = deals_by_position.get(entry_ticket)
            if deal is None:
                # Trade ainda aberto no MT5 (legítimo) — não é drift
                result["still_open"] += 1
                continue

            # Deal encontrado → drift detectado!
            # Deal "out" = fechou uma posição "in" (BUY)
            # profit + commission + swap = PnL real do broker (net_pnl real)
            broker_profit = float(deal.get("profit") or 0)
            broker_commission = float(deal.get("commission") or 0)
            broker_swap = float(deal.get("swap") or 0)
            # Fees B3 estimadas (consistente com log_exit.calc_fees)
            try:
                from core.vt_trade_log import calc_fees as _calc_fees
                # exit_price será sobrescrito abaixo; usar entry_price como
                # bound para não distorcer taxa
                fees_est = _calc_fees(trade["volume"] or 0,
                                      trade["entry_price"] or 0,
                                      float(deal.get("price") or 0),
                                      trade["symbol"] or "")
            except Exception:
                fees_est = abs(broker_commission) if broker_commission else 0.0

            # net_pnl REAL do broker = profit + commission + swap
            # (commission geralmente é negativo, swap pode ser ±)
            # Substitui o valor estimado por log_exit (que pode estar
            # ausente do DB — exit_time=NULL)
            broker_net = broker_profit + broker_commission + broker_swap

            exit_price_real = float(deal.get("price") or 0)
            exit_ticket_real = str(deal.get("ticket") or "")

            # exit_reason: usar o que veio do MT5 se for SL/TP, senão SERVER
            mt5_reason = str(deal.get("deal_type") or deal.get("reason") or "").upper()
            if "SL" in mt5_reason or "STOP" in mt5_reason:
                exit_reason = "SL_SERVIDOR"
            elif "TP" in mt5_reason or "TAKE" in mt5_reason:
                exit_reason = "TP_SERVIDOR"
            else:
                exit_reason = "SERVER_HISTORY_RECONCILE"

            notes = (
                f"[{reconcile_tag}] Drift detectado. "
                f"DB exit_time=NULL mas MT5 fechou. "
                f"deal.ticket={exit_ticket_real} "
                f"deal.profit=R${broker_profit:+.2f} "
                f"deal.commission=R${broker_commission:+.2f} "
                f"deal.swap=R${broker_swap:+.2f} "
                f"broker_net_pnl=R${broker_net:+.2f}"
            )

            # UPDATE no DB — usar estratégia com sufixo para idempotência
            new_strategy = (trade["strategy"] or "") + f" [{reconcile_tag}]"

            try:
                conn.execute(
                    """
                    UPDATE trades SET
                        exit_time = COALESCE(?, ?),
                        exit_price = COALESCE(NULLIF(?, 0), entry_price),
                        exit_reason = ?,
                        exit_ticket = COALESCE(NULLIF(?, ''), 'server'),
                        swap = ?,
                        gross_pnl = ?,
                        fees = ?,
                        net_pnl = ?,
                        notes = COALESCE(notes, '') || ?,
                        strategy = ?,
                        close_source = ?,
                        updated_at = datetime('now', 'localtime')
                    WHERE id = ? AND exit_time IS NULL
                    """,
                    (
                        now_ts,
                        now_ts,                       # fallback se now_ts for NULL
                        exit_price_real,
                        exit_reason,
                        exit_ticket_real,
                        broker_swap,
                        broker_profit,                # gross_pnl = profit do deal
                        fees_est,
                        broker_net,                   # net_pnl REAL do broker
                        "\n" + notes,
                        new_strategy,
                        f"HISTORY_RECONCILE_{datetime.now().strftime('%H%M%S')}",
                        tid,
                    ),
                )

                if conn.total_changes > 0:
                    result["reconciled"] += 1
                    log_callable(
                        f"[RECONCILE] ✅ Trade #{tid} {trade['symbol']} "
                        f"{trade['direction']} ticket={entry_ticket} → "
                        f"closed @ {exit_price_real:.2f} "
                        f"PnL=R${broker_net:+.2f} (broker-truth)"
                    )
                    try:
                        notify_callable(
                            f"🔧 *Drift reconciliado*\n"
                            f"Trade #{tid} {trade['symbol']} {trade['direction']} "
                            f"(ticket {entry_ticket})\n"
                            f"PnL real (broker): R${broker_net:+.2f}\n"
                            f"Fonte: MT5 history (HISTORY_RECONCILE)"
                        )
                    except Exception:
                        pass
                else:
                    # Outra thread reconciliou primeiro — idempotente
                    result["still_open"] += 1

            except sqlite3.OperationalError as _e:
                # DB locked: loga e segue (próximo tick tenta de novo)
                _err = f"db_locked_trade#{tid}: {_e}"
                result["errors"].append(_err)
                log_callable(f"[RECONCILE] {trade['symbol']} #{tid}: DB locked, next tick will retry")
                continue
            except Exception as _e:
                _err = f"update_trade#{tid}: {_e}"
                result["errors"].append(_err)
                log_callable(f"[RECONCILE] {trade['symbol']} #{tid}: update falhou: {_e}")
                continue

        conn.commit()

    finally:
        try:
            conn.close()
        except Exception:
            pass

    log_callable(
        f"[RECONCILE] done: checked={result['checked']} "
        f"reconciled={result['reconciled']} "
        f"still_open={result['still_open']} "
        f"errors={len(result['errors'])}"
    )
    return result


def reconcile_pending_excluded(
    notify_callable=None,
    log_callable=None,
) -> int:
    """Fecha trades com exit_time IS NULL que JÁ FORAM EXCLUDED manualmente.

    Alguns trades ficam com exit_time NULL mas têm strategy = "X [EXCLUDED]"
    — significa que o operador já marcou como excluído. Esses devem ser
    fechados no DB com PnL=0 e exit_reason=EXCLUDED para evitar que
    continuem aparecendo como "open_db" no daily_summary.

    Returns: número de trades fechados.
    """
    if log_callable is None:
        def log_callable(msg):
            print(f"[RECONCILE-EXCLUDED] {msg}")
    if notify_callable is None:
        def notify_callable(msg):
            return None

    try:
        conn = _open_db(timeout=5.0)
    except Exception as _e:
        log_callable(f"[RECONCILE-EXCLUDED] DB connect falhou: {_e}")
        return 0

    closed = 0
    try:
        rows = conn.execute("""
            SELECT id, entry_ticket, symbol, strategy
            FROM trades
            WHERE exit_time IS NULL
            AND (strategy LIKE '%[EXCLUDED]%' OR strategy LIKE '%WATCHDOG_AUTO%')
        """).fetchall()

        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for row in rows:
            try:
                conn.execute("""
                    UPDATE trades SET
                        exit_time = ?,
                        exit_price = entry_price,
                        exit_reason = 'EXCLUDED_AUTO_CLOSE',
                        exit_ticket = 'server',
                        close_source = 'EXCLUDED_RECONCILE',
                        updated_at = datetime('now', 'localtime')
                    WHERE id = ? AND exit_time IS NULL
                """, (now_ts, row["id"]))
                if conn.total_changes > 0:
                    closed += 1
            except sqlite3.OperationalError as _e:
                log_callable(f"[RECONCILE-EXCLUDED] DB locked trade#{row['id']}: {_e}")
                continue
            except Exception as _e:
                log_callable(f"[RECONCILE-EXCLUDED] update#{row['id']} falhou: {_e}")
                continue

        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if closed:
        log_callable(f"[RECONCILE-EXCLUDED] {closed} trades EXCLUDED fechados no DB")
    return closed


if __name__ == "__main__":
    # CLI de teste (uso manual): python -m core.vt_history_reconcile
    r = reconcile_db_with_mt5_history(log_callable=print)
    print(f"\nResultado: {r}")
    n = reconcile_pending_excluded(log_callable=print)
    print(f"EXCLUDED reconciliados: {n}")
