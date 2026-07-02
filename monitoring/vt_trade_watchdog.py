#!/usr/bin/env python3
"""
Vibe-Trading Trade Watchdog — Garante que TODAS as posições abertas no MT5
são rastreadas pelo autotrader. Roda a cada 2 minutos via cron.

Capacidades:
1. Query MT5 positions via Wine
2. Compare with bot state → find orphans + ghosts
3. Handle orphans: alert via Telegram
4. Check balance/equity from MT5
5. Check position consistency (ghost positions)
6. Check trade log integrity (SQLite vs MT5)
7. Save state to /tmp/vt_watchdog_status.json
8. Telegram alerts only when issues found
9. Pure Python, no LLM, <10s execution

Uso:
    python3 vt_trade_watchdog.py          # Run full check
    python3 vt_trade_watchdog.py --json   # Output JSON only (no Telegram)
"""

import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "core"))  # noqa: E402 — garante import de vt_hermes_helper (espelha fix de vt_daily_report.py em 17/06/2026)

from mt5.mt5_orchestrator import status as mt5_status
from core.vt_config_loader import load_config
# Fase 4 (architecture_proposal_2026_07_01.md, secao 4.4):
# PnL diario vem do truth layer (MT5 history broker-truth), NAO de
# SELECT SUM(net_pnl) sobre o DB. DB vira comparacao para detectar drift.
from core import vt_truth

# ===== CONFIG =====
STATE_FILE = "/tmp/vt_autotrader_state.json"
STATUS_FILE = "/tmp/vt_watchdog_status.json"
DB_PATH = Path(__file__).parent.parent / "vt_trades.db"
TELEGRAM_TARGET = "telegram:-1004284773048"
MAGIC = 555501

# Fase 4: limite (em R$) acima do qual o watchdog dispara alerta de drift
# entre PnL MT5-truth e PnL DB. 5 reais eh ruido operacional (comissao,
# swap residual); acima disso indica dessincronizacao real entre bot e
# broker.
DRIFT_THRESHOLD_REAIS = Decimal("5.00")


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def notify_telegram(msg):
    try:
        from core.vt_hermes_helper import hermes_send  # CORRIGIDO 2026-06-26: usar caminho qualificado

        hermes_send(TELEGRAM_TARGET, msg)
    except Exception as e:
        log(f"[NOTIFY FAIL] {e}")


# ===== 1. QUERY MT5 POSITIONS =====
def get_mt5_positions():
    """Query MT5 for ALL open positions via Wine."""
    try:
        data = mt5_status()
        if "error" in data:
            log(f"[MT5 ERRO] {data['error']}")
            return [], {}
        positions = data.get("positions", [])
        account = data.get("account", {})
        return positions, account
    except Exception as e:
        log(f"[MT5 ERRO] {e}")
        return [], {}


# ===== 2. READ BOT STATE =====
def get_bot_positions():
    """Read autotrader state file for tracked positions."""
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return data.get("positions", {})
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log(f"[STATE ERRO] {e}")
        return {}


def get_db_open_trades():
    """Read open trades from the database (authoritative source).

    Returns a dict keyed by entry_ticket for fast lookup.
    """
    if not DB_PATH.exists():
        return {}
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, symbol, direction, entry_ticket, volume, entry_price, entry_time "
            "FROM trades WHERE (exit_time IS NULL OR exit_time = '') "
            # Wave 1C.1 (2026-07-02): 28 trades com ticket=12345/99999 eram teste,
            # marcados [EXCLUDED_TEST_2026_07_02]. Nao sao operacao real. Filtrar
            # pra nao alarmar como "fantasma" (Bruno confirmou).
            "AND (strategy IS NULL OR INSTR(strategy, '[EXCLUDED') = 0)"
        ).fetchall()
        conn.close()
        db_trades = {}
        for r in rows:
            ticket = str(r["entry_ticket"])
            db_trades[ticket] = dict(r)
        return db_trades
    except Exception as e:
        log(f"[DB READ ERRO] {e}")
        return {}


