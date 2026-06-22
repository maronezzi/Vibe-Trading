#!/usr/bin/env python3
"""
vt_calibrate_v2.py — Grid search calibration for v2 filter thresholds.

Tests all combinations of v2 filter settings to find the optimal configuration
that maximizes PnL while maintaining reasonable trade count and drawdown.

Usage:
    python vt_calibrate_v2.py
"""

import json
import itertools
import time
import sys
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent))

from vt_forward_backtest import (
    discover_pairs,
    run_mini_backtest_pair,
    fetch_bars_cached,
    clear_bar_cache,
    BAR_COUNT_PER_TF,
    DEFAULT_BAR_COUNT,
)


def load_config():
    with open("vt_config.json") as f:
        return json.load(f)


def run_baseline_v1(config, pairs):
    """Run v1 baseline: all v2 filters disabled."""
    print("\n" + "=" * 70)
    print("BASELINE V1: All v2 filters DISABLED")
    print("=" * 70)

    total_pnl = 0
    total_trades = 0
    total_dd = 0
    profitable = 0
    results = {}

    for sym, tf, strategy, params in pairs:
        key = f"{sym}_{tf}"
        bar_count = BAR_COUNT_PER_TF.get(tf, DEFAULT_BAR_COUNT)
        bars = fetch_bars_cached(f"{sym}$", tf, count=bar_count)
        if not bars:
            print(f"  {key}: NO DATA")
            continue

        result = run_mini_backtest_pair(sym, tf, config, days=7, use_v2_improvements=False)
        results[key] = result
        pnl = result["pnl"]
        n = result["n_trades"]
        dd = result["max_dd"]
        wr = result["wr"]
        total_pnl += pnl
        total_trades += n
        total_dd = max(total_dd, dd)
        if pnl > 0:
            profitable += 1
        print(f"  {key}: PnL=R${pnl:+.2f} trades={n} WR={wr:.0f}% DD=R${dd:.2f} [{result['decision']}]")

    print(
        f"\n  TOTAL: PnL=R${total_pnl:+.2f} trades={total_trades} maxDD=R${total_dd:.2f} profitable={profitable}/{len(pairs)}"
    )
    return total_pnl, total_trades, total_dd, profitable, results


def run_single_test(config, pairs, v2_overrides):
    """Run a single test with given v2 overrides. Returns aggregate metrics."""
    total_pnl = 0
    total_trades = 0
    max_dd = 0
    profitable = 0

    for sym, tf, strategy, params in pairs:
        key = f"{sym}_{tf}"
        bar_count = BAR_COUNT_PER_TF.get(tf, DEFAULT_BAR_COUNT)
        bars = fetch_bars_cached(f"{sym}$", tf, count=bar_count)
        if not bars:
            continue

        result = run_mini_backtest_pair(sym, tf, config, days=7, use_v2_improvements=True, v2_overrides=v2_overrides)
        total_pnl += result["pnl"]
        total_trades += result["n_trades"]
        max_dd = max(max_dd, result["max_dd"])
        if result["pnl"] > 0:
            profitable += 1

    return {
        "pnl": round(total_pnl, 2),
        "trades": total_trades,
        "max_dd": round(max_dd, 2),
        "profitable_pairs": profitable,
    }


def grid_search(config, pairs, baseline_pnl):
    """Run grid search over all v2 threshold combinations."""

    # Define search space
    entry_filter_enabled_opts = [True, False]
    entry_filter_min_conf_opts = [1, 2]
    trade_scorer_enabled_opts = [True, False]
    trade_scorer_min_score_opts = [0, 30, 40, 50]
    exit_manager_enabled_opts = [True, False]
    regime_filter_enabled_opts = [True, False]

    # Build all combinations
    combos = list(
        itertools.product(
            entry_filter_enabled_opts,
            entry_filter_min_conf_opts,
            trade_scorer_enabled_opts,
            trade_scorer_min_score_opts,
            exit_manager_enabled_opts,
            regime_filter_enabled_opts,
        )
    )

    print(f"\n{'=' * 70}")
    print(f"GRID SEARCH: {len(combos)} combinations x {len(pairs)} pairs")
    print(f"{'=' * 70}")

    best_result = None
    best_config = None
    all_results = []
    start_time = time.time()

    for idx, (ef_enabled, ef_min_conf, ts_enabled, ts_min_score, em_enabled, rf_enabled) in enumerate(combos):
        # Skip redundant combos
        if not ef_enabled and ef_min_conf != 2:
            continue  # min_conf doesn't matter when disabled

        v2_overrides = {
            "entry_filter_enabled": ef_enabled,
            "entry_filter_min_confirmations": ef_min_conf,
            "trade_scorer_enabled": ts_enabled,
            "trade_scorer_min_score": ts_min_score if ts_enabled else 0,
            "exit_manager_enabled": em_enabled,
            "regime_filter_enabled": rf_enabled,
        }

        result = run_single_test(config, pairs, v2_overrides)
        result["config"] = v2_overrides
        all_results.append(result)

        # Score: PnL primary, penalize if worse than baseline
        pnl_diff = result["pnl"] - baseline_pnl
        is_best = best_result is None or result["pnl"] > best_result["pnl"]
        if is_best:
            best_result = result
            best_config = v2_overrides

        elapsed = time.time() - start_time
        pct = (idx + 1) / len(combos) * 100
        eta = (elapsed / (idx + 1)) * (len(combos) - idx - 1) if idx > 0 else 0
        marker = " *** BEST" if is_best else ""

        if idx % 10 == 0 or is_best:
            print(
                f"  [{idx + 1}/{len(combos)} {pct:.0f}%] "
                f"PnL=R${result['pnl']:+.2f} trades={result['trades']} "
                f"DD=R${result['max_dd']:.2f} prof={result['profitable_pairs']}/{len(pairs)} "
                f"ETA={eta:.0f}s{marker}"
            )

    elapsed = time.time() - start_time
    print(f"\nGrid search completed in {elapsed:.1f}s")

    # Sort by PnL descending
    all_results.sort(key=lambda x: x["pnl"], reverse=True)

    return best_result, best_config, all_results


