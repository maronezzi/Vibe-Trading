#!/usr/bin/env python3
"""
Optimize disabled pairs: find best strategy for WIN_M5, WDO_M5, BIT_M5, BIT_M30, WSP_M5.
Tests all 25 strategies with forward backtest and picks the one with highest positive PnL.
"""

import sys
import os
import json
import itertools
import re
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vt_forward_backtest import (
    fetch_bars_cached,
    simulate_forward,
    _load_strategy_module,
    _load_strategy_utils,
    fetch_bars_for_backtest,
)

# Disabled pairs to optimize
DISABLED_PAIRS = ["WIN_M5", "WDO_M5", "BIT_M5", "BIT_M30", "WSP_M5"]

# All strategies to test — dynamically discovered from strategies/ directory
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

# Contract specs
CONTRACT_SPECS = {
    "WIN": {"tick_size": 0.5, "tick_value": 0.1, "mult": 0.2, "commission": 2.5},
    "WDO": {"tick_size": 0.1, "tick_value": 1.0, "mult": 10.0, "commission": 2.5},
    "BIT": {"tick_size": 0.5, "tick_value": 0.5, "mult": 1.0, "commission": 2.5},
    "WSP": {"tick_size": 0.5, "tick_value": 0.5, "mult": 1.0, "commission": 2.5},
}

SL_ATR_MULTS = [0.8, 1.0, 1.2, 1.5, 2.0]

# Parameter grids for each strategy
PARAM_GRIDS = {
    "adx_trend": {
        "sl_atr_mult": SL_ATR_MULTS,
        "ema_fast": [5, 8],
        "ema_slow": [20, 30],
        "adx_threshold": [20, 25],
    },
    "bollinger": {
        "sl_atr_mult": SL_ATR_MULTS,
        "bb_period": [20, 30],
        "bb_std": [1.5, 2.0],
        "rsi_overbought": [65, 70],
        "rsi_oversold": [30, 35],
    },
    "candle_patterns": {
        "sl_atr_mult": SL_ATR_MULTS,
    },
    "divergence_rsi": {
        "sl_atr_mult": SL_ATR_MULTS,
        "rsi_period": [14, 21],
        "rsi_overbought": [65, 70],
        "rsi_oversold": [30, 35],
    },
    "donchian_breakout": {
        "sl_atr_mult": SL_ATR_MULTS,
        "period": [15, 20, 30],
        "exit_period": [10, 15],
    },
    "ema_crossover": {
        "sl_atr_mult": SL_ATR_MULTS,
        "ema_fast": [5, 8],
        "ema_slow": [20, 30],
        "adx_threshold": [20, 25],
    },
    "ema_pullback": {
        "sl_atr_mult": SL_ATR_MULTS,
        "ema_fast": [5, 8],
        "ema_slow": [20, 30],
        "adx_threshold": [20, 25],
        "pullback_pct": [0.3, 0.5],
    },
    "fibonacci_retracement": {
        "sl_atr_mult": SL_ATR_MULTS,
    },
    "heikin_ashi": {
        "sl_atr_mult": SL_ATR_MULTS,
    },
    "ichimoku": {
        "sl_atr_mult": SL_ATR_MULTS,
        "tenkan_period": [7, 9],
        "kijun_period": [22, 26],
        "senkou_period": [52, 44],
    },
    "keltner_channel": {
        "sl_atr_mult": SL_ATR_MULTS,
        "ema_period": [20, 30],
        "atr_multiplier": [1.5, 2.0],
        "rsi_overbought": [65, 70],
        "rsi_oversold": [30, 35],
    },
    "macd_momentum": {
        "sl_atr_mult": SL_ATR_MULTS,
        "ema_fast": [8, 12],
        "ema_slow": [21, 26],
        "adx_threshold": [20, 25],
    },
    "mean_reversion_zscore": {
        "sl_atr_mult": SL_ATR_MULTS,
        "lookback": [20, 50],
        "z_threshold": [1.5, 2.0],
    },
    "momentum_breakout": {
        "sl_atr_mult": SL_ATR_MULTS,
        "lookback": [10, 20],
        "roc_threshold": [0.5, 1.0],
    },
    "pivot_points": {
        "sl_atr_mult": SL_ATR_MULTS,
    },
    "range_trading": {
        "sl_atr_mult": SL_ATR_MULTS,
        "lookback": [10, 20],
        "range_atr_pct": [0.3, 0.5],
        "touch_pct": [0.7, 0.8],
    },
    "rsi_reversion": {
        "sl_atr_mult": SL_ATR_MULTS,
        "rsi_period": [7, 14],
        "rsi_overbought": [65, 70],
        "rsi_oversold": [30, 35],
        "ema_period": [50, 100],
    },
    "smart_ema": {
        "sl_atr_mult": SL_ATR_MULTS,
        "ema_fast": [5, 8],
        "ema_slow": [20, 30],
        "adx_threshold": [20, 25],
        "pullback_pct": [0.3, 0.5],
    },
    "stochastic": {
        "sl_atr_mult": SL_ATR_MULTS,
        "k_period": [10, 14],
        "d_period": [3, 5],
        "smooth": [3, 5],
        "overbought": [75, 80],
        "oversold": [20, 25],
    },
    "strong_trend": {
        "sl_atr_mult": SL_ATR_MULTS,
        "ema_fast": [5, 8],
        "ema_slow": [20, 30],
        "adx_threshold": [25, 30],
    },
    "supertrend": {
        "sl_atr_mult": SL_ATR_MULTS,
        "atr_period": [7, 10],
        "multiplier": [2.0, 3.0],
    },
    "triple_ema": {
        "sl_atr_mult": SL_ATR_MULTS,
        "ema_fast": [5, 8],
        "ema_mid": [15, 20],
        "ema_slow": [50, 60],
        "adx_threshold": [20, 25],
    },
    "volatility_breakout": {
        "sl_atr_mult": SL_ATR_MULTS,
        "atr_mult": [0.5, 1.0],
        "lookback": [10, 20],
    },
    "vwap": {
        "sl_atr_mult": SL_ATR_MULTS,
        "vwap_period": [10, 20],
        "vwap_buy_threshold": [0.001, 0.002],
        "vwap_sell_threshold": [-0.002, -0.001],
    },
    "win_reversion": {
        "sl_atr_mult": SL_ATR_MULTS,
        "bb_period": [20, 30],
        "bb_std": [1.5, 2.0],
        "rsi_overbought": [65, 70],
        "rsi_oversold": [30, 35],
        "max_ema_dist": [50, 100],
    },
}