# ===== 3. COMPARE: FIND ORPHANS + GHOSTS =====
def find_discrepancies(mt5_positions, bot_positions, config):
    """Compare MT5 vs bot state + DB. Returns (orphans, ghosts, sync_fixes).

    orphans: positions in MT5 but NOT tracked by bot AND NOT in DB
    ghosts: positions tracked by bot but NOT in MT5
    sync_fixes: positions found in DB but missing from state file (stale state)
    """
    resolved = config.get("resolved_symbols", {})
    tracked_symbols = set(resolved.values())  # e.g. {"WINQ26", "WDON26", ...}
    magic = config.get("magic", MAGIC)

    # Load DB as authoritative fallback
    db_open = get_db_open_trades()

    # MT5 positions keyed by ticket
    mt5_by_ticket = {}
    for p in mt5_positions:
        ticket = str(p.get("ticket", ""))
        mt5_by_ticket[ticket] = p

    # Bot state keyed by ticket
    bot_by_ticket = {}
    for key, pos in bot_positions.items():
        ticket = str(pos.get("entry_ticket", ""))
        if ticket:
            bot_by_ticket[ticket] = {**pos, "state_key": key}

    # Orphans: MT5 has, bot doesn't track — but check DB first
    orphans = []
    sync_fixes = []  # state file was stale, DB has the trade
    for ticket, p in mt5_by_ticket.items():
        symbol = p.get("symbol", "")
        comment = p.get("comment", "")

        # Only care about our symbols
        if symbol not in tracked_symbols:
            continue
        # Only care about our magic number or VibeTrading comment
        if comment != "VibeTrading" and p.get("magic", 0) != magic:
            continue

        if ticket not in bot_by_ticket:
            # Not in state file — check DB before flagging as orphan
            if ticket in db_open:
                # State file is stale, DB has it → NOT a true orphan
                db_trade = db_open[ticket]
                log(f"[SYNC FIX] Ticket {ticket} ({symbol}) missing from state file "
                    f"but found in DB trade #{db_trade['id']} — not flagging as orphan")
                sync_fixes.append({
                    "ticket": ticket,
                    "symbol": symbol,
                    "db_trade_id": db_trade["id"],
                })
            else:
                # Truly orphan: not in state file AND not in DB
                orphans.append(p)
                log(f"[TRUE ORPHAN] Ticket {ticket} ({symbol}) not in state file "
                    f"and not in DB — needs attention")

    # Ghosts: bot tracks, MT5 doesn't have
    ghosts = []
    for ticket, pos in bot_by_ticket.items():
        if ticket not in mt5_by_ticket:
            ghosts.append(pos)

    return orphans, ghosts, sync_fixes


# ===== 4. CHECK BALANCE/EQUITY =====
def check_account(account):
    """Check balance and equity from MT5."""
    balance = account.get("balance", 0)
    equity = account.get("equity", 0)
    margin_free = account.get("free_margin", 0)
    margin_level = account.get("margin_level", 0)

    issues = []
    # Alert if equity drops below 95% of balance (significant drawdown)
    if balance > 0 and equity < balance * 0.95:
        drop_pct = (1 - equity / balance) * 100
        issues.append(f"Equity {drop_pct:.1f}% abaixo do saldo")

    # Alert if margin level is dangerously low (< 200%)
    if margin_level > 0 and margin_level < 200:
        issues.append(f"Margem nível {margin_level:.0f}% (crítico)")

    return balance, equity, margin_free, issues


