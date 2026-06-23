#!/usr/bin/env python3
"""
Exhaustive strategy search with parameter optimization:
test ALL 16 pairs × ALL 28 strategies × top param combos per strategy.
For each pair, find the strategy+params that gives positive PnL over 7 days.
Apply winning config. Disable pairs that can't be profitable.

v2: Adds parameter optimization via strategic grid search (~20-30 combos/strategy).
"""
import itertools
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

# ─── Parameter Grid for Optimization ─────────────────────────────────────────
# Strategic sampling: 3-5 values per param, chosen from PARAM_BOUNDS ranges.
# Each strategy maps to its key parameters. Total combos per strategy: ~10-30.

# Universal params (applied by simulate_forward for all strategies)
UNIVERSAL_PARAMS = {
    "sl_atr_mult":       [1.0, 1.5, 2.0, 2.5],
    "cooldown_seconds":  [120, 300, 600],
}

# Strategy-specific param grids
STRATEGY_PARAM_GRIDS = {
    "RSI_REVERSION": {
        "rsi_period":      [7, 10, 14, 21],
        "rsi_overbought":  [70, 75, 80],
        "rsi_oversold":    [20, 25, 30],
    },
    "ENHANCED_RSI_REVERSION": {
        "rsi_period":      [7, 10, 14, 21],
        "rsi_overbought":  [70, 75, 80],
        "rsi_oversold":    [20, 25, 30],
    },
    "MACD_MOMENTUM": {
        "macd_fast":       [8, 10, 12],
        "macd_slow":       [20, 24, 26],
        "macd_signal":     [7, 9, 11],
    },
    "ENHANCED_MACD_MOMENTUM": {
        "macd_fast":       [8, 10, 12],
        "macd_slow":       [20, 24, 26],
        "macd_signal":     [7, 9, 11],
    },
    "BOLLINGER": {
        "bb_period":       [14, 20, 30],
        "bb_std":          [1.5, 2.0, 2.5, 3.0],
    },
    "ENHANCED_BOLLINGER": {
        "bb_period":       [14, 20, 30],
        "bb_std":          [1.5, 2.0, 2.5, 3.0],
    },
    "STRONG_TREND": {
        "adx_threshold":   [15, 20, 25, 30],
        "adx_period":      [10, 14, 20],
    },
    "ADX_TREND": {
        "adx_threshold":   [15, 20, 25, 30],
        "adx_period":      [10, 14, 20],
    },
    "EMA_CROSSOVER": {
        "ema_fast":        [8, 10, 12],
        "ema_slow":        [20, 26, 30],
    },
    "EMA_PULLBACK": {
        "ema_fast":        [8, 10, 12],
        "ema_slow":        [20, 26, 30],
    },
    "TRIPLE_EMA": {
        "ema_fast":        [8, 10, 12],
        "ema_slow":        [20, 26, 30],
    },
    "VWAP": {
        "vwap_period":         [20, 30, 40],
        "vwap_buy_threshold":  [1.005, 1.010, 1.015],
        "vwap_sell_threshold": [0.985, 0.990, 0.995],
    },
    "KELTNER_CHANNEL": {
        "keltner_period":     [14, 20, 30],
        "keltner_atr_mult":   [1.5, 2.0, 2.5],
    },
    "DONCHIAN_BREAKOUT": {
        "donchian_period":    [14, 20, 30],
    },
    "PIVOT_POINTS": {
        "pivot_timeframe":    ["H1", "H4", "D1"],
    },
    "DIVERGENCE_RSI": {
        "rsi_period":      [10, 14, 21],
        "rsi_overbought":  [70, 75, 80],
        "rsi_oversold":    [20, 25, 30],
    },
    "MOMENTUM_BREAKOUT": {
        "adx_threshold":   [15, 20, 25],
        "adx_period":      [10, 14, 20],
    },
    "MEAN_REVERSION_ZSCORE": {
        "bb_period":       [14, 20, 30],
        "bb_std":          [1.5, 2.0, 2.5],
    },
    "FIBONACCI_RETRACEMENT": {
        "pullback_pct":    [0.05, 0.10, 0.15, 0.20],
    },
    "VOLATILITY_BREAKOUT": {
        "adx_threshold":   [15, 20, 25],
        "adx_period":      [10, 14, 20],
    },
    "RANGE_TRADING": {
        "rsi_overbought":  [70, 75, 80],
        "rsi_oversold":    [20, 25, 30],
        "rsi_period":      [10, 14, 21],
    },
    "WIN_REVERSION": {
        "rsi_period":      [10, 14, 21],
        "rsi_overbought":  [70, 75, 80],
        "rsi_oversold":    [20, 25, 30],
    },
    "HEIKIN_ASHI": {
        "ema_fast":        [8, 10, 12],
        "ema_slow":        [20, 26],
    },
    "SMART_EMA": {
        "ema_fast":        [8, 10, 12],
        "ema_slow":        [20, 26],
    },
    "SUPERTREND": {
        "adx_threshold":   [15, 20, 25],
        "adx_period":      [10, 14],
    },
    "ICHIMOKU": {
        "adx_threshold":   [15, 20, 25],
    },
    "CANDLE_PATTERNS": {
        "adx_threshold":   [15, 20, 25],
    },
}

