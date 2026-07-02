#!/usr/bin/env python3
"""
scripts/dashboard.py
====================
Dashboard CLI (Fase 4.3 — opcional): status consolidado em tempo real.

Mostra: MT5 status (positions, balance, equity), PnL hoje, drift MT5×DB,
autotrader status (PID, uptime), decisões recentes, próximos crons.

Uso:
    python3 scripts/dashboard.py           # uma vez (saída formatada)
    python3 scripts/dashboard.py --once    # idem (explícito)
    python3 scripts/dashboard.py --watch   # refresh a cada 5s (Ctrl+C sai)

Lei 1: stdlib only. Nunca crasha — se MT5/DB indisponível, mostra 'unavailable'.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

_PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT))
sys.path.insert(0, str(_PROJECT / "core"))
sys.path.insert(0, str(_PROJECT / "mt5"))

CRONTAB_FILE = _PROJECT / "crontab.txt"
LOG_PATH = Path("/tmp/vt_autotrader.log")
REFRESH_SEC = 5


def _safe_call(fn, default="--"):
    """Executa fn, retorna default se falhar (nunca crasha o dashboard)."""
    try:
        return fn()
    except Exception:
        return default


def _mt5_snapshot() -> dict:
    """Snapshot MT5 (status + truth PnL). {} se indisponível."""
    snap = {}
    try:
        from mt5.mt5_orchestrator import status as mt5_status
        data = mt5_status()
        if isinstance(data, dict):
            acct = data.get("account") or {}
            snap["positions"] = len(data.get("positions", []))
            snap["balance"] = acct.get("balance")
            snap["equity"] = acct.get("equity")
            snap["free_margin"] = acct.get("free_margin")
            snap["trade_allowed"] = acct.get("trade_allowed")
    except Exception:
        pass
    # PnL hoje via truth layer
    try:
        from core import vt_truth
        snap["pnl_today"] = float(vt_truth.get_daily_pnl())
    except Exception:
        snap["pnl_today"] = None
    return snap


def _autotrader_status() -> dict:
    """PID + uptime + log freshness do autotrader."""
    st = {}
    try:
        r = subprocess.run(["pgrep", "-f", "core/vt_autotrader.py"],
                           capture_output=True, text=True, timeout=5)
        pid = r.stdout.strip().split("\n")[0] if r.stdout.strip() else None
        st["pid"] = pid
        if pid and LOG_PATH.exists():
            age = (time.time() - LOG_PATH.stat().st_mtime) / 60
            st["log_age_min"] = round(age, 1)
            st["log_fresh"] = age < 5
    except Exception:
        st["pid"] = None
    return st


def _decisions_today() -> int:
    """Conta decisões autônomas de hoje via DecisionLogger."""
    try:
        from core.vt_decision import DecisionLogger
        return DecisionLogger().count_today()
    except Exception:
        return 0


def _next_crons() -> list:
    """Próximos crons (parse simples do crontab.txt)."""
    if not CRONTAB_FILE.exists():
        return []
    upcoming = []
    for line in CRONTAB_FILE.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        # extrai nome do script
        for tok in s.split():
            if tok.endswith((".py", ".sh")):
                upcoming.append(Path(tok).name)
                break
    return upcoming


def render() -> str:
    """Renderiza o dashboard como string formatada."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mt5 = _safe_call(_mt5_snapshot, {})
    at = _safe_call(_autotrader_status, {})
    decs = _safe_call(_decisions_today, 0)
    crons = _safe_call(_next_crons, [])

    lines = []
    lines.append("┌" + "─" * 64 + "┐")
    lines.append("│" + f"{'VIBE-TRADING DASHBOARD':^64}" + "│")
    lines.append("│" + f"{now + ' BRT':^64}" + "│")
    lines.append("├" + "─" * 64 + "┤")

    # MT5
    npos = mt5.get("positions", "?") if mt5 else "unavailable"
    bal = mt5.get("balance")
    eq = mt5.get("equity")
    pnl = mt5.get("pnl_today")
    bal_str = f"R$ {bal:,.2f}" if bal else "--"
    eq_str = f"R$ {eq:,.2f}" if eq else "--"
    pnl_str = f"R$ {pnl:+,.2f}" if pnl is not None else "--"
    lines.append(f"│ MT5:        {npos} pos aberta(s) | bal {bal_str} | eq {eq_str}".ljust(65) + "│")
    lines.append(f"│ PnL HOJE:   {pnl_str}".ljust(65) + "│")

    # Autotrader
    pid = at.get("pid")
    if pid:
        age = at.get("log_age_min", "?")
        fresh = "✓" if at.get("log_fresh") else "⚠ STALE"
        lines.append(f"│ Autotrader: PID {pid} (log {age}min {fresh})".ljust(65) + "│")
    else:
        lines.append(f"│ Autotrader: ✗ NÃO RODANDO".ljust(65) + "│")

    # Decisões
    lines.append(f"│ Decisões hoje: {decs}".ljust(65) + "│")

    # Crons
    if crons:
        lines.append(f"│ Crons ativos: {', '.join(crons[:5])}".ljust(65) + "│")

    lines.append("└" + "─" * 64 + "┘")
    return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    watch = "--watch" in argv
    if watch:
        try:
            while True:
                os.system("clear" if os.name == "posix" else "cls")
                print(render())
                time.sleep(REFRESH_SEC)
        except KeyboardInterrupt:
            print("\nSaindo...")
    else:
        print(render())
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
