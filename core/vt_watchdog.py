"""
Vibe-Trading Watchdog — sincronização periódica MT5 ↔ DB local.

Problema (23/06/2026):
  O autotrader opera um DB SQLite local, mas o MT5 está em outro processo
  (Wine). Posições abertas no MT5 podem não persistir no DB, e vice-versa.
  Solução: reconcile_with_mt5() detecta e corrige drift a cada 5 ciclos.

Cenários:
  A) MT5 tem posição, DB não tem → INSERT no DB com close_source=RECONCILIATION
  B) DB tem posição aberta, MT5 não tem → fecha no DB (PnL=0, exit_reason=MT5_MISSING)
  C) Ambos têm a mesma posição mas com dados divergentes → loga, não corrige

FILTRO DE RUÍDO (23/06/2026):
  reconcile_with_mt5() continua emitindo logs via log_fn para diagnóstico
  interno, mas as mensagens internas (RECONCILE divergência, inserido, fechado)
  são SEMPRE silenciosas (não Telegram). Para notificar drift real pro
  Telegram, use core.vt_notify_log_filter.notify_reconcile_drift() com o
  resultado.

Segurança:
  - Magic comment "VibeTrading" filtra posições de outros bots
  - PnL sempre calculado do MT5 (preço real), nunca inventado
  - Migração de schema é idempotente (ADD COLUMN IF NOT EXISTS não é SQLite,
    usamos try/except)
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

DB_PATH = Path("/home/bruno/Projects/Vibe-Trading/vt_trades.db")

CLOSE_SOURCE_SL_SERVER = "SL_SERVER"
CLOSE_SOURCE_SL_LOCAL = "SL_LOCAL"
CLOSE_SOURCE_EOD = "EOD"
CLOSE_SOURCE_RECONCILIATION = "RECONCILIATION"
CLOSE_SOURCE_MT5_MISSING = "MT5_MISSING"
CLOSE_SOURCE_MANUAL = "MANUAL"
CLOSE_SOURCE_STREAMING = "STREAMING"

# Multiplicadores R$/pt (espelho do vt_trade_log.py)
MULTIPLIER = {
    "WIN": 0.20,
    "WDO": 10.00,
    "BIT": 0.01,
    "DOL": 10.00,
    "IND": 0.20,
    "WSP": 0.01,
}


def _symbol_root(symbol: str) -> Optional[str]:
    """Extrai root de símbolo WINQ26 → WIN, WDOQ26 → WDO, etc."""
    for r in MULTIPLIER.keys():
        if r in symbol:
            return r
    return None


def migrate_close_source(conn: sqlite3.Connection) -> bool:
    """
    Adiciona coluna close_source à tabela trades. Idempotente.
    Retorna True se alterou schema, False se já existia.
    """
    cur = conn.execute("PRAGMA table_info(trades)")
    cols = [row[1] for row in cur.fetchall()]
    if "close_source" in cols:
        return False
    conn.execute("ALTER TABLE trades ADD COLUMN close_source TEXT DEFAULT NULL")
    conn.commit()
    return True


def _calc_pnl(direction: str, entry_price: float, exit_price: float,
              volume: float, symbol_root: str) -> tuple[float, float, float]:
    """
    Calcula PnL (gross, fees, net) a partir de preços e volume.
    Espelho da lógica de vt_trade_log.log_exit.
    """
    mult = MULTIPLIER.get(symbol_root, 1.0)
    if direction == "BUY":
        points = exit_price - entry_price
    else:
        points = entry_price - exit_price
    gross = points * mult * volume
    # Fee estimada: WIN/WDO ~R$ 0.50, BIT/WSP ~R$ 1.50 (mini contrato)
    fees = 1.50 if symbol_root in ("BIT", "WSP") else 0.50
    fees *= volume
    net = gross - fees
    return gross, fees, net


def reconcile_with_mt5(mt5_positions: list, log_fn=None) -> dict:
    """
    Reconcilia posições abertas no MT5 com o DB local.

    Args:
        mt5_positions: lista de dicts no formato retornado por status()['positions'].
                       Cada dict tem: ticket, symbol, type, volume, price_open,
                       price_current, sl, profit, time, comment.
        log_fn: função de log opcional (default: print).

    Returns:
        dict com:
          - inserted: lista de tickets inseridos no DB
          - closed: lista de tickets fechados no DB
          - divergences: lista de tickets com divergência de dados
          - skipped: lista de tickets pulados (sem comment VibeTrading)
          - errors: lista de erros
    """
    log = log_fn or (lambda m: None)

    if not DB_PATH.exists():
        return {"inserted": [], "closed": [], "divergences": [],
                "skipped": [], "errors": ["db_not_found"]}

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    migrate_close_source(conn)

    result = {
        "inserted": [],
        "closed": [],
        "divergences": [],
        "skipped": [],
        "errors": [],
    }

    # Mapa de posições abertas no DB
    try:
        db_open = {
            str(r["entry_ticket"]): r
            for r in conn.execute(
                "SELECT * FROM trades WHERE exit_time IS NULL"
            ).fetchall()
        }
    except Exception as e:
        conn.close()
        result["errors"].append(f"db_read: {e}")
        return result

    # Map ticket → MT5 position
    mt5_by_ticket = {}
    for p in mt5_positions:
        comment = p.get("comment", "")
        if comment != "VibeTrading":
            result["skipped"].append({
                "ticket": p.get("ticket"),
                "reason": "not_vibetrading",
                "comment": comment,
            })
            continue
        ticket = str(p.get("ticket", ""))
        if not ticket:
            continue
        mt5_by_ticket[ticket] = p

    # Cenário A: MT5 tem, DB não tem → INSERT
    for ticket, p in mt5_by_ticket.items():
        if ticket in db_open:
            # Cenário C: ambos têm, checar divergência
            db_row = db_open[ticket]
            db_entry = float(db_row["entry_price"] or 0)
            mt5_entry = float(p.get("price_open", 0) or 0)
            if db_entry and mt5_entry and abs(db_entry - mt5_entry) > 0.01:
                result["divergences"].append({
                    "ticket": ticket,
                    "symbol": p.get("symbol"),
                    "db_entry": db_entry,
                    "mt5_entry": mt5_entry,
                    "diff": mt5_entry - db_entry,
                })
                log(f"[RECONCILE] divergência {ticket}: DB={db_entry} MT5={mt5_entry}")
            continue

        symbol = p.get("symbol", "")
        root = _symbol_root(symbol)
        if not root:
            result["skipped"].append({"ticket": ticket, "reason": "unknown_symbol"})
            continue

        # Descobrir TF — padrão M5 (pode ser melhorado depois com base no tempo)
        tf = "M5"
        direction = "BUY" if p.get("type") in (0, "BUY") else "SELL"
        entry_price = float(p.get("price_open", 0))
        entry_sl = float(p.get("sl", 0)) or None
        volume = float(p.get("volume", 1.0))
        entry_time_dt = datetime.fromtimestamp(int(p.get("time", 0)))
        entry_time = entry_time_dt.strftime("%Y-%m-%d %H:%M:%S")
        multiplier = MULTIPLIER.get(root, 1.0)

        try:
            cur = conn.execute(
                """
                INSERT INTO trades
                (symbol, direction, volume, entry_time, entry_price,
                 entry_sl, entry_ticket, timeframe, strategy,
                 signal_detail, multiplier, close_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (symbol, direction, volume, entry_time, entry_price,
                 entry_sl, ticket, tf, "RECONCILED",
                 json.dumps({"reconciled_at": datetime.now().isoformat()}),
                 multiplier, CLOSE_SOURCE_RECONCILIATION),
            )
            conn.commit()
            result["inserted"].append({
                "ticket": ticket,
                "symbol": symbol,
                "direction": direction,
                "entry_price": entry_price,
                "tf": tf,
            })
            log(f"[RECONCILE] ✅ inserido {direction} {symbol} {tf} @ {entry_price} (ticket {ticket})")
        except Exception as e:
            result["errors"].append(f"insert {ticket}: {e}")
            log(f"[RECONCILE] ❌ erro inserindo {ticket}: {e}")

    # Cenário B: DB tem, MT5 não tem → fechar com PnL=0
    for ticket, db_row in db_open.items():
        if ticket in mt5_by_ticket:
            continue
        # Posição sumiu do MT5
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            entry_price = float(db_row["entry_price"] or 0)
            symbol_root = _symbol_root(db_row["symbol"] or "")
            direction = db_row["direction"] or "BUY"
            volume = float(db_row["volume"] or 1.0)

            # PnL = 0 (não temos preço de saída); fees pequenos
            gross, fees, net = 0.0, 0.0, 0.0

            conn.execute(
                """
                UPDATE trades SET
                    exit_time = ?,
                    exit_price = ?,
                    exit_reason = ?,
                    close_source = ?,
                    gross_pnl = ?,
                    fees = ?,
                    net_pnl = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, entry_price, "MT5_MISSING", CLOSE_SOURCE_MT5_MISSING,
                 gross, fees, net, now, db_row["id"]),
            )
            conn.commit()
            result["closed"].append({
                "ticket": ticket,
                "symbol": db_row["symbol"],
                "reason": "mt5_missing",
                "pnl": 0.0,
            })
            log(f"[RECONCILE] 🔒 fechado {db_row['symbol']} ticket {ticket} (sumiu do MT5)")
        except Exception as e:
            result["errors"].append(f"close {ticket}: {e}")
            log(f"[RECONCILE] ❌ erro fechando {ticket}: {e}")

    conn.close()
    return result


def get_open_positions_from_db(db_path: Optional[str] = None) -> list:
    """Retorna lista de posições abertas no DB (exit_time IS NULL)."""
    p = db_path or str(DB_PATH)
    if not Path(p).exists():
        return []
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM trades WHERE exit_time IS NULL ORDER BY entry_time DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def diff_db_vs_mt5(mt5_positions: list, db_path: Optional[str] = None) -> dict:
    """
    Compara estado MT5 vs DB sem modificar nada. Retorna diff legível.

    Returns:
        {
          "mt5_only": [tickets...],
          "db_only": [tickets...],
          "both_match": [tickets...],
          "divergences": [(ticket, db_entry, mt5_entry), ...]
        }
    """
    p = db_path or str(DB_PATH)
    if not Path(p).exists():
        return {"mt5_only": [], "db_only": [], "both_match": [], "divergences": []}

    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    db_open = {
        str(r["entry_ticket"]): float(r["entry_price"] or 0)
        for r in conn.execute("SELECT * FROM trades WHERE exit_time IS NULL").fetchall()
    }
    conn.close()

    mt5_tickets = set()
    mt5_data = {}
    for pos in mt5_positions:
        if pos.get("comment") != "VibeTrading":
            continue
        t = str(pos.get("ticket", ""))
        if t:
            mt5_tickets.add(t)
            mt5_data[t] = float(pos.get("price_open", 0) or 0)

    db_tickets = set(db_open.keys())
    mt5_only = list(mt5_tickets - db_tickets)
    db_only = list(db_tickets - mt5_tickets)
    common = mt5_tickets & db_tickets

    divergences = []
    both_match = []
    for t in common:
        if abs(db_open[t] - mt5_data[t]) > 0.01:
            divergences.append((t, db_open[t], mt5_data[t]))
        else:
            both_match.append(t)

    return {
        "mt5_only": mt5_only,
        "db_only": db_only,
        "both_match": both_match,
        "divergences": divergences,
    }
