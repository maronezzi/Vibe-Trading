"""
backtest_v7_plugin.py — Backtest using actual strategy plugins (hot-reload compatible).
Tests EMA_PULLBACK for WIN and VWAP for WDO across M5 and M15.
"""

import sys, csv, io, subprocess, os, importlib, importlib.util
from pathlib import Path
import numpy as np, pandas as pd

WINE_PYTHON = os.path.expanduser("~/.wine/drive_c/Python311/python.exe")
FETCH_SCRIPT = os.path.join(os.path.dirname(__file__), "mt5_fetch.py")
STRATEGIES_DIR = Path(__file__).parent / "strategies"

CLOSE_HOUR, CLOSE_MINUTE = 16, 45

CONTRACT_SPECS = {
    "WIN$": {"mult": 0.20, "name": "Mini Índice", "margin": 5000, "tick": 5, "slip_r": 1.0},
    "WDO$": {"mult": 10.0, "name": "Mini Dólar", "margin": 3000, "tick": 0.5, "slip_r": 5.0},
}
COMMISSION = 2.5

# Default params per symbol
DEFAULT_PARAMS = {
    "WIN": {
        "ema_fast": 9, "ema_slow": 21, "adx_period": 14, "adx_threshold": 20,
        "rsi_period": 14, "rsi_overbought": 70, "rsi_oversold": 30,
        "pullback_pct": 0.15, "sl_atr_mult": 1.5, "trail_activate": 1.5,
        "trail_distance": 0.5, "cooldown_seconds": 600, "max_daily_trades": 10,
        "bb_period": 20, "bb_std": 2.0,
    },
    "WDO": {
        "ema_fast": 9, "ema_slow": 21, "rsi_period": 14, "rsi_overbought": 70,
        "rsi_oversold": 30, "vwap_period": 10, "vwap_buy_threshold": 1.002,
        "vwap_sell_threshold": 0.995, "sl_atr_mult": 1.0, "trail_activate": 1.0,
        "trail_distance": 0.2, "cooldown_seconds": 300, "max_daily_trades": 8,
        "trend_min_spread": 0,
    },
}


