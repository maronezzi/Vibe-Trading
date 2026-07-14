#!/usr/bin/env python3
"""Remove [1, "SELL"] de blocked_day_directions (terca-feira sem SELL no WIN).

Wave: bruno_human_unlock_tuesday_sell (2026-07-08, ter).
Usage: com autotrader pausado.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.vt_config_loader import load_config, save_full_config

WEEKDAY_TUE = 1
DIRECTION = "SELL"


def main() -> int:
    cfg = load_config(force=True)
    before = list(cfg.get("blocked_day_directions", []))
    after = [pair for pair in before if not (pair[0] == WEEKDAY_TUE and pair[1] == DIRECTION)]

    if before == after:
        print(f"Nada a fazer: {before}")
        return 0

    cfg["blocked_day_directions"] = after
    save_full_config(cfg, updated_by="bruno_human_unlock_tuesday_sell_20260708")

    print(f"blocked_day_directions antes: {before}")
    print(f"blocked_day_directions depois: {after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())