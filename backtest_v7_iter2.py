"""
backtest_v7_iter2.py — Iteração 2: WIN$ EMA Crossover (mantido) + WDO$ otimizado com restrições.
"""

import sys, csv, io, subprocess, os
from pathlib import Path
import numpy as np, pandas as pd
from itertools import product

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

def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    loss = loss.replace(0, 1e-10)
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calc_adx(df, period=14):
    high, low, close = df["high"], df["low"], df["close"]
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().replace(0, 1e-10)
    plus_di = 100 * (plus_dm.rolling(period).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(period).mean() / atr)
    di_sum = (plus_di + minus_di).replace(0, 1e-10)
    dx = 100 * ((plus_di - minus_di).abs() / di_sum)
    adx = dx.rolling(period).mean()
    return adx, plus_di, minus_di

def calc_vwap(df, period=20):
    typical = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["tick_volume"].replace(0, 1)
    return (typical * vol).rolling(period).sum() / vol.rolling(period).sum()


def _make_bt_func(strategy_type):
    """Factory: returns a backtest function for the given strategy."""
    
    def bt(df, params, capital=100_000.0):
        is_win = strategy_type == "WIN"
        spec = CONTRACT_SPECS["WIN$" if is_win else "WDO$"]
        mult, margin, slip_r = spec["mult"], spec["margin"], spec["slip_r"]
        sl_min = SL_MIN_WIN if is_win else SL_MIN_WDO
        
        atr_period = params.get("atr_period", 14)
        sl_mult = params.get("sl_mult", 1.5)
        trail_activate = params.get("trail_activate", 1.5)
        trail_distance = params.get("trail_distance", 0.5)
        
        atr = calc_atr(df, atr_period)
        
        if is_win:
            ema_fast = calc_ema(df["close"], params.get("ema_fast", 9))
            ema_slow = calc_ema(df["close"], params.get("ema_slow", 21))
            adx, plus_di, minus_di = calc_adx(df, params.get("adx_period", 14))
            rsi = calc_rsi(df["close"], 14)
            adx_thresh = params.get("adx_threshold", 25)
        else:
            vwap = calc_vwap(df, params.get("vwap_period", 20))
            rsi = calc_rsi(df["close"], params.get("rsi_period", 14))
            ema_fast = calc_ema(df["close"], 9)
            ema_slow = calc_ema(df["close"], 21)
            buy_thresh_mult = params.get("buy_threshold", 1.003)
            sell_thresh_mult = params.get("sell_threshold", 0.997)
            rsi_ob = params.get("rsi_overbought", 70)
            rsi_os = params.get("rsi_oversold", 30)
        
        cash = capital
        pos = 0; ep = e_date = e_atr = best = sl_price = 0
        trail_on = False; sl_pts = 0; bars_in_trade = 0
        
        equity, trade_log, daily_pnl = [], [], []
        n_trades = n_wins = n_long = n_short = 0
        n_sl = n_trail = n_close = 0
        gross_win = 0.0; gross_loss_val = 0.0
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
            trade_log.append({"type": "LONG" if pos == 1 else "SHORT", "ep": ep, "xp": price, "pnl": pnl, "reason": reason, "bars": bars_in_trade, "sl_pts": sl_pts})
            daily_pnl.append(pnl)
            d = e_date.date() if hasattr(e_date, 'date') else e_date
            if d not in daily_pnl_dict: daily_pnl_dict[d] = 0.0
            daily_pnl_dict[d] += pnl
            pos = 0; ep = 0; best = 0; sl_price = 0; trail_on = False; bars_in_trade = 0
        
        def _open(direction, price, date, cur_atr):
            nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, e_atr, sl_pts, bars_in_trade
            if pos != 0: return False
            raw_sl = int(cur_atr * sl_mult)
            raw_sl = max(raw_sl, sl_min)
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
            price = float(row["close"])
            high = float(row["high"])
            low = float(row["low"])
            hour = int(row["hour"])
            minute = int(row["minute"])
            cur_atr = float(atr.iloc[i]) if i > 0 and not pd.isna(atr.iloc[i]) else 0
            
            # Mark-to-market
            if pos == 1:
                eq_val = cash + (price - ep) * mult * MAX_CT + margin * MAX_CT
            elif pos == -1:
                eq_val = cash + (ep - price) * mult * MAX_CT + margin * MAX_CT
            else:
                eq_val = cash
            equity.append(eq_val)
            
            if pos == 0:
                if cur_atr > 0:
                    if is_win:
                        # WIN$: EMA Crossover + ADX
                        cur_ema_f = float(ema_fast.iloc[i]) if not pd.isna(ema_fast.iloc[i]) else 0
                        cur_ema_s = float(ema_slow.iloc[i]) if not pd.isna(ema_slow.iloc[i]) else 0
                        cur_adx = float(adx.iloc[i]) if not pd.isna(adx.iloc[i]) else 0
                        cur_rsi_val = float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else 50
                        
                        if cur_ema_f > 0 and cur_ema_s > 0 and cur_adx > 0:
                            if cur_adx < adx_thresh: continue
                            prev_f = float(ema_fast.iloc[i-1]) if i > 0 and not pd.isna(ema_fast.iloc[i-1]) else cur_ema_f
                            prev_s = float(ema_slow.iloc[i-1]) if i > 0 and not pd.isna(ema_slow.iloc[i-1]) else cur_ema_s
                            
                            direction = None
                            if prev_f <= prev_s and cur_ema_f > cur_ema_s: direction = "BUY"
                            elif prev_f >= prev_s and cur_ema_f < cur_ema_s: direction = "SELL"
                            
                            if direction:
                                if direction == "BUY" and cur_rsi_val > 70: continue
                                if direction == "SELL" and cur_rsi_val < 30: continue
                                cur_plus = float(plus_di.iloc[i]) if not pd.isna(plus_di.iloc[i]) else 0
                                cur_minus = float(minus_di.iloc[i]) if not pd.isna(minus_di.iloc[i]) else 0
                                if direction == "BUY" and cur_plus < cur_minus: continue
                                if direction == "SELL" and cur_minus < cur_plus: continue
                                _open(direction, price, date, cur_atr)
                    else:
                        # WDO$: VWAP + RSI + EMA
                        cur_vwap = float(vwap.iloc[i]) if not pd.isna(vwap.iloc[i]) else 0
                        cur_rsi_val = float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else 50
                        cur_ema_f = float(ema_fast.iloc[i]) if not pd.isna(ema_fast.iloc[i]) else 0
                        cur_ema_s = float(ema_slow.iloc[i]) if not pd.isna(ema_slow.iloc[i]) else 0
                        
                        if cur_vwap > 0:
                            # Adaptive threshold
                            atr_pct = (cur_atr / price) if price > 0 else 0
                            if atr_pct < 0.001:
                                adj_buy, adj_sell = 1.001, 0.999
                            elif atr_pct < 0.002:
                                adj_buy, adj_sell = 1.002, 0.998
                            else:
                                adj_buy, adj_sell = buy_thresh_mult, sell_thresh_mult
                            
                            buy_thresh = cur_vwap * adj_buy
                            sell_thresh = cur_vwap * adj_sell
                            
                            direction = None
                            if price > buy_thresh: direction = "BUY"
                            elif price < sell_thresh: direction = "SELL"
                            
                            if direction:
                                if cur_ema_f > 0 and cur_ema_s > 0:
                                    if direction == "BUY" and cur_ema_f < cur_ema_s: continue
                                    if direction == "SELL" and cur_ema_f > cur_ema_s: continue
                                if direction == "BUY" and cur_rsi_val > rsi_ob: continue
                                if direction == "SELL" and cur_rsi_val < rsi_os: continue
                                _open(direction, price, date, cur_atr)
                continue
            
            # Position management
            bars_in_trade += 1
            if pos == 1:
                best = max(best, high); profit_pts = best - ep
            else:
                best = min(best, low) if best > 0 else low; profit_pts = ep - best
            
            if not trail_on and e_atr > 0 and profit_pts >= trail_activate * e_atr:
                trail_on = True
            
            if trail_on and e_atr > 0:
                trail_dist = trail_distance * e_atr
                if pos == 1:
                    new_sl = best - trail_dist
                    if new_sl > sl_price: sl_price = new_sl
                else:
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
        avg_daily = sum(t["pnl"] for t in trade_log) / n_days if n_days else 0
        sharpe = np.mean(daily_vals) / np.std(daily_vals) * np.sqrt(252) if len(daily_vals) > 1 and np.std(daily_vals) > 0 else 0
        eq_arr = np.array(equity)
        running_max = np.maximum.accumulate(eq_arr)
        drawdowns = (running_max - eq_arr) / running_max * 100
        max_dd = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0
        pf = gross_win / gross_loss_val if gross_loss_val > 0 else (999 if gross_win > 0 else 0)
        wr = (n_wins / n_trades * 100) if n_trades else 0
        
        return {
            "ok": True, "trades": n_trades, "wins": n_wins, "wr": wr,
            "long": n_long, "short": n_short,
            "ret": total_ret, "sharpe": sharpe, "max_dd": max_dd,
            "pf": pf, "avg_daily": avg_daily, "n_days": n_days,
            "n_sl": n_sl, "n_trail": n_trail, "n_close": n_close,
            "params": params,
        }
    
    return bt


