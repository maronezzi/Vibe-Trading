"""
parameter_sweep_v6.py — Sweep de parâmetros para WDO (VWAP) e WIN (EMA_CROSSOVER).
NÃO muda estratégia, apenas ajusta parâmetros dos indicadores.
"""

import sys, csv, io, subprocess, os
from pathlib import Path
from itertools import product
import numpy as np, pandas as pd

WINE_PYTHON = os.path.expanduser("~/.wine/drive_c/Python311/python.exe")
FETCH_SCRIPT = os.path.join(os.path.dirname(__file__), "mt5_fetch.py")

CLOSE_HOUR, CLOSE_MINUTE = 16, 45

CONTRACT_SPECS = {
    "WIN$": {"mult": 0.20, "name": "Mini Índice", "margin": 5000, "tick": 5, "slip_r": 1.0},
    "WDO$": {"mult": 10.0, "name": "Mini Dólar", "margin": 3000, "tick": 0.5, "slip_r": 5.0},
}
COMMISSION = 2.5

SL_MIN_WIN = 100
SL_MIN_WDO = 200


def fetch(symbol, tf, n_bars):
    cmd = ["wine", WINE_PYTHON, FETCH_SCRIPT, "rates", symbol, tf, str(n_bars)]
    env = {**os.environ, "WINEDEBUG": "-all"}
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
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


