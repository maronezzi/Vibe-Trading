"""
Vibe-Trading Orchestrator (Linux side).
Interface Python que eu (Hermes) uso para enviar ordens ao MT5.
Chama o mt5_executor.py via Wine subprocess.

Símbolos devem ser SEMPRE completos (ex: 'WDON26', 'WINM26').
O cron Symbol Resolver (8h55) salva os símbolos em vt_config.json.

Uso típico:
    from mt5_orchestrator import mt5
    mt5.status()
    mt5.buy('WDON26', volume=1, sl_pts=200)
    mt5.sell('WINM26', volume=1, sl_pts=50)
    mt5.close_all()
"""

import subprocess
import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# Fase 3 — Lei 3 (SL obrigatório) + Lei 4 (Garantia MT5): exceções e constantes
# nomeadas. Import defensivo: se vt_exceptions falhar, buy/sell caem no fallback
# de validação inline (continuam funcionando, só sem as mensagens ricas).
try:
    from core.vt_exceptions import (
        ACCEPTED_RETCODES,
        REASON_MISSING_STOP_LOSS,
        REASON_NOT_CONFIRMED,
        REASON_REJECTED_BY_RETCODE,
        error_dict,
    )
except Exception:  # pragma: no cover — fallback robusto
    ACCEPTED_RETCODES = frozenset({10008, 10009})
    REASON_MISSING_STOP_LOSS = "MISSING_STOP_LOSS"
    REASON_NOT_CONFIRMED = "NOT_CONFIRMED"
    REASON_REJECTED_BY_RETCODE = "REJECTED_BY_RETCODE"

    def error_dict(reason, detail="", **extra):
        d = {"status": "BLOCKED", "reason": reason, "ticket": 0}
        if detail:
            d["detail"] = detail
        d.update(extra)
        return d

PROJECT = Path("/home/bruno/Projects/Vibe-Trading")
WINE_PYTHON = os.path.expanduser("~/.wine/drive_c/Python311/python.exe")
EXECUTOR_WIN = "Z:\\home\\bruno\\Projects\\Vibe-Trading\\mt5\\mt5_executor.py"
RESOLVE_WIN = "Z:\\home\\bruno\\Projects\\Vibe-Trading\\mt5_resolve.py"

# DB de trades — mesmo path usado por core.vt_trade_log
TRADES_DB = PROJECT / "vt_trades.db"

# Schema mínimo necessário para _persist_close_to_db() — espelha o que
# core.vt_trade_log.init_db() cria (somente as colunas que tocamos aqui).
# Mantido localmente porque o orchestrator não importa o módulo inteiro
# só para ler uma conexão (e para deixar este módulo self-contained).
_TRADES_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_ticket TEXT,
    exit_ticket TEXT,
    magic_number INTEGER DEFAULT 555501,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    volume REAL NOT NULL,
    timeframe TEXT DEFAULT 'M5',
    entry_time TEXT NOT NULL,
    entry_price REAL NOT NULL,
    entry_sl REAL,
    exit_time TEXT,
    exit_price REAL,
    exit_reason TEXT,
    exit_sl_price REAL,
    gross_pnl REAL DEFAULT 0,
    fees REAL DEFAULT 0,
    swap REAL DEFAULT 0,
    net_pnl REAL DEFAULT 0,
    is_day_trade INTEGER DEFAULT 1,
    asset_type TEXT DEFAULT 'FUTURE',
    multiplier REAL DEFAULT 0.20,
    strategy TEXT DEFAULT 'VWAP',
    signal_detail TEXT,
    raw_entry_json TEXT,
    raw_exit_json TEXT,
    notes TEXT,
    close_source TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_trades_entry_ticket ON trades(entry_ticket);
