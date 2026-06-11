"""
backtest_v7_iter2.py — Parameter sweep optimization.

Iter1 results:
  WIN$ M5:  +0.14% | WR 55.6% | Sharpe 6.65  → GOOD, keep
  WIN$ M15: +0.10% | WR 50.0% | Sharpe 2.58  → OK, optimize
  WDO$ M5:  +0.24% | WR 80.0% | Sharpe 10.70 → GOOD, keep
  WDO$ M15: -0.24% | WR 46.2% | Sharpe -2.18 → NEEDS WORK

Focus: WDO M15 optimization, WIN fine-tuning.
"""

import sys, csv, io, subprocess, os
from pathlib import Path
import numpy as np, pandas as pd

WINE_PYTHON = os.path.expanduser("~/.wine/drive_c/Python311/python.exe")
FETCH_SCRIPT = os.path.join(os.path.dirname(__file__), "mt5_fetch.py")

CLOSE_HOUR, CLOSE_MINUTE = 16, 45

CONTRACT_SPECS = {
    "WIN$": {"mult": 0.20, "name": "Mini Índice", "margin": 5000, "tick": 5, "slip_r": 1.0},
    "WDO$": {"mult": 10.0, "name": "Mini Dólar", "margin": 3000, "tick": 0.5, "slip_r": 5.0},
}
COMMISSION = 2.5
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
    tr = pd.concat([h - l, (h - c_prev).abs(), (l - c_prev).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def calc_vwap(df, period=20):
    typical = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["tick_volume"].replace(0, 1)
    return (typical * vol).rolling(period).sum() / vol.rolling(period).sum()


def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def calc_rsi(df, period=14):
    delta = df["close"].diff()
    gain = delta.clip(lower=0).ewm(span=period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(span=period, adjust=False).mean()
    rs = gain / loss.replace(0, 0.001)
    return 100 - 100 / (1 + rs)


def calc_adx(df, period=14):
    high = df["high"]
    low = df["low"]
    close = df["close"]
    
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)
    
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(span=period, adjust=False).mean() / atr.replace(0, 0.001)
    minus_di = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr.replace(0, 0.001)
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 0.001) * 100
    adx = dx.ewm(span=period, adjust=False).mean()
    return adx, plus_di, minus_di


def backtest_split(df, symbol, params, capital=100_000.0):
    spec = CONTRACT_SPECS[symbol]
    mult, margin, slip_r = spec["mult"], spec["margin"], spec["slip_r"]
    is_win = "WIN" in symbol
    
    atr = calc_atr(df, 14)
    rsi = calc_rsi(df, params["rsi_period"])
    
    if is_win:
        ema_fast = calc_ema(df["close"], params["ema_fast"])
        ema_slow = calc_ema(df["close"], params["ema_slow"])
        adx, plus_di, minus_di = calc_adx(df, params["adx_period"])
    else:
        vwap = calc_vwap(df, params["vwap_period"])
        ema_fast = calc_ema(df["close"], params["ema_fast"])
        ema_slow = calc_ema(df["close"], params["ema_slow"])
    
    cash = capital
    pos = 0; ep = 0.0; e_date = None; e_atr = 0.0; best = 0.0
    sl_price = 0.0; trail_on = False; sl_pts = 0; bars_in_trade = 0
    last_trade_bar = -999
    
    equity, trade_log, daily_pnl = [], [], []
    n_trades = n_wins = n_long = n_short = 0
    n_sl = n_trail = n_close = 0
    gross_win = 0.0; gross_loss_val = 0.0; daily_pnl_dict = {}
    
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
        trade_log.append({"pnl": pnl, "reason": reason, "bars": bars_in_trade})
        daily_pnl.append(pnl)
        d = e_date.date() if hasattr(e_date, 'date') else e_date
        if d not in daily_pnl_dict: daily_pnl_dict[d] = 0.0
        daily_pnl_dict[d] += pnl
        pos = 0; ep = 0; best = 0; sl_price = 0; trail_on = False; bars_in_trade = 0
    
    def _open(direction, price, date, cur_atr):
        nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, e_atr, sl_pts, bars_in_trade, last_trade_bar
        if pos != 0: return False
        if i - last_trade_bar < params.get("cooldown_bars", 3): return False
        raw_sl = int(cur_atr * params["sl_atr_mult"])
        raw_sl = max(raw_sl, params["sl_min"])
        raw_sl = ((raw_sl + 4) // 5) * 5
        cost = slip_r * MAX_CT + COMMISSION * MAX_CT
        if cash >= margin * MAX_CT + cost:
            cash -= margin * MAX_CT + cost
            pos = 1 if direction == "BUY" else -1
            ep = price; e_date = date; e_atr = cur_atr; sl_pts = raw_sl
            best = price; trail_on = False
            sl_price = price - raw_sl if pos == 1 else price + raw_sl
            bars_in_trade = 0
            return True
        return False
    
    for i, (date, row) in enumerate(df.iterrows()):
        price = float(row["close"]); high = float(row["high"]); low = float(row["low"])
        hour = int(row["hour"]); minute = int(row["minute"])
        cur_atr = float(atr.iloc[i]) if not pd.isna(atr.iloc[i]) else 0
        cur_rsi = float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else 50
        
        if pos == 1:
            eq_val = cash + (price - ep) * mult * MAX_CT + margin * MAX_CT
        elif pos == -1:
            eq_val = cash + (ep - price) * mult * MAX_CT + margin * MAX_CT
        else:
            eq_val = cash
        equity.append(eq_val)
        
        if pos == 0:
            if cur_atr <= 0: continue
            
            if is_win:
                if i < max(params["ema_slow"], params["adx_period"]) + 5: continue
                cur_ema_fast = float(ema_fast.iloc[i])
                cur_ema_slow = float(ema_slow.iloc[i])
                cur_adx = float(adx.iloc[i])
                cur_plus_di = float(plus_di.iloc[i])
                cur_minus_di = float(minus_di.iloc[i])
                prev_ema_fast = float(ema_fast.iloc[i-1])
                prev_ema_slow = float(ema_slow.iloc[i-1])
                
                if cur_adx < params["adx_threshold"]: continue
                
                direction = None
                if prev_ema_fast <= prev_ema_slow and cur_ema_fast > cur_ema_slow:
                    if cur_plus_di > cur_minus_di: direction = "BUY"
                elif prev_ema_fast >= prev_ema_slow and cur_ema_fast < cur_ema_slow:
                    if cur_minus_di > cur_plus_di: direction = "SELL"
                
                if not direction: continue
                if direction == "BUY" and cur_rsi > params["rsi_overbought"]: continue
                if direction == "SELL" and cur_rsi < params["rsi_oversold"]: continue
                _open(direction, price, date, cur_atr)
                last_trade_bar = i
            else:
                cur_vwap = float(vwap.iloc[i]) if not pd.isna(vwap.iloc[i]) else 0
                if cur_vwap <= 0: continue
                cur_ema_fast = float(ema_fast.iloc[i]) if not pd.isna(ema_fast.iloc[i]) else 0
                cur_ema_slow = float(ema_slow.iloc[i]) if not pd.isna(ema_slow.iloc[i]) else 0
                
                if cur_ema_fast > 0 and cur_ema_slow > 0:
                    spread = abs(cur_ema_fast - cur_ema_slow) / price
                    if spread < params.get("trend_min_spread", 0.001): continue
                
                buy_thresh = cur_vwap * params["vwap_buy_threshold"]
                sell_thresh = cur_vwap * params["vwap_sell_threshold"]
                
                direction = None
                if price > buy_thresh:
                    direction = "BUY"
                    if cur_ema_fast > 0 and cur_ema_slow > 0 and cur_ema_fast < cur_ema_slow: continue
                elif price < sell_thresh:
                    direction = "SELL"
                    if cur_ema_fast > 0 and cur_ema_slow > 0 and cur_ema_fast > cur_ema_slow: continue
                
                if not direction: continue
                if direction == "BUY" and cur_rsi > params["rsi_overbought"]: continue
                if direction == "SELL" and cur_rsi < params["rsi_oversold"]: continue
                _open(direction, price, date, cur_atr)
                last_trade_bar = i
            continue
        
        bars_in_trade += 1
        if pos == 1: best = max(best, high)
        elif pos == -1: best = min(best, low) if best > 0 else low
        
        profit_pts = (best - ep) if pos == 1 else (ep - best)
        if not trail_on and e_atr > 0 and profit_pts >= params["trail_activate"] * e_atr:
            trail_on = True
        
        if trail_on and e_atr > 0:
            trail_dist = params["trail_distance"] * e_atr
            if pos == 1:
                new_sl = best - trail_dist
                if new_sl > sl_price: sl_price = new_sl
            elif pos == -1:
                new_sl = best + trail_dist
                if new_sl < sl_price: sl_price = new_sl
        
        if sl_price > 0:
            if pos == 1 and low <= sl_price: _close(sl_price, "SL"); continue
            elif pos == -1 and high >= sl_price: _close(sl_price, "SL"); continue
        
        if hour > CLOSE_HOUR or (hour == CLOSE_HOUR and minute >= CLOSE_MINUTE):
            _close(price, "1645"); continue
    
    if pos != 0: _close(float(df["close"].iloc[-1]), "FORCE")
    
    total_ret = (cash - capital) / capital * 100
    n_days = df["date"].nunique()
    daily_vals = list(daily_pnl_dict.values())
    avg_daily = sum(t["pnl"] for t in trade_log) / n_days if n_days else 0
    
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
    
    return {
        "ret": total_ret, "trades": n_trades, "wr": wr, "sharpe": sharpe,
        "max_dd": max_dd, "pf": pf, "avg_daily": avg_daily,
        "n_sl": n_sl, "n_trail": n_trail, "n_close": n_close,
    }


def run():
    print("\n" + "═" * 100)
    print("  🔬 BACKTEST v7 ITER2 — PARAMETER SWEEP OPTIMIZATION")
    print("═" * 100)
    
    # Fetch data once
    data = {}
    for sym, tf, n in [("WIN$", "M5", 500), ("WIN$", "M15", 500), ("WDO$", "M5", 500), ("WDO$", "M15", 500)]:
        df = fetch(sym, tf, n)
        if not df.empty:
            data[(sym, tf)] = df
            print(f"  ✅ {sym} {tf}: {len(df)} bars")
    
    # === Iter1 best params ===
    best_win = {
        "ema_fast": 12, "ema_slow": 21, "adx_period": 14, "adx_threshold": 15,
        "rsi_period": 14, "rsi_overbought": 70, "rsi_oversold": 30,
        "sl_atr_mult": 1.5, "trail_activate": 1.0, "trail_distance": 0.2,
        "sl_min": 100, "cooldown_bars": 3,
    }
    best_wdo = {
        "vwap_period": 15, "vwap_buy_threshold": 1.001, "vwap_sell_threshold": 0.999,
        "ema_fast": 9, "ema_slow": 21, "trend_min_spread": 0.001,
        "rsi_period": 14, "rsi_overbought": 70, "rsi_oversold": 30,
        "sl_atr_mult": 1.0, "trail_activate": 1.5, "trail_distance": 0.2,
        "sl_min": 200, "cooldown_bars": 3,
    }
    
    # === Parameter sweep for WDO M15 (the weak link) ===
    print("\n\n📊 SWEEP: WDO$ M15 — Testing VWAP thresholds, trailing, SL...")
    
    wdo_sweep = [
        {"name": "iter1_baseline", **best_wdo},
        {"name": "tighter_vwap", **best_wdo, "vwap_buy_threshold": 1.002, "vwap_sell_threshold": 0.998},
        {"name": "wider_vwap", **best_wdo, "vwap_buy_threshold": 1.0005, "vwap_sell_threshold": 0.9995},
        {"name": "sl_1.2x", **best_wdo, "sl_atr_mult": 1.2},
        {"name": "sl_0.8x", **best_wdo, "sl_atr_mult": 0.8},
        {"name": "trail_1.0", **best_wdo, "trail_activate": 1.0, "trail_distance": 0.3},
        {"name": "trail_2.0", **best_wdo, "trail_activate": 2.0, "trail_distance": 0.15},
        {"name": "cooldown_5", **best_wdo, "cooldown_bars": 5},
        {"name": "cooldown_6", **best_wdo, "cooldown_bars": 6},
        {"name": "no_trend_filter", **best_wdo, "trend_min_spread": 0},
        {"name": "vwap20", **best_wdo, "vwap_period": 20},
        {"name": "vwap25", **best_wdo, "vwap_period": 25},
        {"name": "rsi_40_60", **best_wdo, "rsi_overbought": 60, "rsi_oversold": 40},
        {"name": "rsi_35_65", **best_wdo, "rsi_overbought": 65, "rsi_oversold": 35},
        {"name": "combined_1", **best_wdo, "vwap_buy_threshold": 1.0015, "vwap_sell_threshold": 0.9985, "sl_atr_mult": 1.2, "cooldown_bars": 5},
        {"name": "combined_2", **best_wdo, "trail_activate": 1.0, "trail_distance": 0.3, "cooldown_bars": 5, "sl_atr_mult": 1.2},
    ]
    
    wdo_m15_results = []
    if ("WDO$", "M15") in data:
        df_wdo_m15 = data[("WDO$", "M15")]
        for params in wdo_sweep:
            r = backtest_split(df_wdo_m15, "WDO$", params)
            wdo_m15_results.append((params["name"], r))
            print(f"  {params['name']:<25} Ret: {r['ret']:+.2f}%  WR: {r['wr']:>5.1f}%  Sharpe: {r['sharpe']:>+7.2f}  PF: {r['pf']:>5.2f}  Trades: {r['trades']}")
    
    # Best WDO M15
    wdo_m15_best_name, wdo_m15_best = max(wdo_m15_results, key=lambda x: x[1]["ret"])
    print(f"\n  🏆 Best WDO M15: {wdo_m15_best_name} → Ret: {wdo_m15_best['ret']:+.2f}% | Sharpe: {wdo_m15_best['sharpe']:.2f}")
    
    # === Parameter sweep for WDO M5 ===
    print("\n📊 SWEEP: WDO$ M5 — Testing optimization...")
    
    wdo_m5_sweep = [
        {"name": "iter1_baseline", **best_wdo},
        {"name": "sl_1.2x", **best_wdo, "sl_atr_mult": 1.2},
        {"name": "trail_1.0", **best_wdo, "trail_activate": 1.0, "trail_distance": 0.3},
        {"name": "cooldown_5", **best_wdo, "cooldown_bars": 5},
        {"name": "tighter_vwap", **best_wdo, "vwap_buy_threshold": 1.002, "vwap_sell_threshold": 0.998},
    ]
    
    wdo_m5_results = []
    if ("WDO$", "M5") in data:
        df_wdo_m5 = data[("WDO$", "M5")]
        for params in wdo_m5_sweep:
            r = backtest_split(df_wdo_m5, "WDO$", params)
            wdo_m5_results.append((params["name"], r))
            print(f"  {params['name']:<25} Ret: {r['ret']:+.2f}%  WR: {r['wr']:>5.1f}%  Sharpe: {r['sharpe']:>+7.2f}  PF: {r['pf']:>5.2f}  Trades: {r['trades']}")
    
    wdo_m5_best_name, wdo_m5_best = max(wdo_m5_results, key=lambda x: x[1]["ret"])
    print(f"\n  🏆 Best WDO M5: {wdo_m5_best_name} → Ret: {wdo_m5_best['ret']:+.2f}% | Sharpe: {wdo_m5_best['sharpe']:.2f}")
    
    # === Parameter sweep for WIN M5 ===
    print("\n📊 SWEEP: WIN$ M5 — Testing EMA/ADX tweaks...")
    
    win_m5_sweep = [
        {"name": "iter1_baseline", **best_win},
        {"name": "ema_8_21", **best_win, "ema_fast": 8, "ema_slow": 21},
        {"name": "ema_5_13", **best_win, "ema_fast": 5, "ema_slow": 13},
        {"name": "adx_20", **best_win, "adx_threshold": 20},
        {"name": "adx_18", **best_win, "adx_threshold": 18},
        {"name": "adx_12", **best_win, "adx_threshold": 12},
        {"name": "sl_2.0x", **best_win, "sl_atr_mult": 2.0},
        {"name": "sl_1.0x", **best_win, "sl_atr_mult": 1.0},
        {"name": "trail_1.5", **best_win, "trail_activate": 1.5, "trail_distance": 0.3},
        {"name": "cooldown_5", **best_win, "cooldown_bars": 5},
        {"name": "rsi_35_65", **best_win, "rsi_overbought": 65, "rsi_oversold": 35},
        {"name": "combined", **best_win, "adx_threshold": 18, "sl_atr_mult": 1.2, "cooldown_bars": 5},
    ]
    
    win_m5_results = []
    if ("WIN$", "M5") in data:
        df_win_m5 = data[("WIN$", "M5")]
        for params in win_m5_sweep:
            r = backtest_split(df_win_m5, "WIN$", params)
            win_m5_results.append((params["name"], r))
            print(f"  {params['name']:<25} Ret: {r['ret']:+.2f}%  WR: {r['wr']:>5.1f}%  Sharpe: {r['sharpe']:>+7.2f}  PF: {r['pf']:>5.2f}  Trades: {r['trades']}")
    
    win_m5_best_name, win_m5_best = max(win_m5_results, key=lambda x: x[1]["ret"])
    print(f"\n  🏆 Best WIN M5: {win_m5_best_name} → Ret: {win_m5_best['ret']:+.2f}% | Sharpe: {win_m5_best['sharpe']:.2f}")
    
    # === Now run ALL 4 combos with best params ===
    print("\n\n" + "═" * 100)
    print("  📋 FINAL RESULTS — v7 ITER2 (Optimized)")
    print("═" * 100)
    
    # Build optimized params
    # Use iter1 params for WIN M15 (it was fine)
    final_params = {
        "WIN$": {**best_win},  # iter1 defaults
        "WDO$": {**best_wdo},  # will be overridden per TF
    }
    
    # Override with sweep bests
    # WIN M5: use iter1 (was already good)
    win_final = {**best_win}
    
    # WDO: find best params for M5 and M15
    wdo_m5_final = {**best_wdo}
    wdo_m15_final = {**best_wdo}
    
    if wdo_m5_best_name != "iter1_baseline":
        # Find the winning params
        for p in wdo_m5_sweep:
            if p["name"] == wdo_m5_best_name:
                wdo_m5_final = {k: v for k, v in p.items() if k != "name"}
                break
    
    if wdo_m15_best_name != "iter1_baseline":
        for p in wdo_sweep:
            if p["name"] == wdo_m15_best_name:
                wdo_m15_final = {k: v for k, v in p.items() if k != "name"}
                break
    
    combos = [
        ("WIN$", "M5", 500, win_final),
        ("WIN$", "M15", 500, best_win),
        ("WDO$", "M5", 500, wdo_m5_final),
        ("WDO$", "M15", 500, wdo_m15_final),
    ]
    
    all_results = []
    for sym, tf, n_bars, params in combos:
        if (sym, tf) not in data:
            continue
        df = data[(sym, tf)]
        r = backtest_split(df, sym, params)
        root = "WIN" if "WIN" in sym else "WDO"
        strat_name = f"EMA({params['ema_fast']}/{params['ema_slow']})+ADX>{params['adx_threshold']}" if root == "WIN" else f"VWAP({params['vwap_period']})"
        
        all_results.append({
            "symbol": sym, "tf": tf, "strategy": strat_name, **r,
        })
        
        print(f"\n  {'─' * 60}")
        print(f"  📊 {strat_name} — {sym} {tf}")
        print(f"  Retorno: {r['ret']:+.2f}% | Trades: {r['trades']} | WR: {r['wr']:.1f}%")
        print(f"  Sharpe: {r['sharpe']:.2f} | DD: {r['max_dd']:.2f}% | PF: {r['pf']:.2f}")
        print(f"  R$/dia: R$ {r['avg_daily']:+.1f}")
        print(f"  Exit: SL={r['n_sl']} TRAIL={r['n_trail']} 1645={r['n_close']}")
    
    # Comparison
    v6_data = {
        ("WIN$", "M5"): -0.48, ("WIN$", "M15"): -1.33,
        ("WDO$", "M5"): 0.57, ("WDO$", "M15"): 0.14,
    }
    v7_iter1 = {
        ("WIN$", "M5"): 0.14, ("WIN$", "M15"): 0.10,
        ("WDO$", "M5"): 0.24, ("WDO$", "M15"): -0.24,
    }
    
    print("\n\n" + "═" * 100)
    print("  📈 COMPARISON: v6 → v7 iter1 → v7 iter2")
    print("═" * 100)
    print(f"\n{'Ativo':<10} {'TF':<4} {'v6%':>7} {'iter1%':>7} {'iter2%':>7} {'Δ1':>7} {'Δ2':>7}")
    print("─" * 60)
    
    total_v6 = 0; total_iter1 = 0; total_iter2 = 0
    for r in all_results:
        key = (r["symbol"], r["tf"])
        v6 = v6_data.get(key, 0)
        i1 = v7_iter1.get(key, 0)
        i2 = r["ret"]
        d1 = i1 - v6
        d2 = i2 - i1
        total_v6 += v6; total_iter1 += i1; total_iter2 += i2
        print(f"  {r['symbol']:<7} {r['tf']:<4} {v6:>+6.2f}% {i1:>+6.2f}% {i2:>+6.2f}% {d1:>+6.2f}% {d2:>+6.2f}%")
    
    print(f"\n  📊 TOTAL: v6={total_v6:+.2f}% | iter1={total_iter1:+.2f}% | iter2={total_iter2:+.2f}%")
    print(f"  📈 Δ iter1→iter2: {total_iter2-total_iter1:+.2f}%")
    
    profitable = [x for x in all_results if x["ret"] > 0]
    print(f"  💰 Lucrativos: {len(profitable)}/{len(all_results)}")
    for p in profitable:
        print(f"    ✅ {p['symbol']} {p['tf']} — {p['ret']:+.2f}% | Sharpe {p['sharpe']:.2f} | PF {p['pf']:.2f} | R${p['avg_daily']:+.0f}/dia")
    
    # Save final params for deployment
    print("\n\n" + "═" * 100)
    print("  📦 FINAL OPTIMIZED PARAMS FOR DEPLOYMENT")
    print("═" * 100)
    print(f"\n  WIN: {win_final}")
    print(f"  WDO M5: {wdo_m5_final}")
    print(f"  WDO M15: {wdo_m15_final}")
    
    out = Path("/tmp/backtest_v7_iter2.csv")
    pd.DataFrame([{k: v for k, v in r.items() if k in ["symbol","tf","strategy","ret","trades","wr","sharpe","max_dd","pf","avg_daily"]} for r in all_results]).to_csv(out, index=False)
    print(f"\n  💾 CSV: {out}")
    print("\n" + "═" * 100 + "\n")


if __name__ == "__main__":
    run()
