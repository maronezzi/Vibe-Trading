#!/usr/bin/env python3
"""Final validation: forward backtest all 16 symbol/TF pairs using strategy_by_tf."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from vt_forward_backtest import run_mini_backtest_pair, simulate_forward, fetch_bars_for_backtest, _resolve_pair_params

BAR_COUNT_PER_TF = {"M5": 500, "M15": 200, "M30": 100, "H1": 50}
DEFAULT_BAR_COUNT = 200


def load_config():
    with open("vt_config.json") as f:
        return json.load(f)


def run_pair(sym, tf, config):
    """Run forward backtest for a single SYM_TF pair using strategy_by_tf."""
    key = f"{sym}_{tf}"
    strategy_name = config.get("strategy_by_tf", {}).get(key)

    if not strategy_name or strategy_name == "none":
        return {"key": key, "pnl": 0, "n_trades": 0, "wr": 0, "max_dd": 0, "decision": "disabled"}

    # Get params from params_by_tf
    params = config.get("params_by_tf", {}).get(key, {})

    # Fetch bars
    full_symbol = f"{sym}$"
    bar_count = BAR_COUNT_PER_TF.get(tf, DEFAULT_BAR_COUNT)
    bars = fetch_bars_for_backtest(full_symbol, tf, count=bar_count)

    if not bars:
        return {"key": key, "pnl": 0, "n_trades": 0, "wr": 0, "max_dd": 0, "decision": "no_data"}

    # Simulate
    result = simulate_forward(sym, tf, bars, strategy_name, params)
    return {"key": key, **result}


def main():
    config = load_config()
    symbols = config.get("symbols", [])
    timeframes = config.get("timeframes", [])

    print(f"{'=' * 70}")
    print(f"FINAL FORWARD BACKTEST VALIDATION — ALL {len(symbols) * len(timeframes)} PAIRS")
    print(f"{'=' * 70}\n")

    results = []
    total_pnl = 0
    total_trades = 0
    profitable_count = 0

    for sym in symbols:
        for tf in timeframes:
            result = run_pair(sym, tf, config)
            results.append(result)
            total_pnl += result["pnl"]
            total_trades += result["n_trades"]
            if result["pnl"] > 0:
                profitable_count += 1

            # Status emoji
            if result["decision"] == "disabled":
                status = "⚪"
            elif result["pnl"] > 0:
                status = "🟢"
            elif result["pnl"] < 0:
                status = "🔴"
            else:
                status = "🟡"

            strategy = config.get("strategy_by_tf", {}).get(result["key"], "none")
            print(
                f"  {status} {result['key']:12s} ({strategy:20s}) PnL=R${result['pnl']:+9.2f}  trades={result['n_trades']:3d}  WR={result['wr']:5.1f}%  DD=R${result['max_dd']:8.2f}  [{result['decision']}]"
            )

    # Summary
    print(f"\n{'=' * 70}")
    print(f"SUMMARY")
    print(f"{'=' * 70}")
    print(f"  Total PnL:     R${total_pnl:+.2f}")
    print(f"  Total Trades:  {total_trades}")
    print(f"  Profitable:    {profitable_count}/{len(results)}")
    print(f"  Positive PnL:  {'YES ✅' if total_pnl > 0 else 'NO ❌'}")

    # Save results
    output = {
        "results": {r["key"]: r for r in results},
        "total_pnl": total_pnl,
        "total_trades": total_trades,
        "profitable_count": profitable_count,
        "total_pairs": len(results),
    }
    with open("validation_results.json", "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults saved to validation_results.json")


if __name__ == "__main__":
    main()
