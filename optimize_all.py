#!/usr/bin/env python3
"""
Master optimizer for Vibe-Trading AGI system.
Tests all 25 strategies × 4 symbols × 4 timeframes with focused parameter grids.
Finds optimal strategy_by_tf and params_by_tf assignments for maximum profit.
"""

import sys
import os
import json
import itertools
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vt_forward_backtest import (
    fetch_bars_cached,
    simulate_forward,
    _load_strategy_module,
    _load_strategy_utils,
    fetch_bars_for_backtest,
)
# ── Symbols and timeframes ──────────────────────────────────────────────────
SYMBOL_ROOTS = ["WIN", "WDO", "BIT", "WSP"]
TIMEFRAMES = ["M5", "M15", "M30", "H1"]

# Use perpetual/continuous symbols for backtests (e.g., WIN$, WDO$, BIT$, WSP$)
# NOT specific contracts (e.g., WINM26, WDON26) which have expiry/liquidity issues.
# Real execution uses resolved contracts separately via vt_autotrader → resolve_symbol.
SYMBOLS = {root: f"{root}$" for root in SYMBOL_ROOTS}
print(f"Perpetual symbols: {SYMBOLS}")

# ── Strategy parameter grids ──────────────────────────────────────────────────
# Each strategy has a focused grid of its key tunable parameters.
# Common axis: sl_atr_mult in [0.8, 1.0, 1.2, 1.5, 2.0]
SL_ATR_MULTS = [0.8, 1.0, 1.2, 1.5, 2.0]

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

# ── Contract specs (for P&L) ──────────────────────────────────────────────────
CONTRACT_SPECS = {
    "WIN": {"tick_size": 0.5, "tick_value": 0.1, "mult": 0.2, "commission": 2.5},
    "WDO": {"tick_size": 0.1, "tick_value": 1.0, "mult": 10.0, "commission": 2.5},
    "BIT": {"tick_size": 0.5, "tick_value": 0.5, "mult": 1.0, "commission": 2.5},
    "WSP": {"tick_size": 0.5, "tick_value": 0.5, "mult": 1.0, "commission": 2.5},
}


def build_param_combos(grid: dict) -> list[dict]:
    """Build list of param dicts from a grid (cartesian product)."""
    keys = sorted(grid.keys())
    values = [grid[k] for k in keys]
    combos = []
    for vals in itertools.product(*values):
        combos.append(dict(zip(keys, vals)))
    return combos


def evaluate_result(result: dict, symbol: str) -> dict:
    """Extract PF, DD, trades, P&L from forward backtest result.

    simulate_forward returns: {pnl, n_trades, wr, max_dd, decision}
    """
    decision = result.get("decision", "error")
    if decision != "ok" or result.get("n_trades", 0) == 0:
        return {"pf": 0.0, "dd": 0.0, "trades": 0, "pnl": 0.0}

    pnl = result.get("pnl", 0.0)
    n_trades = result.get("n_trades", 0)
    max_dd = result.get("max_dd", 0.0)
    wr = result.get("wr", 0.0)

    # Estimate PF from win rate and P&L
    # If we have P&L and trades, we can estimate gross profit/loss
    if n_trades > 0 and pnl > 0:
        # Positive P&L = profitable system
        # PF = gross_profit / abs(gross_loss)
        # With WR% and avg P&L per trade, we can estimate
        avg_pnl = pnl / n_trades
        # Assume avg win = avg_pnl * 2 (conservative) and avg loss = avg_pnl
        # This is a rough estimate since we don't have individual trade data
        gross_profit = pnl * (wr / 100.0) * 2  # wins
        gross_loss = pnl * (1 - wr / 100.0)  # losses (negative)
        if gross_loss != 0:
            pf = abs(gross_profit / gross_loss)
        else:
            pf = 999.0  # all wins
    elif pnl > 0:
        pf = 2.0  # default for positive P&L
    else:
        pf = 0.0

    return {"pf": round(pf, 3), "dd": round(max_dd, 2), "trades": n_trades, "pnl": round(pnl, 2)}