# Max combos to test per strategy (cap to avoid explosion)
MAX_COMBOS_PER_STRATEGY = 30


def _generate_param_combos(strat_name: str) -> list:
    """Generate param combos for a strategy: universal + strategy-specific.
    Returns list of dicts. Caps at MAX_COMBOS_PER_STRATEGY.
    """
    # Start with strategy-specific grid (or empty if none defined)
    strat_grid = STRATEGY_PARAM_GRIDS.get(strat_name, {})

    # Build combined grid: universal params + strategy-specific
    combined = {}
    combined.update(UNIVERSAL_PARAMS)
    combined.update(strat_grid)

    if not combined:
        return [{}]  # just defaults

    # Generate cartesian product
    keys = sorted(combined.keys())
    values_lists = [combined[k] for k in keys]
    all_combos = []
    for combo in itertools.product(*values_lists):
        all_combos.append(dict(zip(keys, combo)))

    # If too many, subsample evenly
    if len(all_combos) > MAX_COMBOS_PER_STRATEGY:
        step = len(all_combos) / MAX_COMBOS_PER_STRATEGY
        sampled = [all_combos[int(i * step)] for i in range(MAX_COMBOS_PER_STRATEGY)]
        # Always include the first combo (all-lowest) and last (all-highest)
        if all_combos[0] not in sampled:
            sampled.insert(0, all_combos[0])
        if all_combos[-1] not in sampled:
            sampled.append(all_combos[-1])
        all_combos = sampled[:MAX_COMBOS_PER_STRATEGY]

    return all_combos


def merge_params_by_tf_into_config(config):
    """Create a modified config where params_by_tf values are injected
    into config[sym.lower()][tf] so _resolve_pair_params picks them up.
    """
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


def test_strategy_with_optimization(sym, tf, bars, strat_name, config):
    """Test a single strategy with param optimization.

    Tests multiple param combinations (grid search) and returns the best result.

    Returns: (strategy_name, best_result_dict, best_params_dict)
    """
    param_combos = _generate_param_combos(strat_name)

    best_result = None
    best_params = {}
    best_pnl = -float("inf")

    for params in param_combos:
        try:
            result = simulate_forward(sym, tf, bars, strat_name, params, config=config)
        except Exception as e:
            result = {
                "pnl": 0.0, "n_trades": 0, "wr": 0.0, "max_dd": 0.0,
                "decision": f"error:{type(e).__name__}",
            }
        if result["pnl"] > best_pnl:
            best_pnl = result["pnl"]
            best_result = result
            best_params = params

    return strat_name, best_result, best_params


def test_all_strategies_for_pair(sym, tf, bars, config):
    """Test all 28 strategies for a single (sym, tf) pair.
    For each strategy, tests multiple param combinations via grid search.

    Returns sorted list of (strategy_name, result_dict, best_params) by PnL descending.
    """
    results = []
    for strat_name in ALL_STRATEGIES:
        strat_name, result, best_params = test_strategy_with_optimization(
            sym, tf, bars, strat_name, config
        )
        results.append((strat_name, result, best_params))

    # Sort by PnL descending
    results.sort(key=lambda x: x[1]["pnl"], reverse=True)
    return results


def _format_params(params: dict) -> str:
    """Format params dict for display: 'key=val key=val ...'"""
    if not params:
        return "defaults"
    return " ".join(f"{k}={v}" for k, v in sorted(params.items()))


