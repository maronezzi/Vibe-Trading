#!/usr/bin/env python3
"""
Exhaustive strategy search: test ALL 16 pairs × ALL 28 strategies.
For each pair, find the strategy+params that gives positive PnL over 7 days.
Apply winning config. Disable pairs that can't be profitable.
"""
import json
import sys
import os
import time

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from optimization.vt_forward_backtest import (
    fetch_bars_for_backtest, simulate_forward, BAR_COUNT_PER_TF, DEFAULT_BAR_COUNT
)

# All 28 strategies (file names = lowercase, display names = UPPER)
ALL_STRATEGIES = [
    "ADX_TREND", "BOLLINGER", "CANDLE_PATTERNS", "DIVERGENCE_RSI",
    "DONCHIAN_BREAKOUT", "EMA_CROSSOVER", "EMA_PULLBACK", "ENHANCED_BOLLINGER",
    "ENHANCED_MACD_MOMENTUM", "ENHANCED_RSI_REVERSION", "FIBONACCI_RETRACEMENT",
    "HEIKIN_ASHI", "ICHIMOKU", "KELTNER_CHANNEL", "MACD_MOMENTUM",
    "MEAN_REVERSION_ZSCORE", "MOMENTUM_BREAKOUT", "PIVOT_POINTS",
    "RANGE_TRADING", "RSI_REVERSION", "SMART_EMA", "STRONG_TREND",
    "SUPERTREND", "TRIPLE_EMA", "VOLATILITY_BREAKOUT", "VWAP", "WIN_REVERSION",
]

ALL_SYMBOLS = ["WIN", "BIT", "WSP", "WDO"]
ALL_TIMEFRAMES = ["M5", "M15", "M30", "H1"]

def merge_params_by_tf_into_config(config):
    """Create a modified config where params_by_tf values are injected
    into config[sym.lower()][tf] so _resolve_pair_params picks them up."""
    pbt = config.get("params_by_tf", {})
    modified = json.loads(json.dumps(config))  # deep copy
    for pair_key, params in pbt.items():
        parts = pair_key.split("_", 1)
        if len(parts) == 2:
            sym, tf = parts
            sym_lower = sym.lower()
            if sym_lower not in modified:
                modified[sym_lower] = {}
            if tf not in modified[sym_lower]:
                modified[sym_lower][tf] = {}
            # Merge params_by_tf into the tf-specific section
            # (params_by_tf takes priority)
            for k, v in params.items():
                modified[sym_lower][tf][k] = v
    return modified


def test_all_strategies_for_pair(sym, tf, bars, config):
    """Test all 28 strategies for a single (sym, tf) pair.
    Returns sorted list of (strategy_name, result_dict) by PnL descending.
    """
    results = []
    for strat_name in ALL_STRATEGIES:
        try:
            result = simulate_forward(sym, tf, bars, strat_name, {}, config=config)
        except Exception as e:
            result = {
                "pnl": 0.0, "n_trades": 0, "wr": 0.0, "max_dd": 0.0,
                "decision": f"error:{type(e).__name__}",
            }
        results.append((strat_name, result))

    # Sort by PnL descending
    results.sort(key=lambda x: x[1]["pnl"], reverse=True)
    return results


