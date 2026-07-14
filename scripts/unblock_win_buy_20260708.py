#!/usr/bin/env python3
"""Libera BUY no WIN: win.buy_enabled = true.

Wave: bruno_human_unlock_win_buy (2026-07-08, ter).
Usage: com autotrader pausado.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.vt_config_loader import load_config, save_params


def main() -> int:
    cfg = load_config(force=True)
    win_block = cfg.get("win", {})
    before = win_block.get("buy_enabled", None)
    if before is True:
        print("win.buy_enabled ja esta True, nada a fazer")
        return 0

    save_params("win", {"buy_enabled": True}, updated_by="bruno_human_unlock_win_buy_20260708")

    cfg2 = load_config(force=True)
    after = cfg2.get("win", {}).get("buy_enabled")
    print(f"win.buy_enabled: {before} -> {after}")
    print(f"config v{cfg2['_version']} writer={cfg2['_updated_by']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())