def calc_atr(df, period=14):
    h, l = df["high"], df["low"]
    c_prev = df["close"].shift(1)
    tr = pd.concat([h - l, (h - c_prev).abs(), (l - c_prev).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def calc_vwap(df, period=20):
    typical = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["tick_volume"].replace(0, 1)
    return (typical * vol).rolling(period).sum() / vol.rolling(period).sum()


def calc_ema(df, period):
    return df["close"].ewm(span=period, adjust=False).mean()


def calc_adx(df, period=14):
    h, l, c = df["high"], df["low"], df["close"]
    plus_dm = h.diff()
    minus_dm = -l.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    plus_di = 100 * (plus_dm.rolling(period).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(period).mean() / atr)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1)
    adx = dx.rolling(period).mean()
    return adx


def backtest_vwap(df, symbol, params, capital=100_000.0):
    """Backtest VWAP strategy with given parameters."""
    spec = CONTRACT_SPECS[symbol]
    mult, margin, slip_r = spec["mult"], spec["margin"], spec["slip_r"]
    is_win = "WIN" in symbol
    is_wdo = "WDO" in symbol

    atr = calc_atr(df, params.get("atr_period", 14))
    vwap = calc_vwap(df, params["vwap_period"])

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

    equity, trade_log, daily_pnl = [], [], []
    n_trades = n_wins = n_long = n_short = 0
    n_sl = n_trail = n_close = 0
    gross_win = 0.0
    gross_loss_val = 0.0
    daily_pnl_dict = {}

    last_trade_bar = -params.get("cooldown_seconds", 300) // 300  # approximate

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
        trade_log.append({"type": "LONG" if pos == 1 else "SHORT", "pnl": pnl, "reason": reason, "bars": bars_in_trade})
        daily_pnl.append(pnl)
        d = e_date.date() if hasattr(e_date, 'date') else e_date
        if d not in daily_pnl_dict:
            daily_pnl_dict[d] = 0.0
        daily_pnl_dict[d] += pnl
        pos = 0; ep = 0; best = 0; sl_price = 0; trail_on = False
        bars_in_trade = 0

    def _open(direction, price, date, cur_atr):
        nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, e_atr, sl_pts, bars_in_trade, last_trade_bar
        if pos != 0:
            return False
        raw_sl = int(cur_atr * params.get("sl_atr_mult", 1.0))
        if is_win:
            raw_sl = max(raw_sl, SL_MIN_WIN)
        elif is_wdo:
            raw_sl = max(raw_sl, SL_MIN_WDO)
        raw_sl = ((raw_sl + 4) // 5) * 5
        cost = slip_r + COMMISSION
        if cash >= margin + cost:
            cash -= margin + cost
            pos = 1 if direction == "BUY" else -1
            ep = price; e_date = date; e_atr = cur_atr; sl_pts = raw_sl
            best = price; trail_on = False
            if pos == 1:
                sl_price = price - raw_sl
            else:
                sl_price = price + raw_sl
            bars_in_trade = 0
            last_trade_bar = 0
            return True
        return False

    for i, (date, row) in enumerate(df.iterrows()):
        price = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        hour = int(row["hour"])
        minute = int(row["minute"])
        cur_atr = float(atr.iloc[i]) if i > 0 and not pd.isna(atr.iloc[i]) else 0
        cur_vwap = float(vwap.iloc[i]) if not pd.isna(vwap.iloc[i]) else 0

        if pos == 1:
            eq_val = cash + (price - ep) * mult + margin
        elif pos == -1:
            eq_val = cash + (ep - price) * mult + margin
        else:
            eq_val = cash
        equity.append(eq_val)

        if pos == 0:
            if cur_atr > 0 and cur_vwap > 0:
                buy_thresh = cur_vwap * params.get("vwap_buy_threshold", 1.003)
                sell_thresh = cur_vwap * params.get("vwap_sell_threshold", 0.997)
                if price > buy_thresh:
                    _open("BUY", price, date, cur_atr)
                elif price < sell_thresh:
                    _open("SELL", price, date, cur_atr)
            continue

        bars_in_trade += 1
        if pos == 1:
            best = max(best, high)
        elif pos == -1:
            best = min(best, low) if best > 0 else low

        if pos == 1:
            profit_pts = best - ep
        else:
            profit_pts = ep - best

        if not trail_on and e_atr > 0 and profit_pts >= params.get("trail_activate", 1.5) * e_atr:
            trail_on = True

        if trail_on and e_atr > 0:
            trail_dist = params.get("trail_distance", 0.5) * e_atr
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
        _close(float(df["close"].iloc[-1]), "FORCE")

    eq = pd.Series(equity, index=df.index[:len(equity)])
    total_ret = (cash - capital) / capital * 100
    n_days = df["date"].nunique()
    daily_vals = list(daily_pnl_dict.values())
    avg_daily = sum(t["pnl"] for t in trade_log) / n_days if n_days else 0

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

    return {
        "trades": n_trades, "wr": wr, "ret": total_ret, "sharpe": sharpe,
        "max_dd": max_dd, "pf": pf, "avg_daily": avg_daily,
        "n_days": n_days, "n_long": n_long, "n_short": n_short,
    }


def backtest_ema(df, symbol, params, capital=100_000.0):
    """Backtest EMA CROSSOVER strategy with given parameters."""
    spec = CONTRACT_SPECS[symbol]
    mult, margin, slip_r = spec["mult"], spec["margin"], spec["slip_r"]
    is_win = "WIN" in symbol

    atr = calc_atr(df, params.get("atr_period", 14))
    ema_fast = calc_ema(df, params["ema_fast"])
    ema_slow = calc_ema(df, params["ema_slow"])
    adx = calc_adx(df, params.get("adx_period", 14))
    rsi_period = params.get("rsi_period", 14)
    rsi = df["close"].diff().rolling(rsi_period).apply(lambda x: (x > 0).sum() / len(x) * 100, raw=True)

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
        trade_log.append({"type": "LONG" if pos == 1 else "SHORT", "pnl": pnl, "reason": reason, "bars": bars_in_trade})
        daily_pnl.append(pnl)
        d = e_date.date() if hasattr(e_date, 'date') else e_date
        if d not in daily_pnl_dict:
            daily_pnl_dict[d] = 0.0
        daily_pnl_dict[d] += pnl
        pos = 0; ep = 0; best = 0; sl_price = 0; trail_on = False
        bars_in_trade = 0

    def _open(direction, price, date, cur_atr):
        nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, e_atr, sl_pts, bars_in_trade
        if pos != 0:
            return False
        raw_sl = int(cur_atr * params.get("sl_atr_mult", 1.5))
        if is_win:
            raw_sl = max(raw_sl, SL_MIN_WIN)
        raw_sl = ((raw_sl + 4) // 5) * 5
        cost = slip_r + COMMISSION
        if cash >= margin + cost:
            cash -= margin + cost
            pos = 1 if direction == "BUY" else -1
            ep = price; e_date = date; e_atr = cur_atr; sl_pts = raw_sl
            best = price; trail_on = False
            if pos == 1:
                sl_price = price - raw_sl
            else:
                sl_price = price + raw_sl
            bars_in_trade = 0
            return True
        return False

    prev_ema_fast = None
    prev_ema_slow = None

    for i, (date, row) in enumerate(df.iterrows()):
        price = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        hour = int(row["hour"])
        minute = int(row["minute"])
        cur_atr = float(atr.iloc[i]) if i > 0 and not pd.isna(atr.iloc[i]) else 0
        cur_ema_fast = float(ema_fast.iloc[i]) if not pd.isna(ema_fast.iloc[i]) else 0
        cur_ema_slow = float(ema_slow.iloc[i]) if not pd.isna(ema_slow.iloc[i]) else 0
        cur_adx = float(adx.iloc[i]) if not pd.isna(adx.iloc[i]) else 0

        if pos == 1:
            eq_val = cash + (price - ep) * mult + margin
        elif pos == -1:
            eq_val = cash + (ep - price) * mult + margin
        else:
            eq_val = cash
        equity.append(eq_val)

        if pos == 0 and prev_ema_fast is not None and prev_ema_slow is not None:
            adx_threshold = params.get("adx_threshold", 15)
            if cur_adx >= adx_threshold and cur_atr > 0:
                # Crossover: fast crosses above slow = BUY, fast crosses below slow = SELL
                if prev_ema_fast <= prev_ema_slow and cur_ema_fast > cur_ema_slow:
                    _open("BUY", price, date, cur_atr)
                elif prev_ema_fast >= prev_ema_slow and cur_ema_fast < cur_ema_slow:
                    _open("SELL", price, date, cur_atr)

        prev_ema_fast = cur_ema_fast
        prev_ema_slow = cur_ema_slow

        if pos == 0:
            continue

        bars_in_trade += 1
        if pos == 1:
            best = max(best, high)
        elif pos == -1:
            best = min(best, low) if best > 0 else low

        if pos == 1:
            profit_pts = best - ep
        else:
            profit_pts = ep - best

        if not trail_on and e_atr > 0 and profit_pts >= params.get("trail_activate", 1.0) * e_atr:
            trail_on = True

        if trail_on and e_atr > 0:
            trail_dist = params.get("trail_distance", 0.2) * e_atr
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
        _close(float(df["close"].iloc[-1]), "FORCE")

    eq = pd.Series(equity, index=df.index[:len(equity)])
    total_ret = (cash - capital) / capital * 100
    n_days = df["date"].nunique()
    daily_vals = list(daily_pnl_dict.values())
    avg_daily = sum(t["pnl"] for t in trade_log) / n_days if n_days else 0

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

    return {
        "trades": n_trades, "wr": wr, "ret": total_ret, "sharpe": sharpe,
        "max_dd": max_dd, "pf": pf, "avg_daily": avg_daily,
        "n_days": n_days, "n_long": n_long, "n_short": n_short,
    }


def score_result(r):
    """Score a backtest result. Higher = better."""
    if r["trades"] < 3:
        return -999  # too few trades
    # Weighted score: Sharpe (40%) + PF (30%) + Return (20%) + WR (10%)
    # Penalize high drawdown
    dd_penalty = max(0, r["max_dd"] - 2.0) * 5
    return (
        r["sharpe"] * 0.4 +
        min(r["pf"], 10) * 3 * 0.3 +
        r["ret"] * 10 * 0.2 +
        r["wr"] * 0.1 -
        dd_penalty
    )


def run():
    print("\n" + "═" * 100)
    print("  🔬 PARAMETER SWEEP — WDO (VWAP) & WIN (EMA_CROSSOVER)")
    print("  " + "─" * 96)
    print("  Sweep de parâmetros ±20% dos valores atuais")
    print("═" * 100)

    # Fetch data
    print("\n📡 Fetching data...")
    data = {}
    combos = [("WIN$", "M5", 500), ("WDO$", "M5", 500)]
    for sym, tf, n_bars in combos:
        df = fetch(sym, tf, n_bars)
        if not df.empty:
            data[(sym, tf)] = df
            print(f"  ✅ {sym} {tf}: {len(df)} barras, {df['date'].nunique()} dias")
        else:
            print(f"  ❌ {sym} {tf}: sem dados")

    # ===== WDO VWAP SWEEP =====
    print("\n\n" + "─" * 100)
    print("  📊 WDO$ M5 — VWAP Parameter Sweep")
    print("─" * 100)

    wdo_param_grid = {
        "vwap_period": [10, 15, 20, 25],
        "vwap_buy_threshold": [1.000, 1.001, 1.002, 1.003, 1.005],
        "vwap_sell_threshold": [0.995, 0.997, 0.998, 0.999, 1.000],
        "sl_atr_mult": [0.8, 1.0, 1.2, 1.5],
        "trail_activate": [1.0, 1.5, 2.0],
        "trail_distance": [0.2, 0.3, 0.5],
    }

    # Current WDO params (baseline)
    wdo_baseline = {
        "vwap_period": 15,
        "vwap_buy_threshold": 1.001,
        "vwap_sell_threshold": 0.999,
        "sl_atr_mult": 1.0,
        "trail_activate": 1.5,
        "trail_distance": 0.2,
    }

    if ("WDO$", "M5") in data:
        df_wdo = data[("WDO$", "M5")]
        # Run baseline
        baseline_r = backtest_vwap(df_wdo, "WDO$", wdo_baseline)
        baseline_score = score_result(baseline_r)
        print(f"\n  📏 BASELINE: ret={baseline_r['ret']:+.2f}% WR={baseline_r['wr']:.1f}% Sharpe={baseline_r['sharpe']:.2f} PF={baseline_r['pf']:.2f} DD={baseline_r['max_dd']:.2f}% Score={baseline_score:.2f}")

        # Sweep: one param at a time from grid (faster than full grid)
        best_wdo_params = wdo_baseline.copy()
        best_wdo_score = baseline_score
        best_wdo_result = baseline_r

        for param_name, values in wdo_param_grid.items():
            print(f"\n  🔍 Sweeping {param_name}: {values}")
            for val in values:
                test_params = best_wdo_params.copy()
                test_params[param_name] = val
                r = backtest_vwap(df_wdo, "WDO$", test_params)
                s = score_result(r)
                marker = " ← best" if s > best_wdo_score else ""
                if s > best_wdo_score:
                    best_wdo_params[param_name] = val
                    best_wdo_score = s
                    best_wdo_result = r
                print(f"    {param_name}={val}: ret={r['ret']:+.2f}% WR={r['wr']:.1f}% Sharpe={r['sharpe']:.2f} PF={r['pf']:.2f} Score={s:.2f}{marker}")

        # Also test combined best
        print(f"\n  🏆 BEST WDO PARAMS: {best_wdo_params}")
        print(f"  🏆 BEST WDO RESULT: ret={best_wdo_result['ret']:+.2f}% WR={best_wdo_result['wr']:.1f}% Sharpe={best_wdo_result['sharpe']:.2f} PF={best_wdo_result['pf']:.2f} DD={best_wdo_result['max_dd']:.2f}%")
        delta_ret = best_wdo_result['ret'] - baseline_r['ret']
        delta_sharpe = best_wdo_result['sharpe'] - baseline_r['sharpe']
        print(f"  📈 Δ Ret: {delta_ret:+.2f}% | Δ Sharpe: {delta_sharpe:+.2f}")

    # ===== WIN EMA CROSSOVER SWEEP =====
    print("\n\n" + "─" * 100)
    print("  📊 WIN$ M5 — EMA CROSSOVER Parameter Sweep")
    print("─" * 100)

    win_param_grid = {
        "ema_fast": [8, 10, 12, 15, 20],
        "ema_slow": [15, 21, 26, 30],
        "adx_threshold": [10, 15, 20, 25, 30],
        "sl_atr_mult": [1.0, 1.5, 2.0, 2.5],
        "trail_activate": [0.5, 1.0, 1.5, 2.0],
        "trail_distance": [0.1, 0.2, 0.3, 0.5],
    }

    win_baseline = {
        "ema_fast": 12,
        "ema_slow": 21,
        "adx_period": 14,
        "adx_threshold": 15,
        "rsi_period": 14,
        "sl_atr_mult": 1.5,
        "trail_activate": 1.0,
        "trail_distance": 0.2,
    }

    if ("WIN$", "M5") in data:
        df_win = data[("WIN$", "M5")]
        # Run baseline
        baseline_r_win = backtest_ema(df_win, "WIN$", win_baseline)
        baseline_score_win = score_result(baseline_r_win)
        print(f"\n  📏 BASELINE: ret={baseline_r_win['ret']:+.2f}% WR={baseline_r_win['wr']:.1f}% Sharpe={baseline_r_win['sharpe']:.2f} PF={baseline_r_win['pf']:.2f} DD={baseline_r_win['max_dd']:.2f}% Score={baseline_score_win:.2f}")

        best_win_params = win_baseline.copy()
        best_win_score = baseline_score_win
        best_win_result = baseline_r_win

        for param_name, values in win_param_grid.items():
            print(f"\n  🔍 Sweeping {param_name}: {values}")
            for val in values:
                test_params = best_win_params.copy()
                test_params[param_name] = val
                r = backtest_ema(df_win, "WIN$", test_params)
                s = score_result(r)
                marker = " ← best" if s > best_win_score else ""
                if s > best_win_score:
                    best_win_params[param_name] = val
                    best_win_score = s
                    best_win_result = r
                print(f"    {param_name}={val}: ret={r['ret']:+.2f}% WR={r['wr']:.1f}% Sharpe={r['sharpe']:.2f} PF={r['pf']:.2f} Score={s:.2f}{marker}")

        print(f"\n  🏆 BEST WIN PARAMS: {best_win_params}")
        print(f"  🏆 BEST WIN RESULT: ret={best_win_result['ret']:+.2f}% WR={best_win_result['wr']:.1f}% Sharpe={best_win_result['sharpe']:.2f} PF={best_win_result['pf']:.2f} DD={best_win_result['max_dd']:.2f}%")
        delta_ret_win = best_win_result['ret'] - baseline_r_win['ret']
        delta_sharpe_win = best_win_result['sharpe'] - baseline_r_win['sharpe']
        print(f"  📈 Δ Ret: {delta_ret_win:+.2f}% | Δ Sharpe: {delta_sharpe_win:+.2f}")

    # ===== SUMMARY =====
    print("\n\n" + "═" * 100)
    print("  📋 SWEEP SUMMARY — Parameters to Update")
    print("═" * 100)

    if ("WDO$", "M5") in data:
        print(f"\n  WDO$ M5 (VWAP):")
        for k in wdo_param_grid:
            old = wdo_baseline[k]
            new = best_wdo_params[k]
            changed = "🔄" if old != new else "  "
            print(f"    {changed} {k}: {old} → {new}")
        print(f"    📈 Ret: {baseline_r['ret']:+.2f}% → {best_wdo_result['ret']:+.2f}% ({best_wdo_result['ret']-baseline_r['ret']:+.2f}%)")
        print(f"    📈 Sharpe: {baseline_r['sharpe']:.2f} → {best_wdo_result['sharpe']:.2f}")

    if ("WIN$", "M5") in data:
        print(f"\n  WIN$ M5 (EMA_CROSSOVER):")
        for k in win_param_grid:
            old = win_baseline[k]
            new = best_win_params[k]
            changed = "🔄" if old != new else "  "
            print(f"    {changed} {k}: {old} → {new}")
        print(f"    📈 Ret: {baseline_r_win['ret']:+.2f}% → {best_win_result['ret']:+.2f}% ({best_win_result['ret']-baseline_r_win['ret']:+.2f}%)")
        print(f"    📈 Sharpe: {baseline_r_win['sharpe']:.2f} → {best_win_result['sharpe']:.2f}")

    print("\n" + "═" * 100 + "\n")

    return {
        "wdo_baseline": wdo_baseline, "wdo_best": best_wdo_params,
        "wdo_baseline_result": baseline_r if ("WDO$", "M5") in data else None,
        "wdo_best_result": best_wdo_result if ("WDO$", "M5") in data else None,
        "win_baseline": win_baseline, "win_best": best_win_params,
        "win_baseline_result": baseline_r_win if ("WIN$", "M5") in data else None,
        "win_best_result": best_win_result if ("WIN$", "M5") in data else None,
    }


if __name__ == "__main__":
    results = run()