def run_single_backtest(symbol: str, tf: str, bars, strategy_name: str, params: dict) -> dict:
    """Run a single forward backtest and return metrics."""
    try:
        result = simulate_forward(symbol, tf, bars, strategy_name, params)
        return evaluate_result(result, symbol)
    except Exception as e:
        return {"pf": 0.0, "dd": 0.0, "trades": 0, "pnl": 0.0, "error": str(e)}


def optimize_all():
    """Main optimizer: test all strategies × symbols × TFs × param combos."""
    print("=" * 80)
    print("VIBE-TRADING MASTER OPTIMIZER")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Strategies: {len(PARAM_GRIDS)}")
    print(f"Symbols: {SYMBOLS}")
    print(f"Timeframes: {TIMEFRAMES}")
    print("=" * 80)

    # Pre-fetch bars for all symbol×TF pairs
    print("\n[1/3] Fetching bars for all symbol×TF pairs...")
    bars_cache = {}
    for root in SYMBOL_ROOTS:
        sym = SYMBOLS[root]
        for tf in TIMEFRAMES:
            key = f"{root}_{tf}"
            print(f"  Fetching {key} ({sym})...", end="", flush=True)
            try:
                bars = fetch_bars_cached(sym, tf)
                bars_cache[key] = bars
                print(f" {len(bars)} bars")
            except Exception as e:
                print(f" FAILED: {e}")

    print(f"\n  Cached {len(bars_cache)} symbol×TF pairs")

    # Run grid search
    print("\n[2/3] Running grid search (strategy × symbol × TF × params)...")
    results = {}  # (strategy, root, tf) -> {best_pf, best_dd, best_params, best_pnl, trades}
    total_tests = 0
    completed = 0

    # Count total
    disabled = {"WIN_M5", "BIT_M5", "WSP_M5", "WDO_M5", "BIT_M30"}
    enabled_count = len(SYMBOL_ROOTS) * len(TIMEFRAMES) - len(disabled)
    for strat, grid in PARAM_GRIDS.items():
        combos = build_param_combos(grid)
        total_tests += len(combos) * enabled_count

    print(f"  Total tests to run: {total_tests}")
    start_time = time.time()

    for strat in sorted(PARAM_GRIDS.keys()):
        grid = PARAM_GRIDS[strat]
        combos = build_param_combos(grid)
        print(
            f"\n  [{strat}] {len(combos)} param combos × {len(SYMBOL_ROOTS)} symbols × {len(TIMEFRAMES)} TFs = {len(combos) * len(SYMBOL_ROOTS) * len(TIMEFRAMES)} tests"
        )

        for root in SYMBOL_ROOTS:
            for tf in TIMEFRAMES:
                key = f"{root}_{tf}"
                bars = bars_cache.get(key)
                if not bars or len(bars) < 30:
                    continue

                # disabled combos
                disabled = {"WIN_M5", "BIT_M5", "WSP_M5", "WDO_M5", "BIT_M30"}
                if key in disabled:
                    continue

                best_pf = -1
                best_params = {}
                best_metrics = {}

                for params in combos:
                    # simulate_forward uses the resolved symbol for P&L calc
                    metrics = run_single_backtest(root, tf, bars, strat, params)
                    completed += 1

                    # Best = highest PF, then lowest DD, then most trades
                    if metrics["pf"] > best_pf or (
                        metrics["pf"] == best_pf and metrics["dd"] < best_metrics.get("dd", 999)
                    ):
                        best_pf = metrics["pf"]
                        best_params = params
                        best_metrics = metrics

                    # Progress every 500 tests
                    if completed % 500 == 0:
                        elapsed = time.time() - start_time
                        rate = completed / elapsed if elapsed > 0 else 0
                        eta = (total_tests - completed) / rate if rate > 0 else 0
                        print(
                            f"    Progress: {completed}/{total_tests} ({completed * 100 // total_tests}%) - {rate:.0f}/s - ETA {eta:.0f}s"
                        )

                results[(strat, root, tf)] = {
                    "best_pf": best_pf,
                    "best_dd": best_metrics.get("dd", 0),
                    "best_pnl": best_metrics.get("pnl", 0),
                    "best_trades": best_metrics.get("trades", 0),
                    "best_params": best_params,
                }

    elapsed = time.time() - start_time
    print(f"\n  Grid search complete in {elapsed:.1f}s ({completed} tests)")

    # Build optimal assignments
    print("\n[3/3] Building optimal assignments...")
    strategy_by_tf = {}
    params_by_tf = {}

    for root in SYMBOL_ROOTS:
        for tf in TIMEFRAMES:
            key = f"{root}_{tf}"
            # disabled combos
            disabled = {"WIN_M5", "BIT_M5", "WSP_M5", "WDO_M5", "BIT_M30"}
            if key in disabled:
                strategy_by_tf[key] = "none"
                params_by_tf[key] = {}
                print(f"  {key}: (disabled in config)")
                continue

            # Find best strategy for this root×tf
            best_strat = None
            best_pf = -1
            best_info = {}

            for strat in PARAM_GRIDS:
                result = results.get((strat, root, tf))
                if result and result["best_pf"] > best_pf:
                    best_pf = result["best_pf"]
                    best_strat = strat
                    best_info = result

            if best_strat and best_pf > 0:
                strategy_by_tf[key] = best_strat
                params_by_tf[key] = best_info["best_params"]
                print(
                    f"  {key}: {best_strat} (PF={best_info['best_pf']:.2f}, DD={best_info['best_dd']:.0f}, trades={best_info['best_trades']}, P&L={best_info['best_pnl']:.0f})"
                )
            else:
                strategy_by_tf[key] = "none"
                params_by_tf[key] = {}
                print(f"  {key}: NO PROFITABLE STRATEGY FOUND")

    # Print summary table
    print("\n" + "=" * 100)
    print("SUMMARY: Best Strategy × Symbol × Timeframe")
    print("=" * 100)
    print(f"{'Key':<10} {'Strategy':<22} {'PF':>6} {'DD':>8} {'Trades':>6} {'P&L':>10} {'Params'}")
    print("-" * 100)

    for root in SYMBOL_ROOTS:
        for tf in TIMEFRAMES:
            key = f"{root}_{tf}"
            strat = strategy_by_tf.get(key, "none")
            if strat == "none":
                print(f"{key:<10} {'(disabled)':<22} {'--':>6} {'--':>8} {'--':>6} {'--':>10}")
                continue

            info = results.get((strat, root, tf), {})
            pf = info.get("best_pf", 0)
            dd = info.get("best_dd", 0)
            trades = info.get("best_trades", 0)
            pnl = info.get("best_pnl", 0)
            params = info.get("best_params", {})
            params_str = ", ".join(f"{k}={v}" for k, v in sorted(params.items()) if k != "sl_atr_mult")
            print(f"{key:<10} {strat:<22} {pf:>6.2f} {dd:>8.0f} {trades:>6} {pnl:>10.0f} {params_str}")

    # Write results to JSON
    output = {
        "strategy_by_tf": strategy_by_tf,
        "params_by_tf": params_by_tf,
        "results": {f"{k[0]}_{k[1]}_{k[2]}": v for k, v in sorted(results.items())},
        "generated_at": datetime.now().isoformat(),
        "total_tests": completed,
        "perpetual_symbols": SYMBOLS,
    }

    output_path = os.path.join(os.path.dirname(__file__), "optimizer_results.json")
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nResults saved to: {output_path}")

    # Print top 10 globally
    print("\n" + "=" * 100)
    print("TOP 10 GLOBALLY (by Profit Factor)")
    print("=" * 100)
    sorted_results = sorted(results.items(), key=lambda x: x[1]["best_pf"], reverse=True)
    print(f"{'Rank':<5} {'Key':<10} {'Strategy':<22} {'PF':>6} {'DD':>8} {'Trades':>6} {'P&L':>10}")
    print("-" * 70)
    for i, ((strat, root, tf), info) in enumerate(sorted_results[:10], 1):
        if info["best_pf"] <= 0:
            continue
        key = f"{root}_{tf}"
        print(
            f"{i:<5} {key:<10} {strat:<22} {info['best_pf']:>6.2f} {info['best_dd']:>8.0f} {info['best_trades']:>6} {info['best_pnl']:>10.0f}"
        )

    return strategy_by_tf, params_by_tf, results


if __name__ == "__main__":
    strategy_by_tf, params_by_tf, results = optimize_all()