def main():
    start_time = time.time()

    # Load config
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vt_config.json")
    with open(config_path) as f:
        config = json.load(f)

    # Merge params_by_tf into config for accurate testing
    test_config = merge_params_by_tf_into_config(config)

    print("=" * 100)
    print("EXHAUSTIVE STRATEGY SEARCH: 16 pairs × 28 strategies")
    print("=" * 100)

    # Phase 1: Fetch bars for all 16 pairs (one Wine call per pair)
    print("\n📡 Phase 1: Fetching MT5 bars for all 16 pairs...")
    bars_cache = {}
    for sym in ALL_SYMBOLS:
        for tf in ALL_TIMEFRAMES:
            pair_key = f"{sym}_{tf}"
            full_symbol = f"{sym}$"
            bar_count = BAR_COUNT_PER_TF.get(tf, DEFAULT_BAR_COUNT)
            print(f"  Fetching {pair_key} ({full_symbol}, {tf}, {bar_count} bars)...", end=" ", flush=True)
            bars = fetch_bars_for_backtest(full_symbol, tf, count=bar_count)
            bars_cache[pair_key] = bars
            print(f"{'✅ ' + str(len(bars)) + ' bars' if bars else '❌ NO DATA'}")

    # Phase 2: Test all 28 strategies for each pair
    print("\n🔬 Phase 2: Testing all 28 strategies per pair...")
    all_results = {}  # {pair_key: [(strat, result), ...]}
    pair_count = 0
    for sym in ALL_SYMBOLS:
        for tf in ALL_TIMEFRAMES:
            pair_key = f"{sym}_{tf}"
            pair_count += 1
            bars = bars_cache[pair_key]
            if not bars:
                print(f"\n  [{pair_count}/16] {pair_key}: ❌ No bars available, skipping")
                all_results[pair_key] = []
                continue

            print(f"\n  [{pair_count}/16] {pair_key}: testing {len(ALL_STRATEGIES)} strategies...", flush=True)
            results = test_all_strategies_for_pair(sym, tf, bars, test_config)
            all_results[pair_key] = results

            # Show top 3
            for i, (strat, res) in enumerate(results[:3]):
                emoji = "🟢" if res["pnl"] > 0 else "🔴"
                print(f"    #{i+1}: {strat:30s} PnL={res['pnl']:>10.2f}R  trades={res['n_trades']:>3d}  WR={res['wr']:>5.1f}%  {emoji}")

    # Phase 3: Select best strategy per pair
    print("\n" + "=" * 100)
    print("📊 Phase 3: BEST STRATEGY PER PAIR")
    print("=" * 100)

    best_config = {}  # {pair_key: {strategy, pnl, n_trades, wr, decision}}
    disabled_pairs = []

    print(f"\n{'Pair':<12} {'Best Strategy':<30} {'PnL':>10} {'Trades':>7} {'WR':>7} {'Status':<10}")
    print("-" * 80)

    for sym in ALL_SYMBOLS:
        for tf in ALL_TIMEFRAMES:
            pair_key = f"{sym}_{tf}"
            results = all_results.get(pair_key, [])
            if not results:
                print(f"{pair_key:<12} {'(no bars)':<30} {'N/A':>10} {'N/A':>7} {'N/A':>7} DISABLED")
                disabled_pairs.append((pair_key, "No MT5 data available"))
                continue

            # Find best strategy with PnL > 0 AND trades >= 1
            best_strat = None
            best_result = None
            for strat, res in results:
                if res["pnl"] > 0 and res["n_trades"] >= 1:
                    best_strat = strat
                    best_result = res
                    break

            if best_strat:
                emoji = "✅"
                print(f"{pair_key:<12} {best_strat:<30} {best_result['pnl']:>10.2f} {best_result['n_trades']:>7d} {best_result['wr']:>6.1f}% {emoji}")
                best_config[pair_key] = {
                    "strategy": best_strat,
                    "pnl": best_result["pnl"],
                    "n_trades": best_result["n_trades"],
                    "wr": best_result["wr"],
                    "max_dd": best_result["max_dd"],
                }
            else:
                # No profitable strategy found
                # Show the least-bad one
                least_bad_strat = results[0][0]  # already sorted by PnL desc
                least_bad = results[0][1]
                emoji = "❌"
                print(f"{pair_key:<12} {least_bad_strat:<30} {least_bad['pnl']:>10.2f} {least_bad['n_trades']:>7d} {least_bad['wr']:>6.1f}% {emoji}")
                disabled_pairs.append((pair_key, f"No profitable strategy (best: {least_bad_strat} PnL={least_bad['pnl']:.2f}R)"))

    # Summary
    active_count = len(best_config)
    disabled_count = len(disabled_pairs)
    total_pnl = sum(v["pnl"] for v in best_config.values())
    total_trades = sum(v["n_trades"] for v in best_config.values())

    print(f"\n{'=' * 80}")
    print(f"SUMMARY: {active_count} active / {disabled_count} disabled / 16 total")
    print(f"Total PnL: {total_pnl:.2f}R")
    print(f"Total trades: {total_trades}")

    if disabled_pairs:
        print(f"\nDisabled pairs:")
        for pair_key, reason in disabled_pairs:
            print(f"  {pair_key}: {reason}")

    # Phase 4: Apply winning config
    print(f"\n{'=' * 80}")
    print("🔧 Phase 4: Applying winning config...")

    # Update strategy_by_tf
    strategy_by_tf = config.get("strategy_by_tf", {})
    for pair_key, info in best_config.items():
        strategy_by_tf[pair_key] = info["strategy"]
    config["strategy_by_tf"] = strategy_by_tf

    # Update disabled_timeframes
    config["disabled_timeframes"] = [pair_key for pair_key, _ in disabled_pairs]

    # Save
    config["_version"] = config.get("_version", 0) + 1
    config["_updated_at"] = __import__("datetime").datetime.now().isoformat()
    config["_updated_by"] = "exhaustive_strategy_search"

    # Build notes
    notes_parts = []
    for pair_key, info in best_config.items():
        notes_parts.append(f"{pair_key}→{info['strategy']}({info['pnl']:.0f}R/{info['n_trades']}t/{info['wr']:.0f}%WR)")
    if disabled_pairs:
        notes_parts.append(f"DISABLED: {', '.join(p for p, _ in disabled_pairs)}")
    config["_notes"] = f"Exhaustive 16×28 strategy search. Active: {active_count}, Disabled: {disabled_count}, Total PnL: {total_pnl:.0f}R. " + "; ".join(notes_parts)

    with open(config_path, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    print(f"✅ Config saved as v{config['_version']}")

    # Print full results table
    print(f"\n{'=' * 100}")
    print("📋 FULL RESULTS TABLE (Top 5 strategies per pair)")
    print("=" * 100)
    for sym in ALL_SYMBOLS:
        for tf in ALL_TIMEFRAMES:
            pair_key = f"{sym}_{tf}"
            results = all_results.get(pair_key, [])
            if not results:
                continue
            print(f"\n  {pair_key}:")
            for i, (strat, res) in enumerate(results[:5]):
                marker = " ← SELECTED" if pair_key in best_config and best_config[pair_key]["strategy"] == strat else ""
                emoji = "🟢" if res["pnl"] > 0 and res["n_trades"] >= 1 else "🔴"
                print(f"    {i+1}. {strat:<30s} PnL={res['pnl']:>10.2f}R  trades={res['n_trades']:>3d}  WR={res['wr']:>5.1f}%  DD={res['max_dd']:>8.2f}R  {emoji}{marker}")

    elapsed = time.time() - start_time
    print(f"\n⏱️  Total time: {elapsed:.0f}s ({elapsed/60:.1f}min)")
    print(f"✅ DONE: {active_count}/16 pairs active, total PnL = {total_pnl:.2f}R")


if __name__ == "__main__":
    main()