# ===== 5. CHECK TRADE LOG INTEGRITY =====
def check_trade_log(mt5_positions):
    """Compare SQLite open trades vs MT5 positions."""
    if not DB_PATH.exists():
        return []

    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row

    try:
        open_trades = conn.execute(
            "SELECT id, symbol, direction, entry_ticket, entry_price FROM trades "
            "WHERE exit_time IS NULL "
            # Wave 1C.1 (2026-07-02): filtro EXCLUDED — ver L110-114
            "AND (strategy IS NULL OR INSTR(strategy, '[EXCLUDED') = 0)"
        ).fetchall()
    except Exception as e:
        log(f"[DB ERRO] {e}")
        conn.close()
        return []
    finally:
        conn.close()

    mt5_tickets = {str(p["ticket"]) for p in mt5_positions}
    issues = []

    for trade in open_trades:
        ticket = str(trade["entry_ticket"])
        if ticket not in mt5_tickets:
            issues.append(
                {
                    "type": "DB_ORPHAN",
                    "trade_id": trade["id"],
                    "symbol": trade["symbol"],
                    "direction": trade["direction"],
                    "ticket": ticket,
                    "msg": f"DB #{trade['id']} {trade['direction']} {trade['symbol']} ticket={ticket} não existe no MT5",
                }
            )

    return issues


# ===== 6. CHECK DAILY PNL DRIFT (FASE 4 — TRUTH LAYER) =====
def get_db_daily_pnl(date_iso: Optional[str] = None) -> Decimal:
    """PnL diario calculado direto do DB local (fonte SECUNDARIA).

    Mantido apenas para comparar contra a verdade MT5 e detectar drift.
    NAO deve ser usado para decisao de risco, alerta, ou relatorio — esse
    papel eh do truth layer (vt_truth.get_daily_pnl).

    Args:
        date_iso: data de referencia YYYY-MM-DD. Se None, usa "hoje" (local).

    Returns:
        Decimal em R$ (precisao 0.01). Zero se DB indisponivel / vazio.

    Comportamento:
        - FAIL-SAFE: erros nao levantam; retorna Decimal('0.00').
        - Filtra trades com exit_time preenchido (fechados) na data alvo.
        - Soma net_pnl (que ja incorpora gross - fees - swap no bot).
    """
    if date_iso is None:
        date_iso = datetime.now().strftime("%Y-%m-%d")
    if not DB_PATH.exists():
        return Decimal("0.00")
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT COALESCE(SUM(net_pnl), 0.0) AS total "
            "FROM trades "
            "WHERE date(entry_time) = ? AND exit_time IS NOT NULL",
            (date_iso,),
        ).fetchone()
        conn.close()
        total = row["total"] if row else 0.0
        return Decimal(str(total)).quantize(Decimal("0.01"))
    except Exception as e:
        log(f"[DB DAILY PNL ERRO] {e}")
        return Decimal("0.00")


def get_mt5_daily_pnl_truth(date_iso: Optional[str] = None) -> Decimal:
    """PnL diario MT5-truth via truth layer (fonte AUTORITATIVA).

    Wrapper sobre core.vt_truth.get_daily_pnl() que expoe Decimal em R$
    (precisao 0.01) com cache TTL interno de 5s.

    Args:
        date_iso: data de referencia YYYY-MM-DD. Se None, usa "hoje".

    Returns:
        Decimal em R$. Zero se MT5 indisponivel / sem deals no dia.

    Comportamento:
        - FAIL-SAFE: se MT5 falha, retorna Decimal('0.00') (truth layer
          ja eh defensivo — ver core/vt_truth.py).
    """
    try:
        return vt_truth.get_daily_pnl(date_iso=date_iso)
    except Exception as e:
        log(f"[MT5 DAILY PNL ERRO] {e}")
        return Decimal("0.00")


