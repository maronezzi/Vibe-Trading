"""
backtest_v7_optimize.py — Parameter Sweep for Split Strategy
Optimize WDO (VWAP) and WIN (EMA Cross + ADX) independently.
PARALLEL version — uses all CPU cores via multiprocessing.
"""

import sys, csv, io, subprocess, os
from pathlib import Path
from itertools import product
from multiprocessing import Pool, cpu_count
import numpy as np, pandas as pd

WINE_PYTHON = os.path.expanduser("~/.wine/drive_c/Python311/python.exe")
FETCH_SCRIPT = os.path.join(os.path.dirname(__file__), "mt5_fetch.py")
CLOSE_HOUR, CLOSE_MINUTE = 16, 45

CONTRACT_SPECS = {
    "WIN$": {"mult": 0.20, "name": "Mini Indice", "margin": 5000, "tick": 5, "slip_r": 1.0},
    "WDO$": {"mult": 10.0, "name": "Mini Dolar", "margin": 3000, "tick": 0.5, "slip_r": 5.0},
}
COMMISSION = 2.5
ATR_PERIOD = 14
MAX_CT = 1


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
    tr = pd.concat([h-l, (h-c_prev).abs(), (l-c_prev).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def calc_vwap(df, period=20):
    typical = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["tick_volume"].replace(0, 1)
    return (typical * vol).rolling(period).sum() / vol.rolling(period).sum()

def calc_rsi(df, period=14):
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    # Evitar divisão por zero
    loss = loss.replace(0, 1e-10)
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calc_adx(df, period=14):
    h, l, c = df["high"], df["low"], df["close"]
    plus_dm = h.diff()
    minus_dm = -l.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)
    tr = pd.concat([h-l, (h-c.shift(1)).abs(), (l-c.shift(1)).abs()], axis=1).max(axis=1)
    atr_smooth = tr.rolling(period).mean()
    # Evitar divisão por zero
    atr_smooth = atr_smooth.replace(0, 1e-10)
    plus_di = 100 * (plus_dm.rolling(period).mean() / atr_smooth)
    minus_di = 100 * (minus_dm.rolling(period).mean() / atr_smooth)
    di_sum = plus_di + minus_di
    di_sum = di_sum.replace(0, 1e-10)
    dx = 100 * ((plus_di - minus_di).abs() / di_sum)
    adx = dx.rolling(period).mean()
    return adx.fillna(0), plus_di.fillna(0), minus_di.fillna(0)
def calc_ema(df, period):
    return df["close"].ewm(span=period, adjust=False).mean()


def backtest_wdo_vwap(df, params, capital=100000.0):
    spec = CONTRACT_SPECS["WDO$"]
    mult, margin, slip_r = spec["mult"], spec["margin"], spec["slip_r"]

    atr = calc_atr(df, ATR_PERIOD)
    vwap = calc_vwap(df, params["vwap_period"])
    ema_f = calc_ema(df, params["ema_fast"])
    ema_s = calc_ema(df, params["ema_slow"])
    rsi = calc_rsi(df, 14)

    cash = capital; pos = 0; ep = 0; e_date = None; e_atr = 0
    best = 0; sl_price = 0; trail_on = False; sl_pts = 0; bars_in_trade = 0
    last_trade_ts = None; daily_trade_count = 0; current_date = None

    equity, trade_log, daily_pnl = [], [], []
    n_trades = n_wins = n_long = n_short = 0
    n_sl = n_trail = n_close = 0
    gross_win = 0; gross_loss_val = 0
    daily_pnl_dict = {}

    def _close(price, reason):
        nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, e_atr
        nonlocal n_trades, n_wins, n_long, n_short, n_sl, n_trail, n_close
        nonlocal gross_win, gross_loss_val, bars_in_trade
        if pos == 0: return
        sl_cost = slip_r * MAX_CT; comm = COMMISSION * MAX_CT
        if pos == 1:
            pnl = (price - ep) * mult * MAX_CT - sl_cost - comm; n_long += 1
        else:
            pnl = (ep - price) * mult * MAX_CT - sl_cost - comm; n_short += 1
        cash += margin * MAX_CT + pnl; n_trades += 1
        if reason == "SL": n_sl += 1
        elif reason == "TRAIL": n_trail += 1
        elif reason == "1645": n_close += 1
        if pnl > 0: n_wins += 1; gross_win += pnl
        else: gross_loss_val += abs(pnl)
        trade_log.append({"type": "LONG" if pos == 1 else "SHORT", "ep": ep, "xp": price, "pnl": pnl, "reason": reason, "bars": bars_in_trade})
        daily_pnl.append(pnl)
        d = e_date.date() if hasattr(e_date, 'date') else e_date
        if d not in daily_pnl_dict: daily_pnl_dict[d] = 0.0
        daily_pnl_dict[d] += pnl
        pos = 0; ep = 0; best = 0; sl_price = 0; trail_on = False; bars_in_trade = 0

    def _open(direction, price, date, cur_atr):
        nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, e_atr, sl_pts, bars_in_trade, daily_trade_count, last_trade_ts
        if pos != 0: return False
        raw_sl = int(cur_atr * params["sl_atr_mult"])
        raw_sl = max(raw_sl, 200)
        raw_sl = ((raw_sl + 4) // 5) * 5
        cost = slip_r * MAX_CT + COMMISSION * MAX_CT
        if cash >= margin * MAX_CT + cost:
            cash -= margin * MAX_CT + cost
            pos = 1 if direction == "BUY" else -1
            ep = price; e_date = date; e_atr = cur_atr; sl_pts = raw_sl
            best = price; trail_on = False; bars_in_trade = 0
            sl_price = price - raw_sl if pos == 1 else price + raw_sl
            daily_trade_count += 1; last_trade_ts = date
            return True
        return False

    for i, (date, row) in enumerate(df.iterrows()):
        price = float(row["close"]); high = float(row["high"]); low = float(row["low"])
        hour = int(row["hour"]); minute = int(row["minute"])
        cur_atr = float(atr.iloc[i]) if i > 0 and not pd.isna(atr.iloc[i]) else 0
        cur_vwap = float(vwap.iloc[i]) if not pd.isna(vwap.iloc[i]) else 0
        cur_rsi = float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else 50
        cur_ema_f = float(ema_f.iloc[i]) if not pd.isna(ema_f.iloc[i]) else 0
        cur_ema_s = float(ema_s.iloc[i]) if not pd.isna(ema_s.iloc[i]) else 0

        _cd = date.date() if hasattr(date, 'date') else date
        if current_date != _cd: current_date = _cd; daily_trade_count = 0

        cur_min = hour * 60 + minute
        safe = not ((9*60+5 <= cur_min <= 9*60+20) or (16*60+30 <= cur_min <= 16*60+45))
        if not safe:
            if pos == 1: equity.append(cash + (price - ep) * mult * MAX_CT + margin * MAX_CT)
            elif pos == -1: equity.append(cash + (ep - price) * mult * MAX_CT + margin * MAX_CT)
            else: equity.append(cash)
            continue

        if pos == 1: eq_val = cash + (price - ep) * mult * MAX_CT + margin * MAX_CT
        elif pos == -1: eq_val = cash + (ep - price) * mult * MAX_CT + margin * MAX_CT
        else: eq_val = cash
        equity.append(eq_val)

        if pos == 0:
            if cur_atr > 0 and cur_vwap > 0 and daily_trade_count < params["max_daily"]:
                if cur_ema_f > 0 and cur_ema_s > 0:
                    spread = abs(cur_ema_f - cur_ema_s) / price if price > 0 else 0
                    if spread < params.get("trend_min_spread", 0.001): continue
                atr_pct = cur_atr / price if price > 0 else 0
                if atr_pct < 0.0015: buy_mult = 1.0005; sell_mult = 0.9995
                elif atr_pct < 0.003: buy_mult = 1.0015; sell_mult = 0.9985
                else: buy_mult = params["buy_thresh"]; sell_mult = params["sell_thresh"]
                direction = None
                if price > cur_vwap * buy_mult: direction = "BUY"
                elif price < cur_vwap * sell_mult: direction = "SELL"
                if direction:
                    if cur_ema_f > 0 and cur_ema_s > 0:
                        if direction == "BUY" and cur_ema_f < cur_ema_s: continue
                        if direction == "SELL" and cur_ema_f > cur_ema_s: continue
                    if direction == "BUY" and cur_rsi > 70: continue
                    if direction == "SELL" and cur_rsi < 30: continue
                    if last_trade_ts and (date - last_trade_ts).total_seconds() < params["cooldown"]: continue
                    _open(direction, price, date, cur_atr)
            continue

        bars_in_trade += 1
        if pos == 1: best = max(best, high)
        elif pos == -1: best = min(best, low) if best > 0 else low
        profit_pts = (best - ep) if pos == 1 else (ep - best)

        if not trail_on and e_atr > 0 and profit_pts >= params["trail_act"] * e_atr:
            trail_on = True
        if trail_on and e_atr > 0:
            td = params["trail_dist"] * e_atr
            if pos == 1:
                nsl = best - td
                if nsl > sl_price: sl_price = nsl
            else:
                nsl = best + td
                if nsl < sl_price: sl_price = nsl
        if sl_price > 0:
            if pos == 1 and low <= sl_price: _close(sl_price, "SL"); continue
            elif pos == -1 and high >= sl_price: _close(sl_price, "SL"); continue
        if hour > CLOSE_HOUR or (hour == CLOSE_HOUR and minute >= CLOSE_MINUTE):
            _close(price, "1645"); continue

    if pos != 0: _close(float(df["close"].iloc[-1]), "FORCE")
    return _stats(equity, trade_log, daily_pnl_dict, cash, capital, n_trades, n_wins, n_long, n_short, n_sl, n_trail, n_close, gross_win, gross_loss_val)


def backtest_win_ema_cross(df, params, capital=100000.0):
    spec = CONTRACT_SPECS["WIN$"]
    mult, margin, slip_r = spec["mult"], spec["margin"], spec["slip_r"]

    atr = calc_atr(df, ATR_PERIOD)
    ema_f = calc_ema(df, params["ema_fast"])
    ema_s = calc_ema(df, params["ema_slow"])
    adx, plus_di, minus_di = calc_adx(df, params["adx_period"])
    rsi = calc_rsi(df, params["rsi_period"])

    cash = capital; pos = 0; ep = 0; e_date = None; e_atr = 0
    best = 0; sl_price = 0; trail_on = False; sl_pts = 0; bars_in_trade = 0
    last_trade_ts = None; daily_trade_count = 0; current_date = None

    equity, trade_log, daily_pnl = [], [], []
    n_trades = n_wins = n_long = n_short = 0
    n_sl = n_trail = n_close = 0
    gross_win = 0; gross_loss_val = 0
    daily_pnl_dict = {}

    def _close(price, reason):
        nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, e_atr
        nonlocal n_trades, n_wins, n_long, n_short, n_sl, n_trail, n_close
        nonlocal gross_win, gross_loss_val, bars_in_trade
        if pos == 0: return
        sl_cost = slip_r * MAX_CT; comm = COMMISSION * MAX_CT
        if pos == 1:
            pnl = (price - ep) * mult * MAX_CT - sl_cost - comm; n_long += 1
        else:
            pnl = (ep - price) * mult * MAX_CT - sl_cost - comm; n_short += 1
        cash += margin * MAX_CT + pnl; n_trades += 1
        if reason == "SL": n_sl += 1
        elif reason == "TRAIL": n_trail += 1
        elif reason == "1645": n_close += 1
        if pnl > 0: n_wins += 1; gross_win += pnl
        else: gross_loss_val += abs(pnl)
        trade_log.append({"type": "LONG" if pos == 1 else "SHORT", "ep": ep, "xp": price, "pnl": pnl, "reason": reason, "bars": bars_in_trade})
        daily_pnl.append(pnl)
        d = e_date.date() if hasattr(e_date, 'date') else e_date
        if d not in daily_pnl_dict: daily_pnl_dict[d] = 0.0
        daily_pnl_dict[d] += pnl
        pos = 0; ep = 0; best = 0; sl_price = 0; trail_on = False; bars_in_trade = 0

    def _open(direction, price, date, cur_atr):
        nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, e_atr, sl_pts, bars_in_trade, daily_trade_count, last_trade_ts
        if pos != 0: return False
        raw_sl = int(cur_atr * params["sl_atr_mult"])
        raw_sl = max(raw_sl, 200)
        raw_sl = ((raw_sl + 4) // 5) * 5
        cost = slip_r * MAX_CT + COMMISSION * MAX_CT
        if cash >= margin * MAX_CT + cost:
            cash -= margin * MAX_CT + cost
            pos = 1 if direction == "BUY" else -1
            ep = price; e_date = date; e_atr = cur_atr; sl_pts = raw_sl
            best = price; trail_on = False; bars_in_trade = 0
            sl_price = price - raw_sl if pos == 1 else price + raw_sl
            daily_trade_count += 1; last_trade_ts = date
            return True
        return False

    for i, (date, row) in enumerate(df.iterrows()):
        price = float(row["close"]); high = float(row["high"]); low = float(row["low"])
        hour = int(row["hour"]); minute = int(row["minute"])
        cur_atr = float(atr.iloc[i]) if i > 0 and not pd.isna(atr.iloc[i]) else 0
        cur_ema_f = float(ema_f.iloc[i]) if not pd.isna(ema_f.iloc[i]) else 0
        cur_ema_s = float(ema_s.iloc[i]) if not pd.isna(ema_s.iloc[i]) else 0
        cur_adx = float(adx.iloc[i]) if not pd.isna(adx.iloc[i]) else 0
        cur_plus_di = float(plus_di.iloc[i]) if not pd.isna(plus_di.iloc[i]) else 0
        cur_minus_di = float(minus_di.iloc[i]) if not pd.isna(minus_di.iloc[i]) else 0
        cur_rsi = float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else 50
        prev_ema_f = float(ema_f.iloc[i-1]) if i > 0 and not pd.isna(ema_f.iloc[i-1]) else cur_ema_f
        prev_ema_s = float(ema_s.iloc[i-1]) if i > 0 and not pd.isna(ema_s.iloc[i-1]) else cur_ema_s

        _cd = date.date() if hasattr(date, 'date') else date
        if current_date != _cd: current_date = _cd; daily_trade_count = 0

        cur_min = hour * 60 + minute
        safe = not ((9*60+5 <= cur_min <= 9*60+20) or (16*60+30 <= cur_min <= 16*60+45))
        if not safe:
            if pos == 1: equity.append(cash + (price - ep) * mult * MAX_CT + margin * MAX_CT)
            elif pos == -1: equity.append(cash + (ep - price) * mult * MAX_CT + margin * MAX_CT)
            else: equity.append(cash)
            continue

        if pos == 1: eq_val = cash + (price - ep) * mult * MAX_CT + margin * MAX_CT
        elif pos == -1: eq_val = cash + (ep - price) * mult * MAX_CT + margin * MAX_CT
        else: eq_val = cash
        equity.append(eq_val)

        if pos == 0:
            if cur_atr > 0 and cur_ema_f > 0 and cur_ema_s > 0 and cur_adx > 0:
                if cur_adx < params["adx_threshold"]: continue
                if daily_trade_count >= params["max_daily"]: continue
                direction = None
                if prev_ema_f <= prev_ema_s and cur_ema_f > cur_ema_s: direction = "BUY"
                elif prev_ema_f >= prev_ema_s and cur_ema_f < cur_ema_s: direction = "SELL"
                if not direction: continue
                if direction == "BUY" and cur_rsi > params["rsi_ob"]: continue
                if direction == "SELL" and cur_rsi < params["rsi_os"]: continue
                if direction == "BUY" and cur_plus_di < cur_minus_di: continue
                if direction == "SELL" and cur_minus_di < cur_plus_di: continue
                if last_trade_ts and (date - last_trade_ts).total_seconds() < params["cooldown"]: continue
                _open(direction, price, date, cur_atr)
            continue

        bars_in_trade += 1
        if pos == 1: best = max(best, high)
        elif pos == -1: best = min(best, low) if best > 0 else low
        profit_pts = (best - ep) if pos == 1 else (ep - best)

        if not trail_on and e_atr > 0 and profit_pts >= params["trail_act"] * e_atr:
            trail_on = True
        if trail_on and e_atr > 0:
            td = params["trail_dist"] * e_atr
            if pos == 1:
                nsl = best - td
                if nsl > sl_price: sl_price = nsl
            else:
                nsl = best + td
                if nsl < sl_price: sl_price = nsl
        if sl_price > 0:
            if pos == 1 and low <= sl_price: _close(sl_price, "SL"); continue
            elif pos == -1 and high >= sl_price: _close(sl_price, "SL"); continue
        if hour > CLOSE_HOUR or (hour == CLOSE_HOUR and minute >= CLOSE_MINUTE):
            _close(price, "1645"); continue

    if pos != 0: _close(float(df["close"].iloc[-1]), "FORCE")
    return _stats(equity, trade_log, daily_pnl_dict, cash, capital, n_trades, n_wins, n_long, n_short, n_sl, n_trail, n_close, gross_win, gross_loss_val)


def _stats(equity, trade_log, daily_pnl_dict, cash, capital, n_trades, n_wins, n_long, n_short, n_sl, n_trail, n_close, gross_win, gross_loss_val):
    total_ret = (cash - capital) / capital * 100
    n_days = len(daily_pnl_dict) if daily_pnl_dict else 1
    daily_vals = list(daily_pnl_dict.values())
    if len(daily_vals) > 1:
        sharpe = np.mean(daily_vals) / np.std(daily_vals) * np.sqrt(252) if np.std(daily_vals) > 0 else 0
    else:
        sharpe = 0
    eq_arr = np.array(equity) if equity else np.array([capital])
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
    return {
        "ok": True, "trades": n_trades, "wins": n_wins, "wr": wr,
        "long": n_long, "short": n_short,
        "ret": total_ret, "sharpe": sharpe, "max_dd": max_dd,
        "pf": pf, "avg_daily": avg_daily, "n_days": n_days,
        "avg_win": avg_win, "avg_loss": avg_loss, "payoff": payoff,
        "n_sl": n_sl, "n_trail": n_trail, "n_close": n_close,
    }


# ─── Parallel worker functions (must be top-level for pickle) ────
def _wdo_worker(args):
    """Worker para backtest WDO paralelo."""
    sym_df, sym_name, p = args
    if sym_df.empty:
        return None
    r = backtest_wdo_vwap(sym_df, p)
    if r["ok"] and r["trades"] >= 3:
        score = r["ret"] * 0.3 + r["pf"] * 0.3 + r["wr"] * 0.2 + min(r["sharpe"], 50) * 0.2
        return {"sym_tf": sym_name, "params": p, "result": r, "score": score}
    return None


def _win_worker(args):
    """Worker para backtest WIN paralelo."""
    sym_df, sym_name, p = args
    if sym_df.empty:
        return None
    r = backtest_win_ema_cross(sym_df, p)
    if r["ok"] and r["trades"] >= 3:
        score = r["ret"] * 0.3 + r["pf"] * 0.3 + r["wr"] * 0.2 + min(r["sharpe"], 50) * 0.2
        return {"sym_tf": sym_name, "params": p, "result": r, "score": score}
    return None


def run():
    print("\n" + "="*100)
    print("  PARAMETER SWEEP — WDO (VWAP) + WIN (EMA Cross + ADX)")
    print("="*100)

    # Fetch data once
    wdo_m5 = fetch("WDO$", "M5", 500)
    wdo_m15 = fetch("WDO$", "M15", 500)
    win_m5 = fetch("WIN$", "M5", 500)
    win_m15 = fetch("WIN$", "M15", 500)

    # ===== WDO SWEEP =====
    print("\n\n--- WDO$ PARAMETER SWEEP ---")
    wdo_param_grid = list(product(
        [15, 20, 30],           # vwap_period
        [1.001, 1.003, 1.005],  # buy_thresh
        [0.995, 0.997, 0.999],  # sell_thresh
        [0.8, 1.0, 1.2],        # sl_atr_mult
        [1.0, 1.5, 2.0],        # trail_act
        [0.2, 0.3, 0.5],        # trail_dist
        [300, 600, 900],         # cooldown
        [6, 8, 10],              # max_daily
    ))
    # Too many combos (3^8 = 6561). Use smart subset:
    wdo_params_list = []
    base_wdo = {"ema_fast": 9, "ema_slow": 21, "trend_min_spread": 0.001}
    for vp in [15, 20, 30]:
        for bt in [1.001, 1.003, 1.005]:
            for sl in [0.8, 1.0, 1.2]:
                for ta in [1.0, 1.5]:
                    for td in [0.2, 0.3, 0.5]:
                        for cd in [300, 600]:
                            wdo_params_list.append({
                                **base_wdo,
                                "vwap_period": vp,
                                "buy_thresh": bt,
                                "sell_thresh": 2.0 - bt,  # symmetric
                                "sl_atr_mult": sl,
                                "trail_act": ta,
                                "trail_dist": td,
                                "cooldown": cd,
                                "max_daily": 8,
                            })

    print(f"  Testing {len(wdo_params_list)} WDO parameter combinations on {cpu_count()} CPUs...")
    # Build task list: each combo × each timeframe
    wdo_tasks = []
    for p in wdo_params_list:
        for sym_df, sym_tf, sym_name in [(wdo_m5, "M5", "WDO$ M5"), (wdo_m15, "M15", "WDO$ M15")]:
            wdo_tasks.append((sym_df, sym_name, p))
    # Parallel execution
    with Pool(processes=cpu_count()) as pool:
        wdo_results = [r for r in pool.map(_wdo_worker, wdo_tasks) if r is not None]

    wdo_results.sort(key=lambda x: x["score"], reverse=True)
    print(f"\n  TOP 5 WDO RESULTS:")
    for i, wr in enumerate(wdo_results[:5]):
        r = wr["result"]
        p = wr["params"]
        print(f"  #{i+1} [{wr['sym_tf']}] Score={wr['score']:.2f} | Ret={r['ret']:+.2f}% WR={r['wr']:.0f}% PF={r['pf']:.2f} Sharpe={r['sharpe']:.1f} DD={r['max_dd']:.2f}% Trades={r['trades']}")
        print(f"       VWAP={p['vwap_period']} Buy={p['buy_thresh']} Sell={p['sell_thresh']} SL={p['sl_atr_mult']}x Trail={p['trail_act']}/{p['trail_dist']} CD={p['cooldown']}s")

    # ===== WIN SWEEP =====
    print("\n\n--- WIN$ PARAMETER SWEEP ---")
    win_params_list = []
    for ef in [5, 8, 9, 12]:
        for es in [13, 21, 26]:
            if ef >= es: continue
            for adx_th in [15, 20, 25, 30]:
                for sl in [1.0, 1.5, 2.0]:
                    for ta in [1.0, 1.5, 2.0]:
                        for td in [0.2, 0.3, 0.5]:
                            for cd in [600, 900, 1200]:
                                win_params_list.append({
                                    "ema_fast": ef, "ema_slow": es,
                                    "adx_period": 14, "adx_threshold": adx_th,
                                    "rsi_period": 14, "rsi_ob": 70, "rsi_os": 30,
                                    "sl_atr_mult": sl, "trail_act": ta,
                                    "trail_dist": td, "cooldown": cd, "max_daily": 6,
                                })

    print(f"  Testing {len(win_params_list)} WIN parameter combinations on {cpu_count()} CPUs...")
    # Build task list: each combo × each timeframe
    win_tasks = []
    for p in win_params_list:
        for sym_df, sym_tf, sym_name in [(win_m5, "M5", "WIN$ M5"), (win_m15, "M15", "WIN$ M15")]:
            win_tasks.append((sym_df, sym_name, p))
    # Parallel execution
    with Pool(processes=cpu_count()) as pool:
        win_results = [r for r in pool.map(_win_worker, win_tasks) if r is not None]

    win_results.sort(key=lambda x: x["score"], reverse=True)
    print(f"\n  TOP 5 WIN RESULTS:")
    for i, wr in enumerate(win_results[:5]):
        r = wr["result"]
        p = wr["params"]
        print(f"  #{i+1} [{wr['sym_tf']}] Score={wr['score']:.2f} | Ret={r['ret']:+.2f}% WR={r['wr']:.0f}% PF={r['pf']:.2f} Sharpe={r['sharpe']:.1f} DD={r['max_dd']:.2f}% Trades={r['trades']}")
        print(f"       EMA={p['ema_fast']}/{p['ema_slow']} ADX>{p['adx_threshold']} SL={p['sl_atr_mult']}x Trail={p['trail_act']}/{p['trail_dist']} CD={p['cooldown']}s")

    # ===== BEST OVERALL =====
    print("\n\n" + "="*100)
    print("  OPTIMAL PARAMETERS SUMMARY")
    print("="*100)

    if wdo_results:
        best_wdo = wdo_results[0]
        bp = best_wdo["params"]
        br = best_wdo["result"]
        print(f"\n  WDO$ OPTIMAL ({best_wdo['sym_tf']}):")
        print(f"    Ret={br['ret']:+.2f}% | WR={br['wr']:.0f}% | PF={br['pf']:.2f} | Sharpe={br['sharpe']:.1f}")
        print(f"    VWAP Period={bp['vwap_period']} | Buy={bp['buy_thresh']} | Sell={bp['sell_thresh']}")
        print(f"    SL={bp['sl_atr_mult']}x ATR | Trail={bp['trail_act']}/{bp['trail_dist']} | CD={bp['cooldown']}s")

    if win_results:
        best_win = win_results[0]
        bp = best_win["params"]
        br = best_win["result"]
        print(f"\n  WIN$ OPTIMAL ({best_win['sym_tf']}):")
        print(f"    Ret={br['ret']:+.2f}% | WR={br['wr']:.0f}% | PF={br['pf']:.2f} | Sharpe={br['sharpe']:.1f}")
        print(f"    EMA={bp['ema_fast']}/{bp['ema_slow']} | ADX>{bp['adx_threshold']}")
        print(f"    SL={bp['sl_atr_mult']}x ATR | Trail={bp['trail_act']}/{bp['trail_dist']} | CD={bp['cooldown']}s")

    print("\n" + "="*100 + "\n")


if __name__ == "__main__":
    run()