def build_param_combos(grid: dict) -> list[dict]:
    """Build list of param dicts from a grid (cartesian product)."""
    keys = sorted(grid.keys())
    values = [grid[k] for k in keys]
    combos = []
    for vals in itertools.product(*values):
        combos.append(dict(zip(keys, vals)))
    return combos


def evaluate_result(result: dict) -> dict:
    """Extract PF, DD, trades, PnL from forward backtest result."""
    decision = result.get("decision", "error")
    if decision != "ok" or result.get("n_trades", 0) == 0:
        return {"pf": 0.0, "dd": 0.0, "trades": 0, "pnl": 0.0}

    pnl = result.get("pnl", 0.0)
    n_trades = result.get("n_trades", 0)
    max_dd = result.get("max_dd", 0.0)
    wr = result.get("wr", 0.0)

    if n_trades > 0 and pnl > 0:
        avg_pnl = pnl / n_trades
        gross_profit = pnl * (wr / 100.0) * 2
        gross_loss = pnl * (1 - wr / 100.0)
        if gross_loss != 0:
            pf = abs(gross_profit / gross_loss)
        else:
            pf = 999.0
    elif pnl > 0:
        pf = 2.0
    else:
        pf = 0.0

    return {"pf": round(pf, 3), "dd": round(max_dd, 2), "trades": n_trades, "pnl": round(pnl, 2)}


def run_single_backtest(symbol: str, tf: str, bars, strategy_name: str, params: dict) -> dict:
    """Run a single forward backtest and return metrics."""
    try:
        result = simulate_forward(symbol, tf, bars, strategy_name, params)
        return evaluate_result(result)
    except Exception as e:
        return {"pf": 0.0, "dd": 0.0, "trades": 0, "pnl": 0.0, "error": str(e)}