def fetch(symbol, tf, n_bars):
    cmd = ["wine", WINE_PYTHON, FETCH_SCRIPT, "rates", symbol, tf, str(n_bars)]
    env = {**os.environ, "WINEDEBUG": "-all"}
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    if r.returncode != 0 or not r.stdout.strip():
        return pd.DataFrame()
    reader = csv.reader(io.StringIO(r.stdout.strip()))
    headers = next(reader)
    rows = [x for x in reader if x]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=headers)
    for c in ["open", "high", "low", "close", "tick_volume", "real_volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["time"] = pd.to_datetime(df["time"].astype(int), unit="s")
    df = df.set_index("time")
    df["hour"] = df.index.hour
    df["minute"] = df.index.minute
    df["date"] = df.index.date
    return df[["open", "high", "low", "close", "tick_volume", "real_volume", "hour", "minute", "date"]].dropna(subset=["close"])


def load_strategy(name):
    """Load a strategy plugin by name."""
    for py_file in STRATEGIES_DIR.glob("*.py"):
        if py_file.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(f"strategies.{py_file.stem}", str(py_file))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sname = getattr(module, "STRATEGY_NAME", py_file.stem.upper())
        if sname == name:
            return module.check_entry
    return None


def calc_atr(bars, period=14):
    """Calculate ATR from bar list (newest-first)."""
    if len(bars) < period + 1:
        return 0
    tr_sum = 0
    for i in range(period):
        h = bars[i]["high"]
        l = bars[i]["low"]
        c_prev = bars[i + 1]["close"]
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        tr_sum += tr
    return tr_sum / period


def calc_atr_series(df, period=14):
    """Calculate ATR as pandas series."""
    h, l = df["high"], df["low"]
    c_prev = df["close"].shift(1)
    tr = pd.concat([h - l, (h - c_prev).abs(), (l - c_prev).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


# Utility functions for strategies (matching autotrader interface)
def _calc_sl(symbol, atr_val, params):
    is_win = "WIN" in symbol
    raw = int(atr_val * params.get("sl_atr_mult", 1.0))
    if is_win:
        raw = max(raw, 100)
    else:
        raw = max(raw, 200)
    return ((raw + 4) // 5) * 5


def calculate_vwap(bars, period=20):
    if not bars or len(bars) < period:
        return 0
    data = bars[:period]
    sum_pv, sum_v = 0, 0
    for b in data:
        typical = (b["high"] + b["low"] + b["close"]) / 3
        vol = max(b["volume"], 1)
        sum_pv += typical * vol
        sum_v += vol
    return sum_pv / sum_v if sum_v > 0 else 0


def calculate_ema(bars, period):
    if not bars or len(bars) < period:
        return 0
    chronological = list(reversed(bars))
    seed = sum(b["close"] for b in chronological[:period]) / period
    ema = seed
    multiplier = 2 / (period + 1)
    for b in chronological[period:]:
        ema = b["close"] * multiplier + ema * (1 - multiplier)
    return ema


def calculate_rsi(bars, period=14):
    if not bars or len(bars) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(min(period + 1, len(bars) - 1)):
        diff = bars[i]["close"] - bars[i + 1]["close"]
        gains.append(diff if diff > 0 else 0)
        losses.append(abs(diff) if diff < 0 else 0)
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0.001
    rs = avg_gain / avg_loss if avg_loss > 0 else 100
    return 100 - (100 / (1 + rs))


def calculate_adx(bars, period=14):
    if not bars or len(bars) < period * 2:
        return 0, 0, 0
    highs = [b["high"] for b in bars[:period * 2]]
    lows = [b["low"] for b in bars[:period * 2]]
    closes = [b["close"] for b in bars[:period * 2]]
    plus_dm, minus_dm = [], []
    for i in range(1, len(highs)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)
    tr_list = []
    for i in range(1, len(highs)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        tr_list.append(tr)
    if len(tr_list) < period:
        return 0, 0, 0
    atr_val = sum(tr_list[:period]) / period
    plus_dm_smooth = sum(plus_dm[:period]) / period
    minus_dm_smooth = sum(minus_dm[:period]) / period
    for i in range(period, len(tr_list)):
        atr_val = (atr_val * (period - 1) + tr_list[i]) / period
        plus_dm_smooth = (plus_dm_smooth * (period - 1) + plus_dm[i]) / period
        minus_dm_smooth = (minus_dm_smooth * (period - 1) + minus_dm[i]) / period
    if atr_val == 0:
        return 0, 0, 0
    plus_di = 100 * plus_dm_smooth / atr_val
    minus_di = 100 * minus_dm_smooth / atr_val
    di_sum = plus_di + minus_di
    dx = 100 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0
    return dx, plus_di, minus_di


def calculate_bollinger(bars, period=20, num_std=2.0):
    if not bars or len(bars) < period:
        return 0, 0, 0
    closes = [b["close"] for b in bars[:period]]
    mid = sum(closes) / period
    variance = sum((c - mid) ** 2 for c in closes) / period
    std = variance ** 0.5
    return mid + num_std * std, mid, mid - num_std * std


def calculate_atr(bars, period=14):
    if not bars or len(bars) < period + 1:
        return 0
    tr_sum = 0
    for i in range(period):
        h, l = bars[i]["high"], bars[i]["low"]
        c_prev = bars[i + 1]["close"]
        tr_sum += max(h - l, abs(h - c_prev), abs(l - c_prev))
    return tr_sum / period


def get_market_regime(bars, params=None):
    if params is None:
        params = DEFAULT_PARAMS["WDO"]
    ema_slow_val = params.get("ema_slow", 21)
    if not bars or len(bars) < ema_slow_val + 5:
        return "CHOPPY"
    ema_f = calculate_ema(bars, params.get("ema_fast", 9))
    ema_s = calculate_ema(bars, ema_slow_val)
    current_price = bars[0]["close"]
    if ema_f == 0 or ema_s == 0 or current_price == 0:
        return "CHOPPY"
    spread = abs(ema_f - ema_s) / current_price
    if spread < params.get("trend_min_spread", 0.001):
        return "CHOPPY"
    return "TREND_UP" if ema_f > ema_s else "TREND_DOWN"


def backtest_with_plugin(df, symbol, strategy_name, params, *, capital=100_000.0):
    """Backtest using a strategy plugin."""
    check_entry = load_strategy(strategy_name)
    if check_entry is None:
        print(f"  ❌ Strategy {strategy_name} not found!")
        return None

    spec = CONTRACT_SPECS[symbol]
    mult, margin, slip_r = spec["mult"], spec["margin"], spec["slip_r"]
    is_win = "WIN" in symbol

    utils = {
        "calculate_vwap": calculate_vwap,
        "calculate_ema": calculate_ema,
        "calculate_rsi": calculate_rsi,
        "calculate_adx": calculate_adx,
        "calculate_bollinger": calculate_bollinger,
        "calculate_atr": calculate_atr,
        "get_market_regime": get_market_regime,
        "calc_sl": _calc_sl,
    }

    TRAIL_ACTIVATE = params.get("trail_activate", 1.5)
    TRAIL_DISTANCE = params.get("trail_distance", 0.5)

    # Build bar list (newest-first) for each row
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    opens = df["open"].values
    vols = df["tick_volume"].values
    dates = df["date"].values
    hours = df["hour"].values
    minutes = df["minute"].values
    n = len(df)

    cash = capital
    pos = 0
    ep = 0.0
    e_date = None
    e_atr = 0.0
    best = 0.0
    sl_price = 0.0
    trail_on = False
    sl_pts = 0
    bars_in_trade = 0
    last_trade_bar = -9999

    equity, trade_log, daily_pnl = [], [], []
    n_trades = n_wins = n_long = n_short = 0
    n_sl = n_trail = n_close = 0
    gross_win = 0.0
    gross_loss_val = 0.0
    daily_pnl_dict = {}

    def _close(price, reason):
        nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, e_atr
        nonlocal n_trades, n_wins, n_long, n_short, n_sl, n_trail, n_close
        nonlocal gross_win, gross_loss_val, bars_in_trade

        if pos == 0:
            return

        sl_cost = slip_r
        comm = COMMISSION

        if pos == 1:
            pnl = (price - ep) * mult - sl_cost - comm
            n_long += 1
        else:
            pnl = (ep - price) * mult - sl_cost - comm
            n_short += 1

        cash += margin + pnl
        n_trades += 1

        if reason == "SL": n_sl += 1
        elif reason == "TRAIL": n_trail += 1
        elif reason == "1645": n_close += 1

        if pnl > 0:
            n_wins += 1; gross_win += pnl
        else:
            gross_loss_val += abs(pnl)

        trade_log.append({
            "type": "LONG" if pos == 1 else "SHORT",
            "entry": str(e_date), "exit": "",
            "ep": ep, "xp": price, "pnl": pnl, "reason": reason,
            "bars": bars_in_trade, "sl_pts": sl_pts,
        })
        daily_pnl.append(pnl)

        d = e_date.date() if hasattr(e_date, 'date') else e_date
        if d not in daily_pnl_dict:
            daily_pnl_dict[d] = 0.0
        daily_pnl_dict[d] += pnl

        pos = 0; ep = 0; best = 0; sl_price = 0; trail_on = False
        bars_in_trade = 0

    for i in range(n):
        price = float(closes[i])
        high = float(highs[i])
        low = float(lows[i])
        hour = int(hours[i])
        minute = int(minutes[i])
        bar_date = dates[i]

        # Build bars list for strategy (newest-first, up to 60 bars)
        bar_list = []
        for j in range(max(0, i - 59), i + 1):
            bar_list.append({
                "high": float(highs[j]),
                "low": float(lows[j]),
                "close": float(closes[j]),
                "open": float(opens[j]),
                "volume": int(vols[j]),
                "time": int(df.index[j].timestamp()),
            })

        cur_atr = calc_atr(bar_list, 14)

        # Mark-to-market
        if pos == 1:
            eq_val = cash + (price - ep) * mult + margin
        elif pos == -1:
            eq_val = cash + (ep - price) * mult + margin
        else:
            eq_val = cash
        equity.append(eq_val)

        # No position: check entry
        if pos == 0:
            if cur_atr > 0 and (i - last_trade_bar) > 5:  # cooldown
                signal = check_entry(symbol, "M5", price, cur_atr, str(bar_date), bar_list, params, utils)
                if signal:
                    direction = signal["direction"]
                    sl_pts_raw = signal["sl_pts"]

                    cost = slip_r + COMMISSION
                    if cash >= margin + cost:
                        cash -= margin + cost
                        pos = 1 if direction == "BUY" else -1
                        ep = price; e_date = bar_date; e_atr = cur_atr; sl_pts = sl_pts_raw
                        best = price; trail_on = False; last_trade_bar = i
                        bars_in_trade = 0
                        if pos == 1:
                            sl_price = price - sl_pts_raw
                        else:
                            sl_price = price + sl_pts_raw
            continue

        # Position open
        bars_in_trade += 1

        if pos == 1:
            best = max(best, high)
        elif pos == -1:
            best = min(best, low) if best > 0 else low

        profit_pts = (best - ep) if pos == 1 else (ep - best)

        if not trail_on and e_atr > 0 and profit_pts >= TRAIL_ACTIVATE * e_atr:
            trail_on = True

        if trail_on and e_atr > 0:
            trail_dist = TRAIL_DISTANCE * e_atr
            if pos == 1:
                new_sl = best - trail_dist
                if new_sl > sl_price:
                    sl_price = new_sl
            elif pos == -1:
                new_sl = best + trail_dist
                if new_sl < sl_price:
                    sl_price = new_sl

        if sl_price > 0:
            if pos == 1 and low <= sl_price:
                _close(sl_price, "SL"); continue
            elif pos == -1 and high >= sl_price:
                _close(sl_price, "SL"); continue

        if hour > CLOSE_HOUR or (hour == CLOSE_HOUR and minute >= CLOSE_MINUTE):
            _close(price, "1645"); continue

    if pos != 0:
        _close(float(closes[-1]), "FORCE")

    # Stats
    eq = pd.Series(equity, index=df.index[:len(equity)])
    total_ret = (cash - capital) / capital * 100
    n_days = df["date"].nunique()
    daily_vals = list(daily_pnl_dict.values())

    if len(daily_vals) > 1:
        sharpe = np.mean(daily_vals) / np.std(daily_vals) * np.sqrt(252) if np.std(daily_vals) > 0 else 0
    else:
        sharpe = 0

    eq_arr = np.array(equity)
    running_max = np.maximum.accumulate(eq_arr)
    drawdowns = (running_max - eq_arr) / running_max * 100
    max_dd = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0

    pf = gross_win / gross_loss_val if gross_loss_val > 0 else (999 if gross_win > 0 else 0)
    wr = (n_wins / n_trades * 100) if n_trades else 0

    wins_p = [t["pnl"] for t in trade_log if t["pnl"] > 0]
    losses_p = [t["pnl"] for t in trade_log if t["pnl"] <= 0]
    avg_win = np.mean(wins_p) if wins_p else 0
    avg_loss = abs(np.mean(losses_p)) if losses_p else 1
    payoff = avg_win / avg_loss if avg_loss > 0 else 0
    avg_daily = sum(t["pnl"] for t in trade_log) / n_days if n_days else 0

    pnl_by_reason = {}
    for t in trade_log:
        r = t["reason"]
        if r not in pnl_by_reason:
            pnl_by_reason[r] = {"count": 0, "pnl": 0, "wins": 0}
        pnl_by_reason[r]["count"] += 1
        pnl_by_reason[r]["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            pnl_by_reason[r]["wins"] += 1

    avg_bars = np.mean([t["bars"] for t in trade_log]) if trade_log else 0

    return {
        "ok": True, "trades": n_trades, "wins": n_wins, "wr": wr,
        "long": n_long, "short": n_short,
        "ret": total_ret, "sharpe": sharpe, "max_dd": max_dd,
        "pf": pf, "avg_daily": avg_daily, "n_days": n_days,
        "avg_win": avg_win, "avg_loss": avg_loss, "payoff": payoff,
        "n_sl": n_sl, "n_trail": n_trail, "n_close": n_close,
        "avg_bars": avg_bars, "reasons": pnl_by_reason, "trade_log": trade_log,
    }


def run():
    print("\n" + "═" * 100)
    print("  🧪 BACKTEST v7 — PLUGIN-BASED (EMA_PULLBACK for WIN, VWAP for WDO)")
    print("  " + "─" * 96)
    print("  ✅ Uses actual strategy plugins (hot-reload compatible)")
    print("  ✅ EMA_PULLBACK: trend-following with pullback entries")
    print("  ✅ VWAP: trend-continuation with VWAP filter")
    print("═" * 100)

    combos = [
        ("WIN$", "M5", 500, "EMA_PULLBACK", "WIN"),
        ("WIN$", "M15", 500, "EMA_PULLBACK", "WIN"),
        ("WDO$", "M5", 500, "VWAP", "WDO"),
        ("WDO$", "M15", 500, "VWAP", "WDO"),
    ]

    all_results = []

    for sym, tf, n_bars, strat_name, param_key in combos:
        spec = CONTRACT_SPECS[sym]
        print(f"\n📡 {sym} ({spec['name']}) {tf} — Strategy: {strat_name} — {n_bars} barras...")
        df = fetch(sym, tf, n_bars)
        if df.empty:
            print("  ❌ Sem dados"); continue

        params = DEFAULT_PARAMS[param_key]
        r = backtest_with_plugin(df, sym, strat_name, params)
        if r and r["ok"]:
            print(f"\n  📊 {strat_name} — {sym} {tf}")
            print(f"  {'─' * 60}")
            print(f"  Retorno: {r['ret']:+.2f}%")
            print(f"  Trades:  {r['trades']} (Long: {r['long']}, Short: {r['short']})")
            print(f"  Win Rate: {r['wr']:.1f}%")
            print(f"  Sharpe:   {r['sharpe']:.2f}")
            print(f"  Max DD:   {r['max_dd']:.2f}%")
            print(f"  PF:       {r['pf']:.2f}")
            print(f"  Avg Win:  R$ {r['avg_win']:+.1f}")
            print(f"  Avg Loss: R$ {r['avg_loss']:+.1f}")
            print(f"  Payoff:   {r['payoff']:.2f}")
            print(f"  R$/dia:   R$ {r['avg_daily']:+.1f}")
            print(f"  Avg Barras: {r['avg_bars']:.1f}")

            print(f"\n  Motivos de saída:")
            for reason, data in sorted(r["reasons"].items(), key=lambda x: x[1]["pnl"], reverse=True):
                pct = data["count"] / r["trades"] * 100 if r["trades"] else 0
                wr_r = data["wins"] / data["count"] * 100 if data["count"] else 0
                print(f"    {reason:<6}: {data['count']:>3} ({pct:4.1f}%) WR {wr_r:.0f}% PnL R${data['pnl']:+.0f}")

            all_results.append({
                "symbol": sym, "tf": tf, "strategy": strat_name,
                **r,
            })

    # Ranking
    if all_results:
        ranking = sorted(all_results, key=lambda x: x["ret"], reverse=True)
        print("\n\n" + "═" * 100)
        print("  📋 RANKING GERAL — v7 (Plugin-Based)")
        print("═" * 100)
        print(f"\n{'#':>2} {'Ativo':<7} {'TF':<4} {'Strategy':<15} {'Ret%':>7} {'T':>4} {'WR':>6} {'Sharpe':>7} {'DD':>7} {'PF':>6} {'R$/dia':>9}")
        print("─" * 100)
        for i, r in enumerate(ranking, 1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i:>2}"
            print(f"{medal:>2} {r['symbol']:<7} {r['tf']:<4} {r['strategy']:<15} "
                  f"{r['ret']:>+6.2f}% {r['trades']:>3}  "
                  f"{r['wr']:>5.1f}% {r['sharpe']:>6.2f}  "
                  f"{r['max_dd']:>6.2f}% {r['pf']:>5.2f} "
                  f"R${r['avg_daily']:>+8.1f}")

        # Compare with baseline
        print("\n\n" + "═" * 100)
        print("  📈 COMPARAÇÃO v7 (EMA_PULLBACK/VWAP) vs v6 BASELINE (VWAP)")
        print("═" * 100)

        baseline = {
            ("WIN$", "M5"): -0.36,
            ("WIN$", "M15"): -1.20,
            ("WDO$", "M5"): 0.22,
            ("WDO$", "M15"): 0.60,
        }

        print(f"\n{'Ativo':<10} {'TF':<4} {'v6 Ret%':>8} {'v7 Ret%':>8} {'Δ':>7}")
        print("─" * 50)
        for r in all_results:
            key = (r["symbol"], r["tf"])
            v6 = baseline.get(key, 0)
            v7 = r["ret"]
            delta = v7 - v6
            icon = "✅" if delta > 0 else "❌" if delta < -0.5 else "➡️"
            print(f"  {r['symbol']:<7} {r['tf']:<4} {v6:>+7.2f}% {v7:>+7.2f}% {delta:>+6.2f}% {icon}")

        profitable = [x for x in all_results if x["ret"] > 0]
        print(f"\n  💰 Lucrativos: {len(profitable)}/{len(all_results)}")
        for p in profitable:
            print(f"    ✅ {p['symbol']} {p['tf']} — {p['strategy']} — {p['ret']:+.2f}% | "
                  f"Sharpe {p['sharpe']:.2f} | PF {p['pf']:.2f} | R${p['avg_daily']:+.0f}/dia")

    print("\n" + "═" * 100 + "\n")


if __name__ == "__main__":
    run()