def compute_pnl_drift(date_iso: Optional[str] = None) -> Dict[str, Any]:
    """Calcula drift entre PnL MT5-truth e PnL DB.

    Compara a fonte autoritativa (MT5 history, via truth layer) contra o
    DB local. Drift = |mt5 - db|. Acima de DRIFT_THRESHOLD_REAIS eh
    dessincronizacao real.

    Args:
        date_iso: data de referencia YYYY-MM-DD. Se None, usa "hoje".

    Returns:
        Dict com:
          - mt5_pnl: Decimal em R$ (MT5-truth)
          - db_pnl: Decimal em R$ (DB local)
          - drift: Decimal em R$ (|mt5 - db|)
          - drift_alert: bool (drift > DRIFT_THRESHOLD_REAIS)
          - threshold: Decimal em R$ (limiar usado na comparacao)
          - date_iso: str (data alvo)
          - source: "TRUTH_LAYER" (sempre via vt_truth)
    """
    if date_iso is None:
        date_iso = datetime.now().strftime("%Y-%m-%d")
    mt5_pnl = get_mt5_daily_pnl_truth(date_iso=date_iso)
    db_pnl = get_db_daily_pnl(date_iso=date_iso)
    drift = (mt5_pnl - db_pnl).copy_abs()
    return {
        "mt5_pnl": mt5_pnl,
        "db_pnl": db_pnl,
        "drift": drift,
        "drift_alert": drift > DRIFT_THRESHOLD_REAIS,
        "threshold": DRIFT_THRESHOLD_REAIS,
        "date_iso": date_iso,
        "source": "TRUTH_LAYER",
    }


def format_drift_alert(drift_info: Dict[str, Any]) -> str:
    """Formata alerta Telegram de drift de PnL."""
    mt5 = drift_info["mt5_pnl"]
    db = drift_info["db_pnl"]
    drift = drift_info["drift"]
    threshold = drift_info["threshold"]
    return (
        f"⚠️ *DRIFT PnL DIARIO*: "
        f"MT5=R$ {mt5:+.2f} | DB=R$ {db:+.2f} | "
        f"diff=R$ {drift:.2f} (limite R$ {threshold:.2f})"
    )


# ===== 7. FORMAT OUTPUT =====
def format_orphan(p):
    """Format an orphan position for display."""
    symbol = p.get("symbol", "?")
    direction = "BUY" if p.get("type", 0) == 0 else "SELL"
    volume = p.get("volume", 0)
    pnl = p.get("profit", 0)
    return f"⚠️ ORFAO: {symbol} {direction} {volume} lots | PnL=R${pnl:+.2f}"


def format_ghost(pos):
    """Format a ghost position for display."""
    symbol = pos.get("state_key", "?").split("_")[0]
    direction = pos.get("direction", "?")
    ticket = pos.get("entry_ticket", "?")
    return f"👻 FANTASMA: {symbol} {direction} ticket={ticket} (bot track mas MT5 não tem)"


def format_ok(n_positions, balance, equity):
    """Format OK status."""
    return f"✅ WATCHDOG: OK | {n_positions} posicoes | Equity=R${equity:,.0f}"


