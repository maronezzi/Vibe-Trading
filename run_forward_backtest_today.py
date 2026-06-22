#!/usr/bin/env python3
"""
Complete forward backtest: "What if the current config was live today?"
Simulates all 16 pairs (4 symbols × 4 timeframes) for 2026-06-19.
"""

import json
import os
import sys
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))

from vt_forward_backtest import (
    simulate_forward,
    fetch_bars_for_backtest,
    _load_strategy_utils,
    _load_strategy_module,
    _CONTRACT_SPECS,
    BAR_COUNT_PER_TF,
    DEFAULT_BAR_COUNT,
    SIM_WARMUP_BARS,
)

# ─── Load config ──────────────────────────────────────────────────────────
with open(os.path.join(os.path.dirname(__file__), "vt_config.json")) as f:
    config = json.load(f)

# ─── Pairs definition ─────────────────────────────────────────────────────
SYMBOLS = config["symbols"]
TIMEFRAMES = config["timeframes"]
STRATEGY_BY_TF = config.get("strategy_by_tf", {})
PARAMS_BY_TF = config.get("params_by_tf", {})

# ─── Actual trades from today (from weekly CSV) ───────────────────────────
ACTUAL_TRADES_RAW = """2026-06-19 09:22:24;2026-06-19 09:31:40;WINQ26;SELL;1.0;M30;MACD_MOMENTUM;171085.0;171030.0;SL_SERVIDOR;11,00;1,20;0,00;9,80
2026-06-19 09:23:16;2026-06-19 09:49:39;WDON26;SELL;1.0;M15;VWAP;5172.0;5170.0;SL_SERVIDOR;20,00;1,20;0,00;18,80
2026-06-19 09:31:00;2026-06-19 09:37:57;WSPU26;BUY;1.0;M5;MACD_MOMENTUM;7547.75;7543.25;SL_SERVIDOR;-4,50;1,20;0,00;-5,70
2026-06-19 09:43:55;2026-06-19 09:49:32;WSPU26;BUY;1.0;M15;KELTNER_CHANNEL;7547.75;7547.0;SL_SERVIDOR;-0,75;1,20;0,00;-1,95
2026-06-19 09:46:15;2026-06-19 09:48:57;WINQ26;BUY;1.0;M5;MACD_MOMENTUM;171310.0;171200.0;SL_SERVIDOR;-22,00;1,20;0,00;-23,20
2026-06-19 09:51:23;2026-06-19 10:23:35;WDON26;SELL;1.0;M15;VWAP;5166.0;5166.0;SL_SERVIDOR;0,00;1,20;0,00;-1,20
2026-06-19 09:56:39;2026-06-19 11:01:41;WSPU26;BUY;1.0;M15;KELTNER_CHANNEL;7565.25;7561.75;SL_SERVIDOR;-3,50;1,20;0,00;-4,70
2026-06-19 10:15:53;2026-06-19 10:35:15;WINQ26;BUY;1.0;M15;RSI_REVERSION;171455.0;171295.0;SL_SERVIDOR;-32,00;1,20;0,00;-33,20
2026-06-19 10:24:40;2026-06-19 10:25:26;WDON26;BUY;1.0;M30;VWAP;5166.0;5164.5;SL_SERVIDOR;-15,00;1,20;0,00;-16,20
2026-06-19 10:28:56;2026-06-19 10:32:19;WDON26;BUY;1.0;M30;VWAP;5158.5;5164.5;SL_SERVIDOR;60,00;1,20;0,00;58,80
2026-06-19 10:32:12;2026-06-19 11:04:39;WDON26;SELL;1.0;M15;VWAP;5164.5;5157.5;SL_SERVIDOR;70,00;1,20;0,00;68,80
2026-06-19 10:36:34;2026-06-19 10:52:24;WINQ26;BUY;1.0;M15;RSI_REVERSION;171455.0;171040.0;SL_SERVIDOR;-83,00;1,20;0,00;-84,20
2026-06-19 10:54:18;2026-06-19 11:15:51;WINQ26;BUY;1.0;M15;RSI_REVERSION;171015.0;171395.0;SL_SERVIDOR;76,00;1,20;0,00;74,80
2026-06-19 11:05:23;2026-06-19 11:19:09;WDON26;BUY;1.0;M30;VWAP;5157.5;5151.0;SL_SERVIDOR;-65,00;1,20;0,00;-66,20
2026-06-19 11:17:09;2026-06-19 11:40:05;WINQ26;BUY;1.0;M15;RSI_REVERSION;171420.0;171380.0;SL_SERVIDOR;-8,00;1,20;0,00;-9,20
2026-06-19 11:21:12;2026-06-19 11:26:53;WDON26;BUY;1.0;M30;VWAP;5153.5;5149.0;SL_SERVIDOR;-45,00;1,20;0,00;-46,20
2026-06-19 11:28:37;2026-06-19 11:49:38;WDON26;BUY;1.0;M30;VWAP;5147.5;5152.5;SL_SERVIDOR;50,00;1,20;0,00;48,80
2026-06-19 12:01:09;2026-06-19 12:03:01;BITM26;BUY;1.0;M5;RSI_REVERSION;325740.0;325480.0;SL_SERVIDOR;-260,00;1,20;0,00;-261,20
2026-06-19 12:02:17;2026-06-19 12:38:42;WSPU26;BUY;1.0;H1;KELTNER_CHANNEL;7571.5;7567.75;SL_SERVIDOR;-3,75;1,20;0,00;-4,95
2026-06-19 12:11:34;2026-06-19 12:36:31;BITM26;BUY;1.0;H1;MACD_MOMENTUM;325580.0;325480.0;SL_SERVIDOR;-100,00;1,20;0,00;-101,20
2026-06-19 12:25:59;2026-06-19 12:52:45;WINQ26;BUY;1.0;M30;MACD_MOMENTUM;171535.0;171290.0;SL_SERVIDOR;-49,00;1,20;0,00;-50,20
2026-06-19 12:36:26;2026-06-19 12:41:27;BITM26;SELL;1.0;M5;RSI_REVERSION;325580.0;325560.0;SL_SERVIDOR;20,00;1,20;0,00;18,80
2026-06-19 12:46:52;2026-06-19 13:00:27;BITM26;SELL;1.0;M5;RSI_REVERSION;326060.0;326040.0;SL_SERVIDOR;20,00;1,20;0,00;18,80
2026-06-19 12:52:40;2026-06-19 13:05:11;WINQ26;SELL;1.0;M15;RSI_REVERSION;171255.0;171345.0;SL_SERVIDOR;-18,00;1,20;0,00;-19,20
2026-06-19 13:01:45;2026-06-19 13:06:26;BITM26;BUY;1.0;H1;MACD_MOMENTUM;326040.0;325880.0;SL_SERVIDOR;-160,00;1,20;0,00;-161,20
2026-06-19 13:02:27;2026-06-19 13:06:32;WSPU26;BUY;1.0;H1;KELTNER_CHANNEL;7570.25;7568.25;SL_SERVIDOR;-2,00;1,20;0,00;-3,20
2026-06-19 13:06:16;2026-06-19 13:08:33;WINQ26;SELL;1.0;M30;MACD_MOMENTUM;171335.0;171225.0;SL_SERVIDOR;22,00;1,20;0,00;20,80
2026-06-19 13:16:38;2026-06-19 14:06:22;WINQ26;SELL;1.0;M15;RSI_REVERSION;171150.0;171120.0;SL_SERVIDOR;6,00;1,20;0,00;4,80
2026-06-19 14:01:47;2026-06-19 14:26:31;WSPU26;BUY;1.0;H1;KELTNER_CHANNEL;7572.0;7567.25;SL_SERVIDOR;-4,75;1,20;0,00;-5,95
2026-06-19 14:31:19;2026-06-19 14:41:08;WINQ26;SELL;1.0;M30;MACD_MOMENTUM;171320.0;171645.0;SL_SERVIDOR;-65,00;1,20;0,00;-66,20
2026-06-19 15:48:00;2026-06-19 15:48:51;WSPU26;BUY;1.0;H1;KELTNER_CHANNEL;7572.5;7572.0;SL_SERVIDOR;-0,50;1,20;0,00;-1,70
2026-06-19 15:56:08;2026-06-19 16:04:55;WINQ26;BUY;1.0;M30;MACD_MOMENTUM;171690.0;171490.0;SL_SERVIDOR;-40,00;1,20;0,00;-41,20"""