"""


def _run_wine(script: str, *args, timeout=30) -> dict:
    """Roda um script Python dentro do Wine e retorna o JSON do stdout.

    Wave N+4C (2026-07-08): instrumenta latência via core.vt_latency_monitor
    automaticamente para todas as chamadas. Identifica a op pelo argv[1]
    (primeiro arg pos-script) — ex.: 'buy', 'sell', 'modify', 'close',
    'partial_close', 'status', etc.
    """
    cmd = ["wine", WINE_PYTHON, script, *args]
    env = {**os.environ, "WINEDEBUG": "-all"}

    # Wave N+4C: mede latência Wine end-to-end (start subprocess → retorno).
    op = args[0] if args else script.split("_")[0]
    _t0 = time.perf_counter()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        _record_latency_safe(op, time.perf_counter() - _t0)
        return {"error": "timeout"}
    except Exception as e:
        _record_latency_safe(op, time.perf_counter() - _t0)
        return {"error": str(e)}

    _record_latency_safe(op, time.perf_counter() - _t0)

    out = r.stdout.strip()
    err = r.stderr.strip()

    # stderr tem os logs; descarta-os
    # stdout tem o JSON
    # Pega do primeiro "{" ao último "}" (multi-line JSON)
    if "{" in out:
        start = out.find("{")
        end = out.rfind("}")
        if end > start:
            candidate = out[start:end+1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
    # Se não tem JSON, retorna raw
    return {"raw_stdout": out[-500:] if out else "", "raw_stderr": err[-500:] if err else "", "returncode": r.returncode}


def _record_latency_safe(op: str, ms: float) -> None:
    """Wave N+4C: anota latência; nunca propaga exceção."""
    try:
        from core.vt_latency_monitor import record_latency
        record_latency(op, ms * 1000.0)  # s → ms
    except Exception as exc:
        _log(f"latency_monitor record falhou: {exc!r}")


def _log(msg):
    """Log silencioso pro /tmp."""
    with open("/tmp/vt_orchestrator.log", "a") as f:
        from datetime import datetime
        f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")


def resolve_symbol(root: str) -> Optional[str]:
    """Retorna o símbolo de maior liquidez (ex: 'WINQ26' ou 'WDOQ26')."""
    r = _run_wine(RESOLVE_WIN, root)
    if "best" in r and r["best"]:
        return r["best"]["name"]
    return None


def status() -> dict:
    return _run_wine(EXECUTOR_WIN, "status")


def tick(symbol: str) -> dict:
    return _run_wine(EXECUTOR_WIN, "tick", symbol)


def info(symbol: str) -> dict:
    return _run_wine(EXECUTOR_WIN, "info", symbol)


def _validate_order_safety(symbol: str, sl_pts) -> Optional[dict]:
    """Lei 3: SL obrigatório. Retorna dict BLOCKED se sl inválido, else None.

    Valida ANTES de chamar _run_wine (não confia no caller). sl_pts deve ser
    int > 0. None/0/negativo → BLOCKED com reason MISSING_STOP_LOSS.

    NÃO propaga exceção: devolve dict no contrato existente (status=BLOCKED),
    que safe_buy/safe_sell já sabem tratar via _classify_error (entra no fluxo
    de retry/abort). Assim a defesa da Lei 3 é garantida SEM derrubar o bot.
    """
    if sl_pts is None or not isinstance(sl_pts, (int, float)) or sl_pts <= 0:
        return error_dict(
            REASON_MISSING_STOP_LOSS,
            detail=f"{symbol} sl_pts={sl_pts} (invalido). Lei 3: SL obrigatorio.",
            symbol=symbol, sl_pts=sl_pts,
        )
    return None


def _validate_order_confirmed(symbol: str, side: str, result: dict) -> dict:
    """Lei 4: ordem só é "aberta" se MT5 confirmar ticket + retcode aceito.

    Opera sobre o dict retornado por _run_wine. Se já é FILLED com ticket>0,
    retorna result inalterado (sucesso). Senão devolve dict BLOCKED com a
    razão específica (NOT_CONFIRMED se sem ticket, REJECTED_BY_RETCODE se
    retcode não-aceito). Mantém compat: quem checa status=='FILLED' continua
    funcionando; quem checa ticket>0 também.
    """
    if not isinstance(result, dict):
        return result  # contrato inesperado — não inferir, devolve como está
    status = result.get("status")
    # Sucesso já confirmado pelo executor (FILLED + ticket)
    if status == "FILLED":
        ticket = result.get("ticket", 0)
        try:
            ticket_int = int(ticket) if ticket not in (None, "?", "") else 0
        except (ValueError, TypeError):
            ticket_int = 0
        if ticket_int > 0:
            return result  # OK — Lei 4 satisfeita
        # FILLED mas sem ticket válido → trata como não confirmado
        return error_dict(
            REASON_NOT_CONFIRMED,
            detail=f"{symbol} {side}: FILLED sem ticket valido (ticket={ticket}).",
            symbol=symbol, side=side,
        )
    # retcode presente mas status != FILLED: verifica se broker aceitou
    retcode = result.get("retcode")
    if retcode is not None:
        try:
            rc = int(retcode)
        except (ValueError, TypeError):
            rc = 0
        if rc in ACCEPTED_RETCODES and result.get("ticket"):
            # Bug latente corrigido: retcode 10008 (PLACED) era tratado como
            # REJECTED. Agora se retcode é aceito E tem ticket, confirma.
            result = dict(result)
            result["status"] = "FILLED"
            result["recovered_placed"] = True
            _log(f"[LEI4] {symbol} {side}: retcode {rc} aceito, ticket "
                 f"{result.get('ticket')} — confirmando (recuperação PLACED)")
            return result
        if rc not in ACCEPTED_RETCODES and rc != 0:
            return error_dict(
                REASON_REJECTED_BY_RETCODE,
                detail=f"{symbol} {side}: retcode={rc} não aceito "
                       f"(válidos: {sorted(ACCEPTED_RETCODES)}).",
                symbol=symbol, retcode=rc,
            )
    # status REJECTED / error / sem retcode — devolve como está (já é erro)
    return result


def buy(symbol: str, volume: float = 1.0, sl_pts: Optional[int] = None,
        tp_pts: Optional[int] = None) -> dict:
    """Compra com SL obrigatório (Lei 3) e confirmação MT5 (Lei 4).

    Valida sl_pts > 0 ANTES de enviar (Lei 3). Após _run_wine, valida que MT5
    retornou ticket válido + retcode aceito (Lei 4). Em caso de violação,
    devolve dict {"status":"BLOCKED","reason":...,"ticket":0} no contrato
    existente — NÃO propaga exceção (não derruba o autotrader ao vivo).
    Símbolo deve ser completo (ex: 'WDON26').
    """
    blocked = _validate_order_safety(symbol, sl_pts)
    if blocked is not None:
        _log(f"[LEI3] BUY {symbol} BLOCKED sem SL (sl_pts={sl_pts})")
        return blocked
    args = ["buy", symbol, str(volume), str(sl_pts)]
    if tp_pts is not None:
        args.append(str(tp_pts))
    result = _run_wine(EXECUTOR_WIN, *args)
    result = _validate_order_confirmed(symbol, "BUY", result)
    _log(f"BUY {symbol} vol={volume} sl={sl_pts} → {result.get('status', result.get('error', '?'))}")
    return result


def sell(symbol: str, volume: float = 1.0, sl_pts: Optional[int] = None,
         tp_pts: Optional[int] = None) -> dict:
    """Vende com SL obrigatório (Lei 3) e confirmação MT5 (Lei 4).

    Mesmo contrato e defesas de buy(). Símbolo deve ser completo (ex: 'WDON26').
    """
    blocked = _validate_order_safety(symbol, sl_pts)
    if blocked is not None:
        _log(f"[LEI3] SELL {symbol} BLOCKED sem SL (sl_pts={sl_pts})")
        return blocked
    args = ["sell", symbol, str(volume), str(sl_pts)]
    if tp_pts is not None:
        args.append(str(tp_pts))
    result = _run_wine(EXECUTOR_WIN, *args)
    result = _validate_order_confirmed(symbol, "SELL", result)
    _log(f"SELL {symbol} vol={volume} sl={sl_pts} → {result.get('status', result.get('error', '?'))}")
    return result


def _persist_close_to_db(symbol: str, details: list) -> dict:
    """
    Persiste o PnL de cada close em vt_trades.db.

    Para cada detail (um dict com ticket/symbol/close_price/profit/swap/...):
      - Procura trade existente pelo entry_ticket (= detail.ticket).
      - Se achar E exit_time IS NULL → UPDATE (trade legit).
      - Se achar E exit_time IS NOT NULL → UPDATE de novo (replay-safe; preserva
        exit original se já estava preenchido mas permite reconciliação).
      - Se NÃO achar → INSERT novo trade como orphan (server-close que o bot
        nunca viu entrar).
      - Se detail vier com chave 'error' (retcode != DONE) → pula.

    Nunca lança: qualquer erro de DB é capturado e logado, mas a chamada
    `close()` SEMPRE retorna o JSON do executor para o caller.
    """
    stats = {"updated": 0, "orphans_inserted": 0, "skipped": 0, "errors": 0}
    if not details:
        return stats

    # Garante que o schema existe (idempotente). Se o DB não existir ainda,
    # cria com a schema mínima — produção já tem o DB completo, mas isso
    # também deixa o módulo utilizável em testes com tmp_path.
    db_path = TRADES_DB
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.executescript(_TRADES_SCHEMA)
        conn.commit()
    except Exception as e:
        _log(f"DB_UNAVAILABLE ao abrir {db_path}: {e}")
        stats["errors"] += 1
        return stats

    try:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for detail in details:
            try:
                # Pula detalhes com erro (retcode != DONE no executor)
                if "error" in detail and "ticket" not in detail.get("error", ""):
                    # detalhe de falha do MT5 não tem profit; ignora
                    stats["skipped"] += 1
                    continue

                ticket = str(detail.get("ticket", "") or "")
                if not ticket:
                    stats["skipped"] += 1
                    continue

                profit = float(detail.get("profit", 0) or 0)
                close_price = float(detail.get("close_price", 0) or 0)
                swap = float(detail.get("swap", 0) or 0)
                direction = detail.get("type", "BUY")
                volume = float(detail.get("volume", 0) or 0)
                entry_price = float(detail.get("entry_price", 0) or 0)
                magic = int(detail.get("magic", 555501) or 555501)

                # 1) Procura trade existente pelo entry_ticket
                row = conn.execute(
                    "SELECT id, exit_time FROM trades WHERE entry_ticket = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (ticket,),
                ).fetchone()

                if row is not None:
                    # UPDATE trade existente
                    if row["exit_time"]:
                        # Já tinha exit_time → reescreve só se profit novo
                        # for diferente (reconciliação). Não sobrescreve exit_time.
                        conn.execute(
                            """
                            UPDATE trades SET
                                exit_price = ?,
                                gross_pnl = ?,
                                swap = ?,
                                net_pnl = ?,
                                exit_reason = COALESCE(exit_reason, 'MANUAL_CLOSE_OR_ORPHAN'),
                                close_source = COALESCE(close_source, 'mt5_orchestrator_close'),
                                updated_at = datetime('now', 'localtime')
                            WHERE id = ?
                            """,
                            (close_price, profit, swap, profit, row["id"]),
                        )
                        _log(
                            f"[ORCHESTRATOR_CLOSE] Re-reconciled trade id={row['id']} "
                            f"ticket={ticket} PnL=R${profit:+.2f}"
                        )
                    else:
                        # Trade legit sem exit → UPDATE completo
                        conn.execute(
                            """
                            UPDATE trades SET
                                exit_time = ?,
                                exit_price = ?,
                                gross_pnl = ?,
                                swap = ?,
                                net_pnl = ?,
                                exit_reason = 'MANUAL_CLOSE_OR_ORPHAN',
                                close_source = 'mt5_orchestrator_close',
                                updated_at = datetime('now', 'localtime')
                            WHERE id = ?
                            """,
                            (now_str, close_price, profit, swap, profit, row["id"]),
                        )
                        _log(
                            f"[ORCHESTRATOR_CLOSE] Updated trade id={row['id']} "
                            f"ticket={ticket} PnL=R${profit:+.2f}"
                        )
                    stats["updated"] += 1
                else:
                    # 2) ORPHAN genuíno — MT5 fechou algo que o DB não conhecia
                    #    (pode ser manual close via MT5 GUI, ou trade criado
                    #    pelo reconcile antes do entry_ticket chegar aqui).
                    conn.execute(
                        """
                        INSERT INTO trades (
                            entry_ticket, exit_ticket, magic_number,
                            symbol, direction, volume, timeframe,
                            entry_time, entry_price, entry_sl,
                            exit_time, exit_price, exit_reason,
                            gross_pnl, fees, swap, net_pnl,
                            strategy, raw_exit_json, notes, close_source
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL,
                                  ?, ?, ?, ?, 0, ?, ?,
                                  'MANUAL_ORPHAN', ?, ?, ?)
                        """,
                        (
                            ticket, ticket, magic,
                            detail.get("symbol", symbol), direction, volume, "M5",
                            now_str, entry_price,
                            now_str, close_price, "MANUAL_CLOSE_OR_ORPHAN",
                            profit, swap, profit,
                            json.dumps(detail, default=str),
                            f"[orchestrator_close] orphan ingested ticket={ticket}",
                            "mt5_orchestrator_close",
                        ),
                    )
                    stats["orphans_inserted"] += 1
                    _log(
                        f"[ORCHESTRATOR_CLOSE] Inserted orphan ticket={ticket} "
                        f"symbol={detail.get('symbol', symbol)} PnL=R${profit:+.2f}"
                    )
            except Exception as e:
                # Nunca quebra o close por causa de 1 detail problemático
                stats["errors"] += 1
                _log(
                    f"[ORCHESTRATOR_CLOSE] ERROR processing detail "
                    f"{detail.get('ticket', '?')}: {e}"
                )
                continue

        conn.commit()
    except Exception as e:
        stats["errors"] += 1
        _log(f"[ORCHESTRATOR_CLOSE] DB transaction error: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return stats


def close(symbol: str) -> dict:
    """
    Fecha posição do símbolo.

    IMPORTANTE: após MT5 retornar sucesso (status='ok', closed>=1),
    persiste o PnL no DB (vt_trades.db). Sem isso, MT5 fecha o trade mas
    o DB fica com gross_pnl=0 e net_pnl=0 — bug histórico (2026-07-01).
    """
    result = _run_wine(EXECUTOR_WIN, "close", symbol)

    # Side-effect: persistir no DB se MT5 fechou algo
    if isinstance(result, dict) and result.get("status") == "ok":
        details = result.get("details") or []
        closed = result.get("closed", 0)
        if details and closed >= 1:
            try:
                db_stats = _persist_close_to_db(symbol, details)
                result["db_persist"] = db_stats
                _log(
                    f"CLOSE {symbol} → {closed} fechado(s); "
                    f"DB updated={db_stats['updated']} "
                    f"orphans={db_stats['orphans_inserted']} "
                    f"errors={db_stats['errors']}"
                )
            except Exception as e:
                # Nunca crashar o close por causa do DB update
                result["db_persist_error"] = str(e)
                _log(f"CLOSE {symbol} DB persist falhou: {e}")

    _log(f"CLOSE {symbol} → {result.get('status', result.get('error', '?'))}")
    return result


def close_all() -> dict:
    return _run_wine(EXECUTOR_WIN, "close_all")


def modify_sl(symbol: str, ticket: int, new_sl_pts: int) -> dict:
    """
    Modifica o Stop Loss de uma posição aberta.
    symbol: símbolo completo (ex: 'WDON26')
    ticket: ticket da posição no MT5
    new_sl_pts: novo SL em pontos
    """
    result = _run_wine(EXECUTOR_WIN, "modify", symbol, str(ticket), str(new_sl_pts))
    _log(f"MODIFY_SL {symbol} ticket={ticket} new_sl={new_sl_pts} → {result.get('status', result.get('error', '?'))}")
    return result


def partial_close(symbol: str, ticket: int, close_volume: float) -> dict:
    """Wave N+2A (2026-07-08): fechamento parcial (TP1).

    Args:
        symbol: contrato MT5 resolvido (ex: 'WDON26').
        ticket: ticket da posição no MT5 (int).
        close_volume: fração em contratos (ex: 0.5 = meio lote).

    Returns:
        dict com status, ticket, closed_volume, remaining_volume, exit_price,
        profit, comment. Mesma forma do JSON retornado por
        mt5_executor.cmd_partial_close.
    """
    if close_volume <= 0:
        return {"status": "error", "error": "close_volume deve ser > 0"}
    result = _run_wine(
        EXECUTOR_WIN, "partial_close", symbol,
        str(ticket), str(float(close_volume)),
    )
    status = result.get("status", result.get("error", "?"))
    _log(
        f"PARTIAL_CLOSE {symbol} ticket={ticket} close_vol={close_volume} "
        f"→ {status} (restante={result.get('remaining_volume', '?')})"
    )
    return result


def symbol_info(symbol: str) -> dict:
    """Contract specs (point, digits, tick_size, tick_value, volume, margin, stops)."""
    return _run_wine(EXECUTOR_WIN, "symbol_info", symbol)


def book(symbol: str) -> dict:
    """Market depth (DOM / Level 2)."""
    return _run_wine(EXECUTOR_WIN, "book", symbol)


def orders() -> dict:
    """Pending orders with full details."""
    return _run_wine(EXECUTOR_WIN, "orders")


def bars(symbol: str, tf_str: str = "M5", count: int = 50) -> dict:
    """OHLCV bars. tf_str: M1/M5/M15/M30/H1/H4/D1."""
    return _run_wine(EXECUTOR_WIN, "bars", symbol, tf_str, str(count))


def history(symbol: str = None, days: int = 7, position: str = None) -> dict:
    """Deal history for the last N days, or by specific ticket (position_id).

    Wave 14.3 (Bruno 2026-07-14): adicionado `position` para contornar bug do
    Wine MT5 onde history_deals_get(symbol=..., date_from=...) retorna [].
    Wave 880.I (Bruno 2026-07-20): restaurado — parâmetro tinha sido perdido
    por reset/git, quebrando reconcile ghost (C-2) e get_position_history (C-1).
    Filtrar por position_id funciona e é o único caminho confiável no Wine.
    """
    if position:
        # Filtrar por ticket — formato: history <ticket>
        return _run_wine(EXECUTOR_WIN, "history", str(position), timeout=60)
    args = ["history"]
    if symbol:
        args.append(symbol)
    args.append(str(days))
    return _run_wine(EXECUTOR_WIN, *args, timeout=60)


if __name__ == "__main__":
    # CLI de teste
    import sys
    if len(sys.argv) < 2:
        print("Uso: python mt5_orchestrator.py <comando>")
        print("Comandos: status, tick, info, symbol_info, book, orders, bars, history, buy, sell, close, close_all, resolve")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "status":
        print(json.dumps(status(), indent=2))
    elif cmd == "tick":
        print(json.dumps(tick(sys.argv[2]), indent=2))
    elif cmd == "info":
        print(json.dumps(info(sys.argv[2]), indent=2))
    elif cmd == "buy":
        sym = sys.argv[2]
        vol = float(sys.argv[3])
        sl = int(sys.argv[4]) if len(sys.argv) > 4 else None
        tp = int(sys.argv[5]) if len(sys.argv) > 5 else None
        print(json.dumps(buy(sym, vol, sl, tp), indent=2))
    elif cmd == "sell":
        sym = sys.argv[2]
        vol = float(sys.argv[3])
        sl = int(sys.argv[4]) if len(sys.argv) > 4 else None
        tp = int(sys.argv[5]) if len(sys.argv) > 5 else None
        print(json.dumps(sell(sym, vol, sl, tp), indent=2))
    elif cmd == "close":
        print(json.dumps(close(sys.argv[2]), indent=2))
    elif cmd == "close_all":
        print(json.dumps(close_all(), indent=2))
    elif cmd == "resolve":
        print(f"Best {sys.argv[2]}: {resolve_symbol(sys.argv[2])}")
    elif cmd == "symbol_info":
        print(json.dumps(symbol_info(sys.argv[2]), indent=2))
    elif cmd == "book":
        print(json.dumps(book(sys.argv[2]), indent=2))
    elif cmd == "orders":
        print(json.dumps(orders(), indent=2))
    elif cmd == "bars":
        sym = sys.argv[2]
        tf = sys.argv[3] if len(sys.argv) > 3 else "M5"
        cnt = int(sys.argv[4]) if len(sys.argv) > 4 else 50
        print(json.dumps(bars(sym, tf, cnt), indent=2))
    elif cmd == "history":
        sym = sys.argv[2] if len(sys.argv) > 2 else None
        days = int(sys.argv[3]) if len(sys.argv) > 3 else 7
        print(json.dumps(history(sym, days), indent=2))