# ===== 8. SAVE STATE =====
def save_status(status_data):
    """Save watchdog status to JSON."""
    tmp = STATUS_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(status_data, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, STATUS_FILE)
    except Exception as e:
        log(f"[SAVE ERRO] {e}")
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ===== MAIN =====
def run_watchdog(json_only=False):
    """Run full watchdog check. Returns status dict."""
    start = time.time()
    config = load_config()

    # 1. Query MT5
    mt5_positions, account = get_mt5_positions()

    # 2. Read bot state
    bot_positions = get_bot_positions()

    # 3. Find discrepancies (DB-backed orphan detection)
    orphans, ghosts, sync_fixes = find_discrepancies(mt5_positions, bot_positions, config)

    # 4. Check account
    balance, equity, margin_free, account_issues = check_account(account)

    # 5. Check trade log integrity
    db_issues = check_trade_log(mt5_positions)

    # 6. Check daily PnL drift (Fase 4 — truth layer)
    # MT5-truth eh fonte autoritativa; DB eh comparacao para detectar
    # dessincronizacao entre o que o bot registrou e o que o broker
    # confirma. Drift > R$ 5/dia = alerta.
    drift_info = compute_pnl_drift()
    drift_alert = drift_info["drift_alert"]

    # Build status
    elapsed = time.time() - start
    status_data = {
        "timestamp": datetime.now().isoformat(),
        "elapsed_sec": round(elapsed, 2),
        "mt5_positions": len(mt5_positions),
        "bot_positions": len(bot_positions),
        "orphans": len(orphans),
        "ghosts": len(ghosts),
        "db_issues": len(db_issues),
        "sync_fixes": len(sync_fixes),
        "balance": balance,
        "equity": equity,
        "margin_free": margin_free,
        "account_issues": account_issues,
        # Fase 4: campos de drift PnL (MT5-truth vs DB).
        # mt5_pnl/db_pnl/drift sao Decimal em R$ (serializam como float via
        # default=str no json.dumps). drift_alert eh bool pronto pra UI.
        "mt5_pnl": drift_info["mt5_pnl"],
        "db_pnl": drift_info["db_pnl"],
        "drift": drift_info["drift"],
        "drift_alert": drift_alert,
        "drift_threshold": drift_info["threshold"],
        "drift_date": drift_info["date_iso"],
        "drift_source": drift_info["source"],
        "ok": (
            len(orphans) == 0
            and len(ghosts) == 0
            and len(account_issues) == 0
            and len(db_issues) == 0
            and not drift_alert
        ),
    }

    # 8. Save state
    save_status(status_data)

    # 9. Output + alerts
    has_issues = bool(orphans or ghosts or account_issues or db_issues or drift_alert)

    if not has_issues:
        sync_note = f" | {len(sync_fixes)} state sync fix(es)" if sync_fixes else ""
        msg = format_ok(len(mt5_positions), balance, equity)
        msg = msg.replace("✅ WATCHDOG:", f"✅ WATCHDOG:{sync_note}") if sync_fixes else msg
        print(msg, flush=True)
        if sync_fixes:
            for sf in sync_fixes:
                log(f"[INFO] State file sync: {sf['ticket']} ({sf['symbol']}) "
                    f"DB trade #{sf['db_trade_id']}")
        # Log discreto do drift (sem alerta Telegram — esta abaixo do limite).
        log(
            f"[DRIFT OK] MT5=R${drift_info['mt5_pnl']:+.2f} "
            f"DB=R${drift_info['db_pnl']:+.2f} "
            f"diff=R${drift_info['drift']:.2f} "
            f"(limite R${DRIFT_THRESHOLD_REAIS:.2f})"
        )
    else:
        # Build alert message
        lines = [f"🚨 *WATCHDOG ALERTA* — {datetime.now().strftime('%H:%M:%S')}"]
        lines.append("")

        for p in orphans:
            line = format_orphan(p)
            print(line, flush=True)
            lines.append(line)

        for pos in ghosts:
            line = format_ghost(pos)
            print(line, flush=True)
            lines.append(line)

        for issue in account_issues:
            line = f"💰 {issue}"
            print(line, flush=True)
            lines.append(line)

        for issue in db_issues:
            line = f"📋 {issue['msg']}"
            print(line, flush=True)
            lines.append(line)

        if drift_alert:
            line = format_drift_alert(drift_info)
            print(line, flush=True)
            lines.append(line)

        lines.append("")
        lines.append(f"📊 MT5: {len(mt5_positions)} pos | Bot: {len(bot_positions)} pos | Sync fixes: {len(sync_fixes)}")
        lines.append(f"💰 Balance: R${balance:,.2f} | Equity: R${equity:,.2f}")

        if not json_only:
            notify_telegram("\n".join(lines))

    # Print summary line
    sync_suffix = f" | {len(sync_fixes)} sync fix(es)" if sync_fixes else ""
    drift_suffix = ""
    if drift_alert:
        drift_suffix = f" | DRIFT R${drift_info['drift']:.2f}"
    summary = (
        format_ok(len(mt5_positions), balance, equity)
        if not has_issues
        else (
            f"⚠️ WATCHDOG: {len(orphans)} orfaos, {len(ghosts)} fantasmas"
            f"{sync_suffix}{drift_suffix} | Equity=R${equity:,.0f}"
        )
    )
    print(f"\n{summary}", flush=True)

    return status_data


if __name__ == "__main__":
    json_only = "--json" in sys.argv
    result = run_watchdog(json_only=json_only)
    if json_only:
        print(json.dumps(result, indent=2, default=str))
