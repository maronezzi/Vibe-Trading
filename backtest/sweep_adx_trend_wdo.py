"""
Parameter sweep for ADX_TREND on WDO — find optimal params for both M5 and M15.
"""
import sys, csv, io, subprocess, os, json, itertools
from pathlib import Path
import numpy as np, pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
WINE_PYTHON = os.path.expanduser("~/.wine/drive_c/Python311/python.exe")
FETCH_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mt5_fetch.py")
STRATEGIES_DIR = Path(__file__).parent / "strategies"

CONTRACT_SPECS = {
    "WDO$": {"mult": 10.0, "margin": 3000, "tick": 0.5, "slip_r": 5.0},
}
COMMISSION = 2.5
CLOSE_HOUR, CLOSE_MINUTE = 16, 45
START_HOUR, START_MINUTE = 9, 5


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


def _ema(values, period):
    if len(values) < period:
        return 0
    arr = np.array(values, dtype=float)
    alpha = 2.0 / (period + 1)
    ema = arr[0]
    for v in arr[1:]:
        ema = alpha * v + (1 - alpha) * ema
    return ema


def _rsi(bars, period=14):
    if len(bars) < period + 2:
        return 50.0
    closes = [float(b["close"]) for b in reversed(bars)]
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def _adx(bars, period=14):
    if len(bars) < period * 2 + 2:
        return 0, 0, 0
    closes = [float(b["close"]) for b in reversed(bars)]
    highs = [float(b["high"]) for b in reversed(bars)]
    lows = [float(b["low"]) for b in reversed(bars)]
    
    plus_dm = []
    minus_dm = []
    tr_list = []
    for i in range(1, len(closes)):
        h_diff = highs[i] - highs[i-1]
        l_diff = lows[i-1] - lows[i]
        plus_dm.append(max(h_diff, 0) if h_diff > l_diff else 0)
        minus_dm.append(max(l_diff, 0) if l_diff > h_diff else 0)
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        tr_list.append(tr)
    
    if len(tr_list) < period:
        return 0, 0, 0
    
    atr = np.mean(tr_list[:period])
    plus_di_val = np.mean(plus_dm[:period])
    minus_di_val = np.mean(minus_dm[:period])
    
    for i in range(period, len(tr_list)):
        atr = (atr * (period - 1) + tr_list[i]) / period
        plus_di_val = (plus_di_val * (period - 1) + plus_dm[i]) / period
        minus_di_val = (minus_di_val * (period - 1) + minus_dm[i]) / period
    
    if atr == 0:
        return 0, 0, 0
    
    plus_di = 100 * plus_di_val / atr
    minus_di = 100 * minus_di_val / atr
    
    dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100 if (plus_di + minus_di) > 0 else 0
    adx = dx  # simplified - using last DX as ADX approximation
    
    return adx, plus_di, minus_di


def _vwap(bars, period=20):
    if len(bars) < period:
        return 0
    typical = [(float(b["high"]) + float(b["low"]) + float(b["close"])) / 3 for b in reversed(bars)]
    vol = [float(b.get("volume", 1) or 1) for b in reversed(bars)]
    tp_vol = sum(t * v for t, v in zip(typical[-period:], vol[-period:]))
    vol_sum = sum(vol[-period:])
    return tp_vol / vol_sum if vol_sum > 0 else 0


def _bollinger(bars, period=20, std_dev=2):
    if len(bars) < period:
        return 0, 0, 0
    closes = [float(b["close"]) for b in reversed(bars)]
    sma = np.mean(closes[-period:])
    std = np.std(closes[-period:])
    return sma, sma + std_dev * std, sma - std_dev * std