def main():
    print("=" * 70)
    print("V2 CALIBRATION — Grid Search")
    print("=" * 70)

    config = load_config()
    pairs = discover_pairs(config)
    print(f"Discovered {len(pairs)} pairs:")
    for sym, tf, strat, params in pairs:
        print(f"  {sym}_{tf}: {strat}")

    # Pre-warm bar cache
    print("\nPre-warming bar cache...")
    for sym, tf, strat, params in pairs:
        bar_count = BAR_COUNT_PER_TF.get(tf, DEFAULT_BAR_COUNT)
        bars = fetch_bars_cached(f"{sym}$", tf, count=bar_count)
        print(f"  {sym}_{tf}: {len(bars) if bars else 0} bars cached")
    print("Cache ready.")

    # Step 1: Baseline v1
    baseline_pnl, baseline_trades, baseline_dd, baseline_prof, baseline_results = run_baseline_v1(config, pairs)

    # Step 2: Grid search
    best_result, best_config, all_results = grid_search(config, pairs, baseline_pnl)

    # Step 3: Report
    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    print(
        f"\nV1 Baseline:  PnL=R${baseline_pnl:+.2f}  trades={baseline_trades}  DD=R${baseline_dd:.2f}  profitable={baseline_prof}/{len(pairs)}"
    )
    print(
        f"V2 Best:      PnL=R${best_result['pnl']:+.2f}  trades={best_result['trades']}  DD=R${best_result['max_dd']:.2f}  profitable={best_result['profitable_pairs']}/{len(pairs)}"
    )
    print(f"\nBest config:")
    for k, v in best_config.items():
        print(f"  {k}: {v}")

    pnl_diff = best_result["pnl"] - baseline_pnl
    print(f"\nPnL difference: R${pnl_diff:+.2f} ({'IMPROVED' if pnl_diff > 0 else 'DEGRADED'})")

    # Top 10
    print(f"\nTop 10 configurations:")
    print(f"{'Rank':<5} {'PnL':>10} {'Trades':>7} {'DD':>8} {'Prof':>5} {'Config'}")
    print("-" * 80)
    for i, r in enumerate(all_results[:10]):
        cfg = r["config"]
        cfg_str = (
            f"EF={'ON' if cfg['entry_filter_enabled'] else 'OFF'}/{cfg['entry_filter_min_confirmations']} "
            f"TS={'ON' if cfg['trade_scorer_enabled'] else 'OFF'}/{cfg['trade_scorer_min_score']} "
            f"EM={'ON' if cfg['exit_manager_enabled'] else 'OFF'} "
            f"RF={'ON' if cfg['regime_filter_enabled'] else 'OFF'}"
        )
        print(
            f"  {i + 1:<3} R${r['pnl']:>+9.2f} {r['trades']:>6} R${r['max_dd']:>7.2f} {r['profitable_pairs']:>3}/{len(pairs)} {cfg_str}"
        )

    # Save results
    output = {
        "baseline": {
            "pnl": baseline_pnl,
            "trades": baseline_trades,
            "max_dd": baseline_dd,
            "profitable_pairs": baseline_prof,
        },
        "best": {
            "pnl": best_result["pnl"],
            "trades": best_result["trades"],
            "max_dd": best_result["max_dd"],
            "profitable_pairs": best_result["profitable_pairs"],
            "config": best_config,
        },
        "top_10": [
            {
                "pnl": r["pnl"],
                "trades": r["trades"],
                "max_dd": r["max_dd"],
                "profitable_pairs": r["profitable_pairs"],
                "config": r["config"],
            }
            for r in all_results[:10]
        ],
    }
    with open("vt_calibration_results.json", "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults saved to vt_calibration_results.json")


if __name__ == "__main__":
    main()