def run():
    print("\n" + "═" * 100)
    print("  🧬 AGI Otimizador v7 — ITERAÇÃO 2")
    print("  " + "─" * 96)
    print("  WIN$: EMA Crossover + ADX (mantido da iteração 1)")
    print("  WDO$: VWAP adaptativo com restrições de qualidade (WR>=50%, PF>=1.5)")
    print("═" * 100)
    
    bt_win = _make_bt_func("WIN")
    bt_wdo = _make_bt_func("WDO")
    
    # WIN$ configs from iteration 1 (proven winners)
    win_configs = {
        "M5": {"ema_fast": 12, "ema_slow": 21, "adx_threshold": 20, "sl_mult": 1.5, "trail_activate": 1.5, "trail_distance": 0.3, "adx_period": 14, "atr_period": 14},
        "M15": {"ema_fast": 12, "ema_slow": 26, "adx_threshold": 20, "sl_mult": 1.0, "trail_activate": 1.5, "trail_distance": 0.3, "adx_period": 14, "atr_period": 14},
    }
    
    # WDO$ constrained sweep
    print("\n\n🔍 WDO$ M5 — Constrained Sweep (WR>=50%, PF>=1.5)...")
    df_wdo_m5 = fetch("WDO$", "M5", 500)
    wdo_m5_grid = {
        "vwap_period": [15, 20, 25],
        "buy_threshold": [1.001, 1.002, 1.003],
        "sell_threshold": [0.997, 0.998, 0.999],
        "sl_mult": [0.8, 1.0, 1.2],
        "trail_activate": [1.0, 1.5, 2.0],
        "trail_distance": [0.3, 0.5],
    }
    
    keys = list(wdo_m5_grid.keys())
    values = list(wdo_m5_grid.values())
    
    best_score = -999
    best_params = {}
    best_result = {}
    combos_tested = 0
    
    for combo in product(*values):
        params = dict(zip(keys, combo))
        params.update({"rsi_period": 14, "rsi_overbought": 70, "rsi_oversold": 30, "atr_period": 14})
        
        r = bt_wdo(df_wdo_m5, params)
        if r["ok"] and r["wr"] >= 50 and r["pf"] >= 1.5:
            # Score: weighted combination
            score = r["ret"] * 2 + r["sharpe"] * 0.1 + r["pf"] * 0.5 - r["max_dd"] * 10
            if score > best_score:
                best_score = score
                best_params = params.copy()
                best_result = r.copy()
        combos_tested += 1
    
    if best_result:
        print(f"\n  🏆 WDO$ M5 ({combos_tested} combos, filtrados WR>=50% PF>=1.5):")
        print(f"  Params: VWAP({best_params['vwap_period']}) Buy>{best_params['buy_threshold']} Sell<{best_params['sell_threshold']} SL={best_params['sl_mult']}x Trail={best_params['trail_activate']}x/{best_params['trail_distance']}x")
        print(f"  Ret={best_result['ret']:+.2f}% WR={best_result['wr']:.1f}% Sharpe={best_result['sharpe']:.2f} PF={best_result['pf']:.2f} R$/dia=R${best_result['avg_daily']:+.0f}")
        print(f"  Exit: SL={best_result['n_sl']} TRAIL={best_result['n_trail']} 1645={best_result['n_close']}")
    else:
        print("  ⚠️ Nenhum combo atendeu restrições. Usando config original.")
        best_params = {"vwap_period": 20, "buy_threshold": 1.003, "sell_threshold": 0.997, "sl_mult": 1.0, "trail_activate": 1.5, "trail_distance": 0.3, "rsi_period": 14, "rsi_overbought": 70, "rsi_oversold": 30, "atr_period": 14}
        best_result = bt_wdo(df_wdo_m5, best_params)
    
    wdo_m5_best = {"params": best_params, "result": best_result}
    
    # WDO$ M15
    print("\n\n🔍 WDO$ M15 — Constrained Sweep (WR>=50%, PF>=1.5)...")
    df_wdo_m15 = fetch("WDO$", "M15", 500)
    
    best_score = -999
    best_params = {}
    best_result = {}
    combos_tested = 0
    
    for combo in product(*values):
        params = dict(zip(keys, combo))
        params.update({"rsi_period": 14, "rsi_overbought": 70, "rsi_oversold": 30, "atr_period": 14})
        
        r = bt_wdo(df_wdo_m15, params)
        if r["ok"] and r["wr"] >= 50 and r["pf"] >= 1.5:
            score = r["ret"] * 2 + r["sharpe"] * 0.1 + r["pf"] * 0.5 - r["max_dd"] * 10
            if score > best_score:
                best_score = score
                best_params = params.copy()
                best_result = r.copy()
        combos_tested += 1
    
    if best_result:
        print(f"\n  🏆 WDO$ M15 ({combos_tested} combos, filtrados WR>=50% PF>=1.5):")
        print(f"  Params: VWAP({best_params['vwap_period']}) Buy>{best_params['buy_threshold']} Sell<{best_params['sell_threshold']} SL={best_params['sl_mult']}x Trail={best_params['trail_activate']}x/{best_params['trail_distance']}x")
        print(f"  Ret={best_result['ret']:+.2f}% WR={best_result['wr']:.1f}% Sharpe={best_result['sharpe']:.2f} PF={best_result['pf']:.2f} R$/dia=R${best_result['avg_daily']:+.0f}")
        print(f"  Exit: SL={best_result['n_sl']} TRAIL={best_result['n_trail']} 1645={best_result['n_close']}")
    else:
        print("  ⚠️ Nenhum combo atendeu restrições. Usando config original.")
        best_params = {"vwap_period": 20, "buy_threshold": 1.003, "sell_threshold": 0.997, "sl_mult": 1.0, "trail_activate": 1.5, "trail_distance": 0.3, "rsi_period": 14, "rsi_overbought": 70, "rsi_oversold": 30, "atr_period": 14}
        best_result = bt_wdo(df_wdo_m15, best_params)
    
    wdo_m15_best = {"params": best_params, "result": best_result}
    
    # WIN$ (from iteration 1)
    print("\n\n🔍 WIN$ — Usando configs vencedoras da iteração 1...")
    win_results = {}
    for tf, params in win_configs.items():
        df = fetch("WIN$", tf, 500)
        if not df.empty:
            r = bt_win(df, params)
            win_results[tf] = {"params": params, "result": r}
            print(f"  WIN$ {tf}: Ret={r['ret']:+.2f}% WR={r['wr']:.1f}% Sharpe={r['sharpe']:.2f} PF={r['pf']:.2f} R$/dia=R${r['avg_daily']:+.0f}")
    
    # ===== COMPARAÇÃO FINAL =====
    print("\n\n" + "═" * 100)
    print("  📊 COMPARAÇÃO: BASELINE vs ITERAÇÃO 2")
    print("═" * 100)
    
    baseline = {
        ("WDO$", "M5"): {"ret": 0.57, "wr": 77.8, "sharpe": 20.56, "pf": 6.29, "daily": 127},
        ("WDO$", "M15"): {"ret": 0.14, "wr": 57.1, "sharpe": 1.84, "pf": 1.30, "daily": 21},
        ("WIN$", "M5"): {"ret": -0.48, "wr": 26.7, "sharpe": -23.01, "pf": 0.44, "daily": -86},
        ("WIN$", "M15"): {"ret": -1.33, "wr": 37.9, "sharpe": -7.34, "pf": 0.64, "daily": -81},
    }
    
    optimized = {
        ("WDO$", "M5"): wdo_m5_best["result"],
        ("WDO$", "M15"): wdo_m15_best["result"],
        ("WIN$", "M5"): win_results.get("M5", {}).get("result", {}),
        ("WIN$", "M15"): win_results.get("M15", {}).get("result", {}),
    }
    
    print(f"\n{'Ativo':<8} {'TF':<4} {'Baseline':>10} {'Otimizado':>10} {'Δ':>8} {'WR Base':>8} {'WR Otim':>8} {'PF Base':>8} {'PF Otim':>8}")
    print("─" * 100)
    
    improvements = 0
    for sym, tf in [("WDO$", "M5"), ("WDO$", "M15"), ("WIN$", "M5"), ("WIN$", "M15")]:
        b = baseline[(sym, tf)]
        o = optimized[(sym, tf)]
        if not o: continue
        delta = o["ret"] - b["ret"]
        icon = "✅" if delta > 0 else "❌" if delta < -0.5 else "➡️"
        if delta > 0: improvements += 1
        print(f"  {sym:<6} {tf:<4} {b['ret']:>+7.2f}% {o['ret']:>+7.2f}% {delta:>+6.2f}% {b['wr']:>6.1f}% {o['wr']:>6.1f}% {b['pf']:>6.2f} {o['pf']:>6.2f} {icon}")
    
    print(f"\n  📈 Combos melhorados: {improvements}/4")
    
    # Final configs
    print("\n\n  🏆 CONFIGURAÇÕES FINAIS VENCEDORAS:")
    print("  " + "─" * 80)
    
    final_configs = {
        "WDO$ M5": wdo_m5_best,
        "WDO$ M15": wdo_m15_best,
        "WIN$ M5": win_results.get("M5", {}),
        "WIN$ M15": win_results.get("M15", {}),
    }
    
    for name, data in final_configs.items():
        if not data: continue
        p = data["params"]
        r = data["result"]
        if not r: continue
        print(f"\n  📌 {name}:")
        print(f"     Params: {p}")
        print(f"     Ret={r['ret']:+.2f}% WR={r['wr']:.1f}% Sharpe={r['sharpe']:.2f} PF={r['pf']:.2f} R$/dia=R${r['avg_daily']:+.0f}")
    
    print("\n" + "═" * 100 + "\n")
    
    return {
        "wdo_m5": wdo_m5_best,
        "wdo_m15": wdo_m15_best,
        "win_m5": win_results.get("M5", {}),
        "win_m15": win_results.get("M15", {}),
    }


if __name__ == "__main__":
    results = run()