def _market_regime(bars, params):
    if len(bars) < 30:
        return "UNKNOWN"
    ema_fast = _ema([float(b["close"]) for b in reversed(bars)], params.get("ema_fast", 9))
    ema_slow = _ema([float(b["close"]) for b in reversed(bars)], params.get("ema_slow", 21))
    adx_val, _, _ = _adx(bars, params.get("adx_period", 14))
    if adx_val < 15:
        return "CHOPPY"
    if adx_val > 25:
        return "TRENDING"
    return "RANGING"


def _calc_sl(symbol, atr, params):
    is_win = "WIN" in symbol.upper()
    mult = params.get("sl_atr_mult", 1.5)
    raw = int(atr * mult)
    raw = max(raw, 200)
    return ((raw + 4) // 5) * 5


def make_utils():
    return {
        "calculate_ema": lambda bars, period: _ema([float(b["close"]) for b in reversed(bars)], period),
        "calculate_rsi": _rsi,
        "calculate_adx": _adx,
        "calculate_vwap": _vwap,
        "calculate_bollinger": _bollinger,
        "get_market_regime": _market_regime,
        "calc_sl": _calc_sl,
    }


def load_strategy(name):
    import importlib.util
    for py_file in STRATEGIES_DIR.glob("*.py"):
        if py_file.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(f"strategies.{py_file.stem}", str(py_file))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if getattr(module, "STRATEGY_NAME", "") == name:
            return module.check_entry
    return None


def backtest_strategy(df, symbol, strategy_func, params, utils, capital=100_000.0):
    spec = CONTRACT_SPECS[symbol]
    mult, margin, slip_r = spec["mult"], spec["margin"], spec["slip_r"]
    
    atr = calc_atr(df, 14)
    cash = capital
    pos = 0; ep = 0.0; e_date = None; e_atr = 0.0; best = 0.0
    sl_price = 0.0; trail_on = False; sl_pts_val = 0; bars_in_trade = 0
    
    equity = []; trade_log = []; daily_pnl_dict = {}
    n_trades = n_wins = n_long = n_short = n_sl = n_trail = n_close = 0
    gross_win = 0.0; gross_loss_val = 0.0
    
    bars_list = []
    for idx in range(len(df)):
        row = df.iloc[idx]
        bars_list.append({
            "open": float(row["open"]), "high": float(row["high"]),
            "low": float(row["low"]), "close": float(row["close"]),
            "volume": float(row["tick_volume"]), "time": df.index[idx],
        })
    
    def _close(price, reason):
        nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, e_atr
        nonlocal n_trades, n_wins, n_long, n_short, n_sl, n_trail, n_close
        nonlocal gross_win, gross_loss_val, bars_in_trade
        if pos == 0: return
        sl_cost = slip_r; comm = COMMISSION
        if pos == 1:
            pnl = (price - ep) * mult - sl_cost - comm; n_long += 1
        else:
            pnl = (ep - price) * mult - sl_cost - comm; n_short += 1
        cash += margin + pnl; n_trades += 1
        if reason == "SL": n_sl += 1
        elif reason == "TRAIL": n_trail += 1
        elif reason == "1645": n_close += 1
        if pnl > 0: n_wins += 1; gross_win += pnl
        else: gross_loss_val += abs(pnl)
        d = e_date.date() if hasattr(e_date, 'date') else e_date
        if d not in daily_pnl_dict: daily_pnl_dict[d] = 0.0
        daily_pnl_dict[d] += pnl
        pos = 0; ep = 0; best = 0; sl_price = 0; trail_on = False; bars_in_trade = 0
    
    def _open(direction, price, date, cur_atr):
        nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, e_atr, sl_pts_val, bars_in_trade
        if pos != 0: return False
        raw_sl = int(cur_atr * params.get("sl_atr_mult", 1.5))
        raw_sl = max(raw_sl, 200)
        raw_sl = ((raw_sl + 4) // 5) * 5
        cost = slip_r + COMMISSION
        if cash >= margin + cost:
            cash -= margin + cost
            pos = 1 if direction == "BUY" else -1
            ep = price; e_date = date; e_atr = cur_atr; sl_pts_val = raw_sl
            best = price; trail_on = False
            sl_price = price - raw_sl if pos == 1 else price + raw_sl
            bars_in_trade = 0
            return True
        return False
    
    for i, (date, row) in enumerate(df.iterrows()):
        price = float(row["close"]); high = float(row["high"]); low = float(row["low"])
        hour = int(row["hour"]); minute = int(row["minute"])
        cur_atr = float(atr.iloc[i]) if i > 0 and not pd.isna(atr.iloc[i]) else 0
        
        if pos == 1:
            eq_val = cash + (price - ep) * mult + margin
        elif pos == -1:
            eq_val = cash + (ep - price) * mult + margin
        else:
            eq_val = cash
        equity.append(eq_val)
        
        if pos == 0:
            if cur_atr > 0 and i >= max(params.get("ema_slow", 21), params.get("adx_period", 14)) + 5:
                active_bars = bars_list[max(0, i-29):i+1]
                result = strategy_func("WDO$", "M5" if len(df) < 300 else "M15",
                                       price, cur_atr, date, active_bars, params, utils)
                if result:
                    _open(result["direction"], price, date, cur_atr)
            continue
        
        bars_in_trade += 1
        if pos == 1: best = max(best, high)
        elif pos == -1: best = min(best, low) if best > 0 else low
        
        profit_pts = (best - ep) if pos == 1 else (ep - best)
        
        if not trail_on and e_atr > 0 and profit_pts >= params.get("trail_activate", 1.5) * e_atr:
            trail_on = True
        
        if trail_on and e_atr > 0:
            trail_dist = params.get("trail_distance", 0.5) * e_atr
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
    avg_daily = sum(t["pnl"] for t in trade_log) / n_days if n_days else 0
    
    return {
        "ret": total_ret, "trades": n_trades, "wr": wr,
        "sharpe": sharpe, "max_dd": max_dd, "pf": pf,
        "avg_daily": avg_daily, "n_days": n_days,
        "n_sl": n_sl, "n_trail": n_trail, "n_close": n_close,
    }


def run():
    print("🔬 PARAMETER SWEEP — ADX_TREND on WDO$")
    print("=" * 80)
    
    strategy_func = load_strategy("ADX_TREND")
    if not strategy_func:
        print("❌ Could not load ADX_TREND strategy")
        return
    
    utils = make_utils()
    symbol = "WDO$"
    
    # Fetch data
    df_m5 = fetch(symbol, "M5", 500)
    df_m15 = fetch(symbol, "M15", 500)
    
    if df_m5.empty or df_m15.empty:
        print("❌ No data"); return
    
    print(f"  M5: {len(df_m5)} bars, M15: {len(df_m15)} bars")
    
    # Parameter grid
    param_grid = {
        "ema_fast": [7, 9, 12],
        "ema_slow": [18, 21, 26],
        "adx_period": [14],
        "adx_threshold": [15, 20, 25, 30],
        "rsi_period": [14],
        "rsi_overbought": [75, 80, 85],
        "rsi_oversold": [15, 20, 25],
        "sl_atr_mult": [0.8, 1.0, 1.2],
        "trail_activate": [1.0, 1.5, 2.0],
        "trail_distance": [0.3, 0.5, 0.7],
    }
    
    # Generate combinations (limited for speed)
    keys = list(param_grid.keys())
    values = list(param_grid.values())
    combos = list(itertools.product(*values))
    
    print(f"  Testing {len(combos)} parameter combinations...")
    
    results = []
    for i, combo in enumerate(combos):
        params = dict(zip(keys, combo))
        
        # Skip invalid combos (fast >= slow)
        if params["ema_fast"] >= params["ema_slow"]:
            continue
        
        try:
            r_m5 = backtest_strategy(df_m5, symbol, strategy_func, params, utils)
            r_m15 = backtest_strategy(df_m15, symbol, strategy_func, params, utils)
        except Exception as e:
            continue
        
        # Combined score: weighted by days
        combined_ret = (r_m5["ret"] * r_m5["n_days"] + r_m15["ret"] * r_m15["n_days"]) / (r_m5["n_days"] + r_m15["n_days"]) if (r_m5["n_days"] + r_m15["n_days"]) > 0 else 0
        
        # Penalty for negative M5
        penalty = abs(r_m5["ret"]) * 2 if r_m5["ret"] < 0 else 0
        
        score = combined_ret - penalty
        
        results.append({
            "params": params,
            "m5_ret": r_m5["ret"], "m5_wr": r_m5["wr"], "m5_sharpe": r_m5["sharpe"],
            "m5_pf": r_m5["pf"], "m5_trades": r_m5["trades"], "m5_dd": r_m5["max_dd"],
            "m15_ret": r_m15["ret"], "m15_wr": r_m15["wr"], "m15_sharpe": r_m15["sharpe"],
            "m15_pf": r_m15["pf"], "m15_trades": r_m15["trades"], "m15_dd": r_m15["max_dd"],
            "combined_ret": combined_ret, "score": score,
        })
        
        if (i + 1) % 50 == 0:
            print(f"  ... {i+1}/{len(combos)} done")
    
    if not results:
        print("❌ No valid results"); return
    
    # Sort by score
    results.sort(key=lambda x: x["score"], reverse=True)
    
    print("\n🏆 TOP 10 PARAMETER SETS:")
    print("=" * 140)
    print(f"{'Rank':>4} {'Params':50} {'M5 Ret%':>8} {'M5 WR':>6} {'M5 Sharpe':>9} {'M5 PF':>6} {'M5 DD':>6} {'M15 Ret%':>8} {'M15 WR':>7} {'M15 Sharpe':>9} {'M15 PF':>6} {'M15 DD':>6} {'Score':>6}")
    print("-" * 140)
    
    for rank, r in enumerate(results[:10], 1):
        p = r["params"]
        pstr = f"ema={p['ema_fast']}/{p['ema_slow']} adx_th={p['adx_threshold']} rsi_ob={p['rsi_overbought']} rsi_os={p['rsi_oversold']} sl={p['sl_atr_mult']} trail={p['trail_activate']}/{p['trail_distance']}"
        print(f"{rank:>4} {pstr:50} {r['m5_ret']:>+7.2f}% {r['m5_wr']:>5.1f}% {r['m5_sharpe']:>+8.2f} {r['m5_pf']:>5.1f} {r['m5_dd']:>5.2f}% {r['m15_ret']:>+7.2f}% {r['m15_wr']:>6.1f}% {r['m15_sharpe']:>+8.2f} {r['m15_pf']:>5.1f} {r['m15_dd']:>5.2f}% {r['score']:>+5.2f}")
    
    # Best params
    best = results[0]
    print(f"\n✅ BEST PARAMS:")
    print(json.dumps(best["params"], indent=2))
    print(f"\n   M5:  Ret {best['m5_ret']:+.2f}% | WR {best['m5_wr']:.1f}% | Sharpe {best['m15_sharpe']:.2f} | PF {best['m5_pf']:.1f} | DD {best['m5_dd']:.2f}%")
    print(f"   M15: Ret {best['m15_ret']:+.2f}% | WR {best['m15_wr']:.1f}% | Sharpe {best['m15_sharpe']:.2f} | PF {best['m15_pf']:.1f} | DD {best['m15_dd']:.2f}%")
    
    # Also test current EMA_PULLBACK for comparison
    print("\n📊 COMPARISON vs CURRENT (EMA_PULLBACK):")
    print("   Current EMA_PULLBACK: M5 +0.41% | M15 +0.86%")
    print(f"   New ADX_TREND best:   M5 {best['m5_ret']:+.2f}% | M15 {best['m15_ret']:+.2f}%")


if __name__ == "__main__":
    run()
