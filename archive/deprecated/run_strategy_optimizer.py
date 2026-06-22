#!/usr/bin/env python3
"""
Full strategy optimizer — tests ALL 25 strategies × ALL 16 pairs.
Finds the best strategy for each SYM_TF and updates vt_config.json.

Usage:
  python3 run_strategy_optimizer.py              # test + update
  python3 run_strategy_optimizer.py --dry-run    # test only, don't update
  python3 run_strategy_optimizer.py --pairs WIN_M15  # test specific pair only
"""

import argparse
import itertools
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from vt_forward_backtest import (
    fetch_bars_for_backtest,
    simulate_forward,
    BAR_COUNT_PER_TF,
    DEFAULT_BAR_COUNT,
)

# ── Symbols and timeframes ──────────────────────────────────────────────────
SYMBOL_ROOTS = ["WIN", "WDO", "BIT", "WSP"]
TIMEFRAMES = ["M5", "M15", "M30", "H1"]

# ── Dynamic strategy discovery — scans strategies/ directory ────────────────
def discover_strategies() -> list[str]:
    """Scan strategies/ directory for all available strategies.
    Reads STRATEGY_NAME from each .py file without importing (fast).
    Returns lowercase names for backtest compatibility.
    """
    strategies = []
    strategies_dir = Path(__file__).parent / "strategies"
    for py_file in sorted(strategies_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        try:
            content = py_file.read_text()
            match = re.search(r'STRATEGY_NAME\s*=\s*["\']([^"\']+)["\']', content)
            if match:
                strategies.append(match.group(1).lower())
        except Exception:
            continue
    return strategies

ALL_STRATEGIES = discover_strategies()
print(f"Discovered {len(ALL_STRATEGIES)} strategies: {ALL_STRATEGIES}")

# ── Strategy-specific param grids (focused, not exhaustive) ─────────────────
SL_ATR_MULTS = [0.8, 1.0, 1.2, 1.5]

PARAM_GRIDS = {
    "adx_trend": {"sl_atr_mult": SL_ATR_MULTS, "adx_threshold": [20, 25]},
    "ema_crossover": {"sl_atr_mult": SL_ATR_MULTS, "ema_fast": [5, 8], "ema_slow": [20, 30]},
    "range_trading": {"sl_atr_mult": SL_ATR_MULTS, "lookback": [10, 20]},
    "rsi_reversion": {"sl_atr_mult": SL_ATR_MULTS, "rsi_period": [7, 14], "rsi_overbought": [65, 70], "rsi_oversold": [30, 35]},
    "bollinger": {"sl_atr_mult": SL_ATR_MULTS, "bb_period": [20, 30], "bb_std": [1.5, 2.0]},
    "macd_momentum": {"sl_atr_mult": SL_ATR_MULTS, "ema_fast": [8, 12], "ema_slow": [21, 26]},
    "pivot_points": {"sl_atr_mult": SL_ATR_MULTS},
    "supertrend": {"sl_atr_mult": SL_ATR_MULTS, "atr_period": [7, 10], "multiplier": [2.0, 3.0]},
    "ema_pullback": {"sl_atr_mult": SL_ATR_MULTS, "ema_fast": [5, 8], "ema_slow": [20, 30], "adx_threshold": [20, 25]},
    "donchian_breakout": {"sl_atr_mult": SL_ATR_MULTS, "period": [15, 20]},
    "vwap": {"sl_atr_mult": SL_ATR_MULTS, "vwap_period": [10, 20]},
    "keltner_channel": {"sl_atr_mult": SL_ATR_MULTS, "ema_period": [20, 30], "atr_multiplier": [1.5, 2.0]},
    "stochastic": {"sl_atr_mult": SL_ATR_MULTS, "k_period": [10, 14], "overbought": [75, 80], "oversold": [20, 25]},
    "strong_trend": {"sl_atr_mult": SL_ATR_MULTS, "ema_fast": [5, 8], "ema_slow": [20, 30], "adx_threshold": [25, 30]},
    "candle_patterns": {"sl_atr_mult": SL_ATR_MULTS},
    "ichimoku": {"sl_atr_mult": SL_ATR_MULTS, "tenkan_period": [7, 9], "kijun_period": [22, 26]},
    "momentum_breakout": {"sl_atr_mult": SL_ATR_MULTS, "lookback": [10, 20], "roc_threshold": [0.5, 1.0]},
    "mean_reversion_zscore": {"sl_atr_mult": SL_ATR_MULTS, "lookback": [20, 50], "z_threshold": [1.5, 2.0]},
    "triple_ema": {"sl_atr_mult": SL_ATR_MULTS, "ema_fast": [5, 8], "ema_mid": [15, 20], "ema_slow": [50, 60]},
    "volatility_breakout": {"sl_atr_mult": SL_ATR_MULTS, "atr_mult": [0.5, 1.0], "lookback": [10, 20]},
    "heikin_ashi": {"sl_atr_mult": SL_ATR_MULTS},
    "fibonacci_retracement": {"sl_atr_mult": SL_ATR_MULTS},
    "divergence_rsi": {"sl_atr_mult": SL_ATR_MULTS, "rsi_period": [14, 21]},
    "smart_ema": {"sl_atr_mult": SL_ATR_MULTS, "ema_fast": [5, 8], "ema_slow": [20, 30]},
    "win_reversion": {"sl_atr_mult": SL_ATR_MULTS, "bb_period": [20, 30], "bb_std": [1.5, 2.0]},
}

# ── Disabled pairs (no data / excluded) ─────────────────────────────────────
DISABLED_PAIRS = {"WIN_M5", "BIT_M5", "WSP_M5", "WDO_M5", "BIT_M30"}


def build_param_combos(grid: dict) -> list[dict]:
    """Build list of param dicts from a grid (cartesian product)."""
    keys = sorted(grid.keys())
    values = [grid[k] for k in keys]
    return [dict(zip(keys, vals)) for vals in itertools.product(*values)]


def fetch_all_bars() -> dict:
    """Fetch bars for all symbol×TF pairs. Returns dict keyed by 'SYM_TF'."""
    bars_cache = {}
    for root in SYMBOL_ROOTS:
        sym = f"{root}$"
        for tf in TIMEFRAMES:
            key = f"{root}_{tf}"
            if key in DISABLED_PAIRS:
                continue
            count = BAR_COUNT_PER_TF.get(tf, DEFAULT_BAR_COUNT)
            print(f"  Fetching {key} ({sym}, {count} bars)...", end="", flush=True)
            bars = fetch_bars_for_backtest(sym, tf, count=count)
            if bars:
                bars_cache[key] = bars
                print(f" {len(bars)} bars")
            else:
                print(" FAILED")
    return bars_cache


def test_all_strategies(bars_cache: dict, target_pairs: list = None, strategies: list = None) -> dict:
    """Test all strategies for all pairs. Returns results dict."""
    results = {}  # (strategy, root, tf) -> {pnl, n_trades, wr, max_dd, best_params}
    total_tests = 0

    if strategies is None:
        strategies = ALL_STRATEGIES

    pairs_to_test = []
    for root in SYMBOL_ROOTS:
        for tf in TIMEFRAMES:
            key = f"{root}_{tf}"
            if key in DISABLED_PAIRS:
                continue
            if target_pairs and key not in target_pairs:
                continue
            pairs_to_test.append((root, tf, key))

    for strat in strategies:
        grid = PARAM_GRIDS.get(strat, {"sl_atr_mult": SL_ATR_MULTS})
        combos = build_param_combos(grid)

        for root, tf, key in pairs_to_test:
            bars = bars_cache.get(key)
            if not bars or len(bars) < 30:
                results[(strat, root, tf)] = {"pnl": 0, "n_trades": 0, "wr": 0, "max_dd": 0, "best_params": {}}
                continue

            best_pnl = -999999
            best_params = {}
            best_metrics = {}

            for params in combos:
                try:
                    r = simulate_forward(root, tf, bars, strat, params)
                except Exception:
                    continue
                total_tests += 1

                pnl = r.get("pnl", 0)
                if pnl > best_pnl:
                    best_pnl = pnl
                    best_params = params
                    best_metrics = r

            results[(strat, root, tf)] = {
                "pnl": round(best_metrics.get("pnl", 0), 2),
                "n_trades": best_metrics.get("n_trades", 0),
                "wr": round(best_metrics.get("wr", 0), 1),
                "max_dd": round(best_metrics.get("max_dd", 0), 2),
                "best_params": best_params,
            }

    print(f"\n  Total simulations: {total_tests}")
    return results


def find_best_per_pair(results: dict, strategies: list = None) -> dict:
    """Find best strategy for each SYM_TF pair."""
    if strategies is None:
        strategies = ALL_STRATEGIES
    best = {}
    for root in SYMBOL_ROOTS:
        for tf in TIMEFRAMES:
            key = f"{root}_{tf}"
            if key in DISABLED_PAIRS:
                continue

            best_strat = None
            best_pnl = -999999
            best_info = {}

            for strat in strategies:
                info = results.get((strat, root, tf), {})
                pnl = info.get("pnl", 0)
                n_trades = info.get("n_trades", 0)

                # Prefer profitable strategies with trades
                if n_trades > 0 and pnl > best_pnl:
                    best_pnl = pnl
                    best_strat = strat
                    best_info = info

            if best_strat:
                best[key] = {
                    "strategy": best_strat,
                    "pnl": best_info.get("pnl", 0),
                    "n_trades": best_info.get("n_trades", 0),
                    "wr": best_info.get("wr", 0),
                    "max_dd": best_info.get("max_dd", 0),
                    "params": best_info.get("best_params", {}),
                }
            else:
                best[key] = {"strategy": "none", "pnl": 0, "n_trades": 0, "wr": 0, "max_dd": 0, "params": {}}

    return best


def load_config() -> dict:
    path = Path(__file__).parent / "vt_config.json"
    with open(path) as f:
        return json.load(f)


def save_config(config: dict):
    path = Path(__file__).parent / "vt_config.json"
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
    print(f"  Config saved to {path}")


def update_config(config: dict, best: dict) -> tuple[dict, list]:
    """Update config with best strategies. Returns (updated_config, changes_log)."""
    changes = []

    for key, info in best.items():
        root, tf = key.split("_")
        new_strat = info["strategy"].upper()
        new_params = info["params"]

        # Current values
        old_strat = config.get("strategy_by_tf", {}).get(key, "?")
        old_params = config.get("params_by_tf", {}).get(key, {})

        if new_strat == "NONE":
            continue

        # Update strategy_by_tf
        if "strategy_by_tf" not in config:
            config["strategy_by_tf"] = {}
        config["strategy_by_tf"][key] = new_strat

        # Update params_by_tf
        if "params_by_tf" not in config:
            config["params_by_tf"] = {}

        # Build params dict with strategy-specific params
        final_params = {}
        for k, v in new_params.items():
            if isinstance(v, float) and v == int(v) and k not in ("sl_atr_mult",):
                final_params[k] = int(v)
            else:
                final_params[k] = v

        config["params_by_tf"][key] = final_params

        # Track changes
        strat_changed = old_strat != new_strat
        params_changed = old_params != final_params

        if strat_changed or params_changed:
            changes.append({
                "key": key,
                "old_strategy": old_strat,
                "new_strategy": new_strat,
                "old_params": old_params,
                "new_params": final_params,
                "pnl": info["pnl"],
                "n_trades": info["n_trades"],
                "wr": info["wr"],
                "strat_changed": strat_changed,
            })

    return config, changes


def print_before_after(changes: list):
    """Print before/after comparison table."""
    print("\n" + "=" * 120)
    print("BEFORE / AFTER COMPARISON")
    print("=" * 120)
    print(f"{'Pair':<10} {'OLD Strategy':<22} {'NEW Strategy':<22} {'PnL':>10} {'Trades':>7} {'WR%':>6} {'Changed':>8}")
    print("-" * 120)

    for c in sorted(changes, key=lambda x: x["pnl"], reverse=True):
        marker = "STRAT" if c["strat_changed"] else "PARAMS"
        print(
            f"{c['key']:<10} {c['old_strategy']:<22} {c['new_strategy']:<22} "
            f"{c['pnl']:>+10.2f} {c['n_trades']:>7} {c['wr']:>6.1f} {marker:>8}"
        )

    strat_changes = [c for c in changes if c["strat_changed"]]
    param_changes = [c for c in changes if not c["strat_changed"]]
    print(f"\n  Strategy swaps: {len(strat_changes)} | Parameter updates: {len(param_changes)} | Total changes: {len(changes)}")


def print_full_results(best: dict, results: dict, strategies: list = None):
    """Print full results table with top 3 strategies per pair."""
    if strategies is None:
        strategies = ALL_STRATEGIES
    print("\n" + "=" * 130)
    print("FULL RESULTS: Best Strategy per Pair (with top 3 alternatives)")
    print("=" * 130)

    for root in SYMBOL_ROOTS:
        for tf in TIMEFRAMES:
            key = f"{root}_{tf}"
            if key in DISABLED_PAIRS:
                print(f"  {key}: (disabled)")
                continue

            # Get all strategies for this pair, sorted by PnL
            strats = []
            for strat in strategies:
                info = results.get((strat, root, tf), {})
                if info.get("n_trades", 0) > 0:
                    strats.append((strat, info))

            strats.sort(key=lambda x: x[1].get("pnl", 0), reverse=True)

            if not strats:
                print(f"  {key}: NO TRADES from any strategy")
                continue

            best_strat, best_info = strats[0]
            print(f"\n  {key}: BEST = {best_strat.upper()} (PnL R${best_info['pnl']:+.2f}, "
                  f"{best_info['n_trades']}t, WR {best_info['wr']}%, DD R${best_info['max_dd']:.0f})")

            # Show top 3 alternatives
            for i, (s, info) in enumerate(strats[1:4], 2):
                print(f"    #{i}: {s.upper():20s} PnL R${info['pnl']:+.2f}, "
                      f"{info['n_trades']}t, WR {info['wr']}%")


def main():
    parser = argparse.ArgumentParser(description="Strategy optimizer for all pairs")
    parser.add_argument("--dry-run", action="store_true", help="Don't update config")
    parser.add_argument("--pairs", nargs="*", help="Specific pairs to test (e.g. WIN_M15 WDO_H1)")
    parser.add_argument("--strategies", nargs="*", help="Specific strategies to test")
    args = parser.parse_args()

    target_pairs = set(args.pairs) if args.pairs else None
    strategies_to_run = args.strategies if args.strategies else ALL_STRATEGIES

    print("=" * 80)
    print("VIBE-TRADING STRATEGY OPTIMIZER")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Strategies: {len(strategies_to_run)}")
    print(f"Symbols: {SYMBOL_ROOTS}")
    print(f"Timeframes: {TIMEFRAMES}")
    if target_pairs:
        print(f"Target pairs: {target_pairs}")
    print("=" * 80)

    # Load current config
    config = load_config()
    original_strategy_by_tf = dict(config.get("strategy_by_tf", {}))
    print(f"\nCurrent config version: {config.get('_version', '?')}")

    # Step 1: Fetch bars
    print("\n[1/4] Fetching bars for all pairs...")
    start = time.time()
    bars_cache = fetch_all_bars()
    print(f"  Fetched {len(bars_cache)} pairs in {time.time() - start:.1f}s")

    # Step 2: Test all strategies
    print("\n[2/4] Testing all strategies...")
    start = time.time()

    strategies_to_run = args.strategies if args.strategies else ALL_STRATEGIES

    results = test_all_strategies(bars_cache, target_pairs, strategies_to_run)
    print(f"  Completed in {time.time() - start:.1f}s")

    # Step 3: Find best per pair
    print("\n[3/4] Finding best strategy per pair...")
    best = find_best_per_pair(results, strategies_to_run)

    # Print full results
    print_full_results(best, results, strategies_to_run)

    # Step 4: Update config
    print("\n[4/4] Updating config...")
    config, changes = update_config(config, best)

    if changes:
        print_before_after(changes)

        if not args.dry_run:
            config["_version"] = config.get("_version", 0) + 1
            config["_updated_at"] = datetime.now().isoformat()
            config["_updated_by"] = "run_strategy_optimizer"
            save_config(config)
            print("\n  Config UPDATED successfully!")
        else:
            print("\n  [DRY-RUN] Config NOT updated")
    else:
        print("\n  No changes needed — all pairs already optimal!")

    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY: Best strategy per pair")
    print("=" * 80)
    for root in SYMBOL_ROOTS:
        for tf in TIMEFRAMES:
            key = f"{root}_{tf}"
            if key in DISABLED_PAIRS:
                continue
            info = best.get(key, {})
            strat = info.get("strategy", "none").upper()
            pnl = info.get("pnl", 0)
            n = info.get("n_trades", 0)
            wr = info.get("wr", 0)
            old_strat = config.get("strategy_by_tf", {}).get(key, "?")
            marker = " <-- CHANGED" if old_strat != strat else ""
            print(f"  {key:10s}: {strat:22s} PnL R${pnl:+.2f} | {n}t | WR {wr}%{marker}")


if __name__ == "__main__":
    main()