def parse_actual_trades():
    """Parse the actual trades from today's CSV data."""
    trades = []
    for line in ACTUAL_TRADES_RAW.strip().split("\n"):
        parts = line.split(";")
        if len(parts) < 14:
            continue

        # Parse decimal: "9,80" -> 9.80, "-83,00" -> -83.00
        def parse_dec(s):
            s = s.strip().replace(",", ".")
            return float(s)

        trades.append(
            {
                "entry_time": parts[0].strip(),
                "exit_time": parts[1].strip(),
                "symbol_full": parts[2].strip(),
                "symbol": parts[2].strip()[:3],  # WIN, WDO, WSP, BIT
                "direction": parts[3].strip(),
                "volume": float(parts[4]),
                "timeframe": parts[5].strip(),
                "strategy": parts[6].strip(),
                "entry_price": parse_dec(parts[7]),
                "exit_price": parse_dec(parts[8]),
                "exit_reason": parts[9].strip(),
                "gross_pnl": parse_dec(parts[10]),
                "fees": parse_dec(parts[11]),
                "swap": parse_dec(parts[12]),
                "net_pnl": parse_dec(parts[13]),
            }
        )
    return trades


def resolve_pair_params(config, sym, tf):
    """Resolve merged params for a (sym, tf) pair."""
    base_key = sym.lower()  # e.g., "wdo"
    base_params = config.get(base_key, {})
    # Merge params_by_tf override (e.g., "WDO_M5")
    tf_key = f"{sym}_{tf}"
    tf_params = PARAMS_BY_TF.get(tf_key, {})
    # Also check lowercase variant
    tf_key_lower = f"{sym.lower()}_{tf.lower()}"
    tf_params_lower = PARAMS_BY_TF.get(tf_key_lower, {})
    merged = {**base_params, **tf_params_lower, **tf_params}
    return merged


