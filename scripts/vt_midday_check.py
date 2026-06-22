#!/usr/bin/env python3
"""
Mid-day health check — runs at 12:00 during market hours.

Checks:
1. PnL status and alerts if approaching loss limiter thresholds
2. Loss limiter status (halt/recovery mode)
3. Per-symbol performance
4. Position health (positions open too long)
5. Regime detection (if available)

Sends Telegram alert if any issues found.
"""

import sys
import json
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

TELEGRAM_TARGET = "telegram:-1004284773048"


def notify(msg: str):
    try:
        from vt_hermes_helper import hermes_send

        hermes_send(TELEGRAM_TARGET, msg)
    except Exception as e:
        print(f"[NOTIFY FAIL] {e}", flush=True)


def load_state():
    try:
        with open("/tmp/vt_autotrader_state.json") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def load_loss_limiter_state():
    try:
        with open("/tmp/vt_loss_limiter_state.json") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def load_config():
    from vt_config_loader import load_config as _load

    return _load()


def check_positions(state: dict) -> list:
    """Check for positions open too long."""
    warnings = []
    positions = state.get("positions", {})
    now = datetime.now()
    for key, pos in positions.items():
        entry_time = pos.get("entry_time")
        if entry_time:
            try:
                if isinstance(entry_time, str):
                    entry_time = datetime.fromisoformat(entry_time)
                open_minutes = (now - entry_time).total_seconds() / 60
                if open_minutes > 60:
                    warnings.append(f"⏱️ {key}: aberta há {open_minutes:.0f}min")
            except (ValueError, TypeError):
                pass
    return warnings


def run():
    now = datetime.now()
    print(f"[MID-DAY CHECK] {now.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

    state = load_state()
    if not state:
        print("[MID-DAY CHECK] Sem state — autotrader pode não estar rodando", flush=True)
        notify("⚠️ MID-DAY: Sem state — autotrader pode não estar rodando!")
        return

    daily_pnl = state.get("daily_pnl", 0)
    trade_count = state.get("daily_trade_count", 0)
    wins = state.get("wins", 0)
    losses = state.get("losses", 0)
    consecutive_losses = state.get("consecutive_losses", {})
    halt_until = state.get("halt_until", {})

    # Loss limiter status
    ll_state = load_loss_limiter_state()
    ll_status = ""
    if ll_state:
        active_tier = ll_state.get("active_tier")
        recovery = ll_state.get("recovery_mode", False)
        if active_tier:
            ll_status = f" | Tier: {active_tier}"
        if recovery:
            ll_status += " | RECOVERY MODE"

    # Win rate
    total = wins + losses
    wr = (wins / total * 100) if total > 0 else 0

    # Build report
    lines = [
        f"📊 MID-DAY CHECK ({now.strftime('%H:%M')})",
        f"",
        f"PnL diário: R$ {daily_pnl:+.2f}",
        f"Trades: {trade_count} | W/L: {wins}/{losses} | WR: {wr:.0f}%{ll_status}",
    ]

    # Alerts
    alerts = []

    # PnL approaching thresholds
    if daily_pnl <= -400:
        alerts.append(f"🔴 PnL R${daily_pnl:.2f} próximo do threshold -R$500!")
    elif daily_pnl <= -600:
        alerts.append(f"🔴 PnL R${daily_pnl:.2f} próximo do threshold -R$750!")

    # Per-symbol consecutive losses
    for sym, count in consecutive_losses.items():
        if count >= 2:
            alerts.append(f"⚠️ {sym}: {count} perdas consecutivas")

    # Active halts
    for sym, halt_time in halt_until.items():
        try:
            ht = datetime.fromisoformat(halt_time) if isinstance(halt_time, str) else halt_time
            if datetime.now() < ht:
                remaining = (ht - datetime.now()).total_seconds() / 60
                alerts.append(f"🛑 {sym}: halt ativo ({remaining:.0f}min restantes)")
        except (ValueError, TypeError):
            pass

    # Position health
    pos_warnings = check_positions(state)
    alerts.extend(pos_warnings)

    if alerts:
        lines.append("")
        lines.append("ALERTAS:")
        lines.extend([f"  {a}" for a in alerts])

    # Always show mid-day report
    report = "\n".join(lines)
    print(report, flush=True)

    # Only notify on Telegram if there are alerts or PnL is notable
    if alerts or daily_pnl < -200 or daily_pnl > 200:
        notify(report)

    print("[MID-DAY CHECK] Concluído", flush=True)


if __name__ == "__main__":
    run()