def optimize_disabled_pairs():
    """Find best strategy for each disabled pair."""
    print("=" * 80)
    print("OPTIMIZE DISABLED PAIRS")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Pairs: {DISABLED_PAIRS}")
    print(f"Strategies: {len(ALL_STRATEGIES)}")
    print("=" * 80)

    # Resolve symbols — use perpetual/continuous symbols for backtests
    SYMBOL_ROOTS = ["WIN", "WDO", "BIT", "WSP"]
    SYMBOLS = {root: f"{root}$" for root in SYMBOL_ROOTS}
    print(f"Perpetual symbols: {SYMBOLS}")

    # Pre-fetch bars for disabled pairs
    print("\n[1/2] Fetching bars for disabled pairs...")
    bars_cache = {}
    for pair in DISABLED_PAIRS:
        parts = pair.split("_")
        root = parts[0]
        tf = parts[1]
        sym = SYMBOLS.get(root, f"{root}$")
        print(f"  Fetching {pair} ({sym})...", end="", flush=True)
        try:
            bars = fetch_bars_cached(sym, tf)
            bars_cache[pair] = bars
            print(f" {len(bars)} bars")
        except Exception as e:
            print(f" FAILED: {e}")

    # Run grid search
    print("\n[2/2] Running grid search (strategy × disabled pairs × params)...")
    results = {}  # pair -> {strategy, best_pf, best_pnl, best_params, ...}
    total_tests = 0
    completed = 0
    start_time = time.time()

    for pair in DISABLED_PAIRS:
        parts = pair.split("_")
        root = parts[0]
        tf = parts[1]
        bars = bars_cache.get(pair)
        if not bars or len(bars) < 30:
            print(f"  {pair}: NO BARS AVAILABLE")
            results[pair] = {"strategy": "none", "best_pf": 0, "best_pnl": 0, "best_params": {}}
            continue

        best_strat = None
        best_pf = -1
        best_info = {}
        best_params = {}

        for strat in ALL_STRATEGIES:
            grid = PARAM_GRIDS.get(strat, {"sl_atr_mult": SL_ATR_MULTS})
            combos = build_param_combos(grid)
            total_tests += len(combos)

            for params in combos:
                metrics = run_single_backtest(root, tf, bars, strat, params)
                completed += 1

                if metrics["pf"] > best_pf or (metrics["pf"] == best_pf and metrics["dd"] < best_info.get("dd", 999)):
                    best_pf = metrics["pf"]
                    best_strat = strat
                    best_params = params
                    best_info = metrics

                # Progress every 500 tests
                if completed % 500 == 0:
                    elapsed = time.time() - start_time
                    rate = completed / elapsed if elapsed > 0 else 0
                    eta = (total_tests - completed) / rate if rate > 0 else 0
                    print(
                        f"    Progress: {completed}/{total_tests} ({completed * 100 // total_tests}%) - {rate:.0f}/s - ETA {eta:.0f}s"
                    )

        if best_strat and best_pf > 0:
            results[pair] = {
                "strategy": best_strat,
                "best_pf": best_pf,
                "best_dd": best_info.get("dd", 0),
                "best_pnl": best_info.get("pnl", 0),
                "best_trades": best_info.get("trades", 0),
                "best_params": best_params,
            }
            print(
                f"  {pair}: {best_strat} (PF={best_pf:.2f}, DD={best_info.get('dd', 0):.0f}, "
                f"trades={best_info.get('trades', 0)}, PnL=R${best_info.get('pnl', 0):.0f})"
            )
        else:
            results[pair] = {"strategy": "none", "best_pf": 0, "best_pnl": 0, "best_params": {}}
            print(f"  {pair}: NO PROFITABLE STRATEGY FOUND")

    elapsed = time.time() - start_time
    print(f"\n  Grid search complete in {elapsed:.1f}s ({completed} tests)")

    # Print summary
    print("\n" + "=" * 80)
    print("SUMMARY: Best Strategy for Disabled Pairs")
    print("=" * 80)
    print(f"{'Pair':<12} {'Strategy':<25} {'PF':>6} {'DD':>8} {'Trades':>6} {'P&L':>10} {'Params'}")
    print("-" * 80)

    for pair in DISABLED_PAIRS:
        info = results.get(pair, {})
        strat = info.get("strategy", "none")
        if strat == "none":
            print(f"{pair:<12} {'(no profitable strategy)':<25} {'--':>6} {'--':>8} {'--':>6} {'--':>10}")
            continue

        pf = info.get("best_pf", 0)
        dd = info.get("best_dd", 0)
        trades = info.get("best_trades", 0)
        pnl = info.get("best_pnl", 0)
        params = info.get("best_params", {})
        params_str = ", ".join(f"{k}={v}" for k, v in sorted(params.items()) if k != "sl_atr_mult")
        print(f"{pair:<12} {strat:<25} {pf:>6.2f} {dd:>8.0f} {trades:>6} {pnl:>10.0f} {params_str}")

    # Save results
    output = {
        "results": results,
        "generated_at": datetime.now().isoformat(),
        "total_tests": completed,
        "perpetual_symbols": SYMBOLS,
    }
    output_path = os.path.join(os.path.dirname(__file__), "disabled_pairs_results.json")
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nResults saved to: {output_path}")

    return results


if __name__ == "__main__":
    results = optimize_disabled_pairs()
