#!/usr/bin/env python3
"""Validate calibrated v2 config against v1 baseline."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from vt_forward_backtest import discover_pairs, run_mini_backtest_pair


def load_config():
    with open("vt_config.json") as f:
        return json.load(f)


def run_test(config, pairs, label, use_v2, v2_overrides=None):
    total_pnl = 0
    total_trades = 0
    max_dd = 0
    profitable = 0
    details = []

    for sym, tf, strategy, params in pairs:
        key = f"{sym}_{tf}"
        result = run_mini_backtest_pair(sym, tf, config, days=7, use_v2_improvements=use_v2, v2_overrides=v2_overrides)
        pnl = result["pnl"]
        n = result["n_trades"]
        dd = result["max_dd"]
        wr = result["wr"]
        total_pnl += pnl
        total_trades += n
        max_dd = max(max_dd, dd)
        if pnl > 0:
            profitable += 1
        details.append((key, pnl, n, wr, dd))

    print(f"\n{'=' * 60}")
    print(f"{label}")
    print(f"{'=' * 60}")
    for key, pnl, n, wr, dd in sorted(details, key=lambda x: -x[1]):
        print(f"  {key:12s} PnL=R${pnl:+9.2f}  trades={n:3d}  WR={wr:4.0f}%  DD=R${dd:8.2f}")
    print(f"{'─' * 60}")
    print(
        f"  TOTAL: PnL=R${total_pnl:+.2f}  trades={total_trades}  maxDD=R${max_dd:.2f}  profitable={profitable}/{len(pairs)}"
    )
    return total_pnl, total_trades, max_dd, profitable


if __name__ == "__main__":
    config = load_config()
    pairs = discover_pairs(config)
    print(f"Testing {len(pairs)} pairs")

    # V1 baseline
    v1_pnl, v1_trades, v1_dd, v1_prof = run_test(config, pairs, "V1 BASELINE (no v2 filters)", use_v2=False)

    # V2 calibrated
    v2_overrides = {
        "entry_filter_enabled": False,
        "trade_scorer_enabled": True,
        "trade_scorer_min_score": 0,
        "exit_manager_enabled": False,
        "regime_filter_enabled": True,
    }
    v2_pnl, v2_trades, v2_dd, v2_prof = run_test(config, pairs, "V2 CALIBRATED", use_v2=True, v2_overrides=v2_overrides)

    # Summary
    print(f"\n{'=' * 60}")
    print(f"COMPARISON SUMMARY")
    print(f"{'=' * 60}")
    print(f"  V1 PnL: R${v1_pnl:+.2f}  trades={v1_trades}  maxDD=R${v1_dd:.2f}  profitable={v1_prof}/{len(pairs)}")
    print(f"  V2 PnL: R${v2_pnl:+.2f}  trades={v2_trades}  maxDD=R${v2_dd:.2f}  profitable={v2_prof}/{len(pairs)}")
    delta = v2_pnl - v1_pnl
    print(f"  DELTA:  R${delta:+.2f}  ({'IMPROVED' if delta > 0 else 'REGRESSED'})")
    dd_delta = v2_dd - v1_dd
    print(f"  DD:     R${dd_delta:+.2f}  ({'BETTER' if dd_delta < 0 else 'WORSE'})")