def main():
    start_time = time.time()

    # Load config
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vt_config.json")
    with open(config_path) as f:
        config = json.load(f)

    # Merge params_by_tf into config for accurate testing
    test_config = merge_params_by_tf_into_config(config)

    # Count total param combos
    total_combos = 0
    for strat in ALL_STRATEGIES:
        total_combos += len(_generate_param_combos(strat))

    print("=" * 100)
    print(f"EXHAUSTIVE STRATEGY SEARCH WITH PARAMETER OPTIMIZATION")
    print(f"16 pairs × {len(ALL_STRATEGIES)} strategies × ~avg {total_combos // len(ALL_STRATEGIES)} param combos")
    print(f"Total simulations per pair: ~{total_combos}")
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

    # Phase 2: Test all strategies with param optimization for each pair
    print(f"\n🔬 Phase 2: Testing all {len(ALL_STRATEGIES)} strategies with param optimization per pair...")
    all_results = {}  # {pair_key: [(strat, result, best_params), ...]}
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

            print(f"\n  [{pair_count}/16] {pair_key}: testing {len(ALL_STRATEGIES)} strategies with param optimization...", flush=True)
            results = test_all_strategies_for_pair(sym, tf, bars, test_config)
            all_results[pair_key] = results

            # Show top 3
            for i, (strat, res, params) in enumerate(results[:3]):
                emoji = "🟢" if res["pnl"] > 0 else "🔴"
                pstr = _format_params(params)
                print(f"    #{i+1}: {strat:30s} PnL={res['pnl']:>10.2f}R  trades={res['n_trades']:>3d}  WR={res['wr']:>5.1f}%  {emoji}")
                print(f"          params: {pstr}")

    # Phase 3: Select best strategy per pair
    print("\n" + "=" * 100)
    print("📊 Phase 3: BEST STRATEGY + PARAMS PER PAIR")
    print("=" * 100)

    best_config = {}  # {pair_key: {strategy, pnl, n_trades, wr, decision, params}}
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
            best_params = {}
            for strat, res, params in results:
                if res["pnl"] > 0 and res["n_trades"] >= 1:
                    best_strat = strat
                    best_result = res
                    best_params = params
                    break

            if best_strat:
                emoji = "✅"
                print(f"{pair_key:<12} {best_strat:<30} {best_result['pnl']:>10.2f} {best_result['n_trades']:>7d} {best_result['wr']:>6.1f}% {emoji}")
                pstr = _format_params(best_params)
                print(f"{'':12} params: {pstr}")
                best_config[pair_key] = {
                    "strategy": best_strat,
                    "pnl": best_result["pnl"],
                    "n_trades": best_result["n_trades"],
                    "wr": best_result["wr"],
                    "max_dd": best_result["max_dd"],
                    "params": best_params,
                }
            else:
                # No profitable strategy found
                # Show the least-bad one
                least_bad_strat = results[0][0]  # already sorted by PnL desc
                least_bad = results[0][1]
                least_bad_params = results[0][2]
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

    # Update params_by_tf with optimized params
    params_by_tf = config.get("params_by_tf", {})
    for pair_key, info in best_config.items():
        if info.get("params"):
            if pair_key not in params_by_tf:
                params_by_tf[pair_key] = {}
            params_by_tf[pair_key].update(info["params"])
    config["params_by_tf"] = params_by_tf

    # Update disabled_timeframes
    config["disabled_timeframes"] = [pair_key for pair_key, _ in disabled_pairs]

    # Save
    config["_version"] = config.get("_version", 0) + 1
    config["_updated_at"] = __import__("datetime").datetime.now().isoformat()
    config["_updated_by"] = "exhaustive_strategy_search_v2"

    # Build notes
    notes_parts = []
    for pair_key, info in best_config.items():
        pstr = _format_params(info.get("params", {}))
        notes_parts.append(f"{pair_key}→{info['strategy']}({info['pnl']:.0f}R/{info['n_trades']}t/{info['wr']:.0f}%WR)[{pstr}]")
    if disabled_pairs:
        notes_parts.append(f"DISABLED: {', '.join(p for p, _ in disabled_pairs)}")
    config["_notes"] = f"Exhaustive 16×{len(ALL_STRATEGIES)} strategy+param search. Active: {active_count}, Disabled: {disabled_count}, Total PnL: {total_pnl:.0f}R. " + "; ".join(notes_parts)

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
            for i, (strat, res, params) in enumerate(results[:5]):
                marker = " ← SELECTED" if pair_key in best_config and best_config[pair_key]["strategy"] == strat else ""
                emoji = "🟢" if res["pnl"] > 0 and res["n_trades"] >= 1 else "🔴"
                pstr = _format_params(params)
                print(f"    {i+1}. {strat:<30s} PnL={res['pnl']:>10.2f}R  trades={res['n_trades']:>3d}  WR={res['wr']:>5.1f}%  DD={res['max_dd']:>8.2f}R  {emoji}{marker}")
                print(f"       params: {pstr}")

    elapsed = time.time() - start_time
    print(f"\n⏱️  Total time: {elapsed:.0f}s ({elapsed/60:.1f}min)")
    print(f"✅ DONE: {active_count}/16 pairs active, total PnL = {total_pnl:.2f}R")


if __name__ == "__main__":
    main()
