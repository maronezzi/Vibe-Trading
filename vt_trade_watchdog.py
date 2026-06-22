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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mt5_orchestrator import status as mt5_status, _run_wine, EXECUTOR_WIN
from vt_config_loader import load_config
from vt_trade_log import get_multiplier

# ===== CONFIG =====
STATE_FILE = "/tmp/vt_autotrader_state.json"
STATUS_FILE = "/tmp/vt_watchdog_status.json"
DB_PATH = Path(__file__).parent / "vt_trades.db"
TELEGRAM_TARGET = "telegram:-1004284773048"
MAGIC = 555501


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def notify_telegram(msg):
    try:
        from vt_hermes_helper import hermes_send

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
            "FROM trades WHERE exit_time IS NULL OR exit_time = ''"
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
            "SELECT id, symbol, direction, entry_ticket, entry_price FROM trades WHERE exit_time IS NULL"
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


# ===== 6. FORMAT OUTPUT =====
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


# ===== 7. SAVE STATE =====
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
        "ok": len(orphans) == 0 and len(ghosts) == 0 and len(account_issues) == 0 and len(db_issues) == 0,
    }

    # 7. Save state
    save_status(status_data)

    # 8. Output + alerts
    has_issues = orphans or ghosts or account_issues or db_issues

    if not has_issues:
        sync_note = f" | {len(sync_fixes)} state sync fix(es)" if sync_fixes else ""
        msg = format_ok(len(mt5_positions), balance, equity)
        msg = msg.replace("✅ WATCHDOG:", f"✅ WATCHDOG:{sync_note}") if sync_fixes else msg
        print(msg, flush=True)
        if sync_fixes:
            for sf in sync_fixes:
                log(f"[INFO] State file sync: {sf['ticket']} ({sf['symbol']}) "
                    f"DB trade #{sf['db_trade_id']}")
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

        lines.append("")
        lines.append(f"📊 MT5: {len(mt5_positions)} pos | Bot: {len(bot_positions)} pos | Sync fixes: {len(sync_fixes)}")
        lines.append(f"💰 Balance: R${balance:,.2f} | Equity: R${equity:,.2f}")

        if not json_only:
            notify_telegram("\n".join(lines))

    # Print summary line
    sync_suffix = f" | {len(sync_fixes)} sync fix(es)" if sync_fixes else ""
    summary = (
        format_ok(len(mt5_positions), balance, equity)
        if not has_issues
        else f"⚠️ WATCHDOG: {len(orphans)} orfaos, {len(ghosts)} fantasmas{sync_suffix} | Equity=R${equity:,.0f}"
    )
    print(f"\n{summary}", flush=True)

    return status_data


if __name__ == "__main__":
    json_only = "--json" in sys.argv
    result = run_watchdog(json_only=json_only)
    if json_only:
        print(json.dumps(result, indent=2, default=str))