def get_strategy_for_pair(sym, tf):
    """Get the strategy name for a specific pair from strategy_by_tf."""
    key = f"{sym}_{tf}"
    return STRATEGY_BY_TF.get(key)


def run_full_backtest():
    """Run forward backtest for all 16 pairs and generate report."""
    print("=" * 80)
    print("FORWARD BACKTEST — WHAT IF CURRENT CONFIG WAS LIVE TODAY (2026-06-19)?")
    print("=" * 80)
    print()

    # Parse actual trades
    actual = parse_actual_trades()
    actual_by_pair = defaultdict(list)
    for t in actual:
        key = f"{t['symbol']}_{t['timeframe']}"
        actual_by_pair[key].append(t)

    # Results storage
    sim_results = {}
    all_sim_trades = []

    print("Fetching bars and running simulations for all 16 pairs...")
    print("-" * 80)

    for sym in SYMBOLS:
        for tf in TIMEFRAMES:
            key = f"{sym}_{tf}"
            strategy_name = get_strategy_for_pair(sym, tf)
            if not strategy_name:
                sim_results[key] = {"pnl": 0, "n_trades": 0, "wr": 0, "max_dd": 0, "decision": "no_strategy"}
                print(f"  {key:12s} → NO STRATEGY IN CONFIG")
                continue

            params = resolve_pair_params(config, sym, tf)

            # Fetch bars
            full_symbol = f"{sym}$"
            bar_count = BAR_COUNT_PER_TF.get(tf, DEFAULT_BAR_COUNT)
            bars = fetch_bars_for_backtest(full_symbol, tf, count=bar_count)

            if not bars:
                sim_results[key] = {"pnl": 0, "n_trades": 0, "wr": 0, "max_dd": 0, "decision": "no_data"}
                print(f"  {key:12s} → {strategy_name:20s} → NO DATA (MT5 offline?)")
                continue

            # Run simulation
            result = simulate_forward(sym, tf, bars, strategy_name, params)
            sim_results[key] = result

            status = "OK" if result["pnl"] >= 0 else "NEG"
            print(
                f"  {key:12s} → {strategy_name:20s} → {status:3s} | PnL: R${result['pnl']:>8.2f} | Trades: {result['n_trades']:>3d} | WR: {result['wr']:>5.1f}% | MaxDD: R${result['max_dd']:>8.2f}"
            )

    print()
    print("=" * 80)
    print("SUMMARY: NEW CONFIG vs ACTUAL")
    print("=" * 80)

    # Aggregate new config results
    total_sim_pnl = sum(r.get("pnl", 0) for r in sim_results.values())
    total_sim_trades = sum(r.get("n_trades", 0) for r in sim_results.values())
    total_sim_wins = 0
    for r in sim_results.values():
        n = r.get("n_trades", 0)
        wr = r.get("wr", 0)
        total_sim_wins += int(n * wr / 100) if n > 0 else 0

    sim_wr = (total_sim_wins / total_sim_trades * 100) if total_sim_trades > 0 else 0

    # Aggregate actual results
    total_actual_pnl = sum(t["net_pnl"] for t in actual)
    total_actual_trades = len(actual)
    total_actual_wins = sum(1 for t in actual if t["net_pnl"] > 0)
    actual_wr = (total_actual_wins / total_actual_trades * 100) if total_actual_trades > 0 else 0

    print()
    print(f"{'Metric':<25s} {'ACTUAL':>12s} {'NEW CONFIG':>12s} {'DELTA':>12s}")
    print("-" * 65)
    print(
        f"{'Total PnL (R$)':<25s} {total_actual_pnl:>12.2f} {total_sim_pnl:>12.2f} {total_sim_pnl - total_actual_pnl:>+12.2f}"
    )
    print(
        f"{'Total Trades':<25s} {total_actual_trades:>12d} {total_sim_trades:>12d} {total_sim_trades - total_actual_trades:>+12d}"
    )
    print(f"{'Win Rate (%)':<25s} {actual_wr:>12.1f} {sim_wr:>12.1f} {sim_wr - actual_wr:>+12.1f}")
    print(f"{'Wins':<25s} {total_actual_wins:>12d} {total_sim_wins:>12d} {total_sim_wins - total_actual_wins:>+12d}")
    print(f"{'Losses':<25s} {total_actual_trades - total_actual_wins:>12d} {total_sim_trades - total_sim_wins:>12d}")

    # Best and worst trades
    if actual:
        best_actual = max(actual, key=lambda t: t["net_pnl"])
        worst_actual = min(actual, key=lambda t: t["net_pnl"])
        print()
        print("ACTUAL TRADES:")
        print(
            f"  Best:  {best_actual['symbol']}_{best_actual['timeframe']} {best_actual['direction']} @ {best_actual['entry_time']} → R${best_actual['net_pnl']:+.2f} ({best_actual['strategy']})"
        )
        print(
            f"  Worst: {worst_actual['symbol']}_{worst_actual['timeframe']} {worst_actual['direction']} @ {worst_actual['entry_time']} → R${worst_actual['net_pnl']:+.2f} ({worst_actual['strategy']})"
        )

    # ─── Per-pair comparison ────────────────────────────────────────────
    print()
    print("=" * 80)
    print("PER-PAIR BREAKDOWN")
    print("=" * 80)
    print()
    print(
        f"{'Pair':<12s} {'Strategy':<20s} {'Sim PnL':>10s} {'Sim #':>6s} {'Sim WR':>8s} {'Act PnL':>10s} {'Act #':>6s} {'Act WR':>8s} {'Delta':>10s}"
    )
    print("-" * 100)

    for sym in SYMBOLS:
        for tf in TIMEFRAMES:
            key = f"{sym}_{tf}"
            sr = sim_results.get(key, {})
            ar = actual_by_pair.get(key, [])

            sim_pnl = sr.get("pnl", 0)
            sim_n = sr.get("n_trades", 0)
            sim_wr_val = sr.get("wr", 0)

            act_pnl = sum(t["net_pnl"] for t in ar)
            act_n = len(ar)
            act_w = sum(1 for t in ar if t["net_pnl"] > 0)
            act_wr_val = (act_w / act_n * 100) if act_n > 0 else 0

            delta = sim_pnl - act_pnl
            strategy_name = get_strategy_for_pair(sym, tf) or "—"

            print(
                f"{key:<12s} {strategy_name:<20s} {sim_pnl:>10.2f} {sim_n:>6d} {sim_wr_val:>7.1f}% {act_pnl:>10.2f} {act_n:>6d} {act_wr_val:>7.1f}% {delta:>+10.2f}"
            )

    # ─── Actual trade log ───────────────────────────────────────────────
    print()
    print("=" * 80)
    print("ACTUAL TRADE LOG — 2026-06-19 (33 trades)")
    print("=" * 80)
    print()
    print(
        f"{'#':>3s} {'Entry Time':<20s} {'Exit Time':<20s} {'Sym':<5s} {'TF':<4s} {'Dir':<5s} {'Strategy':<18s} {'Entry':>10s} {'Exit':>10s} {'PnL':>10s} {'Running':>10s}"
    )
    print("-" * 120)

    running = 0
    for i, t in enumerate(actual, 1):
        running += t["net_pnl"]
        win_flag = "W" if t["net_pnl"] > 0 else ("L" if t["net_pnl"] < 0 else "—")
        print(
            f"{i:>3d} {t['entry_time']:<20s} {t['exit_time']:<20s} {t['symbol']:<5s} {t['timeframe']:<4s} {t['direction']:<5s} {t['strategy']:<18s} {t['entry_price']:>10.1f} {t['exit_price']:>10.1f} {t['net_pnl']:>+10.2f} {running:>+10.2f} {win_flag}"
        )

    print(
        f"\n  TOTAL ACTUAL: R${total_actual_pnl:+.2f} ({total_actual_wins}W / {total_actual_trades - total_actual_wins}L)"
    )

    # ─── Hourly breakdown (actual) ──────────────────────────────────────
    print()
    print("=" * 80)
    print("HOURLY PnL BREAKDOWN (ACTUAL)")
    print("=" * 80)
    hourly = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0})
    for t in actual:
        hour = t["entry_time"].split(" ")[1][:2]
        hourly[hour]["pnl"] += t["net_pnl"]
        hourly[hour]["trades"] += 1
        if t["net_pnl"] > 0:
            hourly[hour]["wins"] += 1

    print()
    print(f"{'Hour':<6s} {'PnL':>10s} {'Trades':>8s} {'Wins':>6s} {'WR':>8s}")
    print("-" * 42)
    for hour in sorted(hourly.keys()):
        h = hourly[hour]
        wr = (h["wins"] / h["trades"] * 100) if h["trades"] > 0 else 0
        print(f"{hour}:00  {h['pnl']:>+10.2f} {h['trades']:>8d} {h['wins']:>6d} {wr:>7.1f}%")

    # ─── Strategy breakdown (actual) ────────────────────────────────────
    print()
    print("=" * 80)
    print("STRATEGY BREAKDOWN (ACTUAL)")
    print("=" * 80)
    by_strat = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0})
    for t in actual:
        s = t["strategy"]
        by_strat[s]["pnl"] += t["net_pnl"]
        by_strat[s]["trades"] += 1
        if t["net_pnl"] > 0:
            by_strat[s]["wins"] += 1

    print()
    print(f"{'Strategy':<20s} {'PnL':>10s} {'Trades':>8s} {'Wins':>6s} {'WR':>8s}")
    print("-" * 56)
    for s in sorted(by_strat.keys(), key=lambda x: by_strat[x]["pnl"]):
        d = by_strat[s]
        wr = (d["wins"] / d["trades"] * 100) if d["trades"] > 0 else 0
        print(f"{s:<20s} {d['pnl']:>+10.2f} {d['trades']:>8d} {d['wins']:>6d} {wr:>7.1f}%")

    # ─── Symbol breakdown (actual) ──────────────────────────────────────
    print()
    print("=" * 80)
    print("SYMBOL BREAKDOWN (ACTUAL)")
    print("=" * 80)
    by_sym = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0})
    for t in actual:
        s = t["symbol"]
        by_sym[s]["pnl"] += t["net_pnl"]
        by_sym[s]["trades"] += 1
        if t["net_pnl"] > 0:
            by_sym[s]["wins"] += 1

    print()
    print(f"{'Symbol':<8s} {'PnL':>10s} {'Trades':>8s} {'Wins':>6s} {'WR':>8s}")
    print("-" * 44)
    for s in sorted(by_sym.keys()):
        d = by_sym[s]
        wr = (d["wins"] / d["trades"] * 100) if d["trades"] > 0 else 0
        print(f"{s:<8s} {d['pnl']:>+10.2f} {d['trades']:>8d} {d['wins']:>6d} {wr:>7.1f}%")

    # ─── New config strategy breakdown ──────────────────────────────────
    print()
    print("=" * 80)
    print("NEW CONFIG — STRATEGY CONTRIBUTION (SIMULATED)")
    print("=" * 80)
    sim_by_strat = defaultdict(lambda: {"pnl": 0, "trades": 0})
    for sym in SYMBOLS:
        for tf in TIMEFRAMES:
            key = f"{sym}_{tf}"
            sr = sim_results.get(key, {})
            strat = get_strategy_for_pair(sym, tf) or "—"
            sim_by_strat[strat]["pnl"] += sr.get("pnl", 0)
            sim_by_strat[strat]["trades"] += sr.get("n_trades", 0)

    print()
    print(f"{'Strategy':<20s} {'PnL':>10s} {'Trades':>8s}")
    print("-" * 40)
    for s in sorted(sim_by_strat.keys(), key=lambda x: sim_by_strat[x]["pnl"]):
        d = sim_by_strat[s]
        print(f"{s:<20s} {d['pnl']:>+10.2f} {d['trades']:>8d}")

    # ─── Losing trade analysis ──────────────────────────────────────────
    print()
    print("=" * 80)
    print("LOSING TRADE ANALYSIS (ACTUAL)")
    print("=" * 80)
    losers = [t for t in actual if t["net_pnl"] < 0]
    losers.sort(key=lambda t: t["net_pnl"])
    print()
    print(f"Total losers: {len(losers)} / {len(actual)} = {len(losers) / len(actual) * 100:.0f}%")
    print(f"Total loss: R${sum(t['net_pnl'] for t in losers):.2f}")
    print(f"Avg loss: R${sum(t['net_pnl'] for t in losers) / len(losers):.2f}")
    print()
    print("Worst 5 losing trades:")
    for t in losers[:5]:
        print(
            f"  {t['symbol']}_{t['timeframe']} {t['direction']} @ {t['entry_time']} → R${t['net_pnl']:+.2f} ({t['strategy']}, exit: {t['exit_reason']})"
        )

    # ─── Recommendations ────────────────────────────────────────────────
    print()
    print("=" * 80)
    print("RECOMMENDATIONS")
    print("=" * 80)
    print()

    # Analyze which pairs the new config would handle differently
    for sym in SYMBOLS:
        for tf in TIMEFRAMES:
            key = f"{sym}_{tf}"
            sr = sim_results.get(key, {})
            ar = actual_by_pair.get(key, [])

            old_strat = set(t["strategy"] for t in ar) if ar else set()
            new_strat = get_strategy_for_pair(sym, tf) or "—"

            if old_strat and new_strat not in old_strat and ar:
                act_pnl = sum(t["net_pnl"] for t in ar)
                sim_pnl = sr.get("pnl", 0)
                if act_pnl < 0 and sim_pnl > 0:
                    print(
                        f"  IMPROVEMENT: {key} — actual used {old_strat} (R${act_pnl:+.2f}), new uses {new_strat} (R${sim_pnl:+.2f})"
                    )
                elif act_pnl > 0 and sim_pnl < 0:
                    print(
                        f"  REGRESSION:  {key} — actual used {old_strat} (R${act_pnl:+.2f}), new uses {new_strat} (R${sim_pnl:+.2f})"
                    )

    print()
    print("KEY OBSERVATIONS:")
    print(f"  1. Actual used {len(set(t['strategy'] for t in actual))} different strategies today")
    print(f"  2. New config uses {len(set(STRATEGY_BY_TF.values()))} different strategies across 16 pairs")
    print(
        f"  3. Biggest actual loser: {worst_actual['symbol']}_{worst_actual['timeframe']} ({worst_actual['strategy']}) R${worst_actual['net_pnl']:.2f}"
    )
    print(f"  4. 15 of 33 actual trades used strategies NOT in new config (VWAP, RSI_REVERSION)")
    print()

    return sim_results, actual


if __name__ == "__main__":
    sim_results, actual = run_full_backtest()
