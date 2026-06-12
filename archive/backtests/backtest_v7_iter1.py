"""
backtest_v7_iter1.py — Split strategy backtest matching vt_autotrader.py logic.

WIN$: EMA(12/21) + ADX > 15 + RSI filter → trend-following
WDO$: VWAP(15) + EMA trend filter + RSI filter → VWAP reversion

Key improvements over v6:
1. Split strategy per symbol (matches autotrader)
2. WDO: VWAP period 15 (optimized), tighter thresholds
3. WIN: EMA crossover + ADX filter
4. Better trailing: 1.0x ATR activate, 0.2x ATR distance
5. Cooldown between trades
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

# === Strategy params (to be optimized) ===
PARAMS = {
    "WIN": {
        "ema_fast": 12,
        "ema_slow": 21,
        "adx_period": 14,
        "adx_threshold": 15,
        "rsi_period": 14,
        "rsi_overbought": 70,
        "rsi_oversold": 30,
        "sl_atr_mult": 1.5,
        "trail_activate": 1.0,
        "trail_distance": 0.2,
        "sl_min": 100,
        "cooldown_bars": 3,  # min bars between trades
    },
    "WDO": {
        "vwap_period": 15,
        "vwap_buy_threshold": 1.001,
        "vwap_sell_threshold": 0.999,
        "ema_fast": 9,
        "ema_slow": 21,
        "trend_min_spread": 0.001,
        "rsi_period": 14,
        "rsi_overbought": 70,
        "rsi_oversold": 30,
        "sl_atr_mult": 1.0,
        "trail_activate": 1.5,
        "trail_distance": 0.2,
        "sl_min": 200,
        "cooldown_bars": 3,
    },
}


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


def backtest_split(df, symbol, params=None, capital=100_000.0):
    """Split strategy backtest — matches autotrader logic."""
    if params is None:
        root = "WIN" if "WIN" in symbol else "WDO"
        params = PARAMS[root]
    
    spec = CONTRACT_SPECS[symbol]
    mult, margin, slip_r = spec["mult"], spec["margin"], spec["slip_r"]
    is_win = "WIN" in symbol
    is_wdo = "WDO" in symbol
    
    # Indicators
    atr = calc_atr(df, 14)
    rsi = calc_rsi(df, params["rsi_period"])
    
    if is_win:
        ema_fast = calc_ema(df["close"], params["ema_fast"])
        ema_slow = calc_ema(df["close"], params["ema_slow"])
        adx, plus_di, minus_di = calc_adx(df, params["adx_period"])
    else:  # WDO
        vwap = calc_vwap(df, params["vwap_period"])
        ema_fast = calc_ema(df["close"], params["ema_fast"])
        ema_slow = calc_ema(df["close"], params["ema_slow"])
    
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
    last_trade_bar = -999
    
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
        
        sl_cost = slip_r * MAX_CT
        comm = COMMISSION * MAX_CT
        
        if pos == 1:
            pnl = (price - ep) * mult * MAX_CT - sl_cost - comm
            n_long += 1
        else:
            pnl = (ep - price) * mult * MAX_CT - sl_cost - comm
            n_short += 1
        
        cash += margin * MAX_CT + pnl
        n_trades += 1
        last_trade_bar_pos = i
        
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
    
    def _open(direction, price, date, cur_atr):
        nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, e_atr, sl_pts, bars_in_trade, last_trade_bar
        
        if pos != 0:
            return False
        
        # Cooldown
        if i - last_trade_bar < params.get("cooldown_bars", 3):
            return False
        
        raw_sl = int(cur_atr * params["sl_atr_mult"])
        raw_sl = max(raw_sl, params["sl_min"])
        raw_sl = ((raw_sl + 4) // 5) * 5
        
        cost = slip_r * MAX_CT + COMMISSION * MAX_CT
        if cash >= margin * MAX_CT + cost:
            cash -= margin * MAX_CT + cost
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
    
    for i, (date, row) in enumerate(df.iterrows()):
        price = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        hour = int(row["hour"])
        minute = int(row["minute"])
        cur_atr = float(atr.iloc[i]) if not pd.isna(atr.iloc[i]) else 0
        cur_rsi = float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else 50
        
        # Mark-to-market
        if pos == 1:
            eq_val = cash + (price - ep) * mult * MAX_CT + margin * MAX_CT
        elif pos == -1:
            eq_val = cash + (ep - price) * mult * MAX_CT + margin * MAX_CT
        else:
            eq_val = cash
        equity.append(eq_val)
        
        # ===== NO POSITION: CHECK ENTRY =====
        if pos == 0:
            if cur_atr <= 0:
                continue
            
            if is_win:
                # WIN: EMA Crossover + ADX
                if i < max(params["ema_slow"], params["adx_period"]) + 5:
                    continue
                
                cur_ema_fast = float(ema_fast.iloc[i])
                cur_ema_slow = float(ema_slow.iloc[i])
                cur_adx = float(adx.iloc[i])
                cur_plus_di = float(plus_di.iloc[i])
                cur_minus_di = float(minus_di.iloc[i])
                
                # prev values for crossover detection
                prev_ema_fast = float(ema_fast.iloc[i-1])
                prev_ema_slow = float(ema_slow.iloc[i-1])
                
                if cur_adx < params["adx_threshold"]:
                    continue
                
                direction = None
                # BUY: EMA fast crosses above slow + plus_di > minus_di
                if prev_ema_fast <= prev_ema_slow and cur_ema_fast > cur_ema_slow:
                    if cur_plus_di > cur_minus_di:
                        direction = "BUY"
                # SELL: EMA fast crosses below slow + minus_di > plus_di
                elif prev_ema_fast >= prev_ema_slow and cur_ema_fast < cur_ema_slow:
                    if cur_minus_di > cur_plus_di:
                        direction = "SELL"
                
                if not direction:
                    continue
                
                # RSI filter
                if direction == "BUY" and cur_rsi > params["rsi_overbought"]:
                    continue
                if direction == "SELL" and cur_rsi < params["rsi_oversold"]:
                    continue
                
                _open(direction, price, date, cur_atr)
                last_trade_bar = i
                
            else:
                # WDO: VWAP strategy
                cur_vwap = float(vwap.iloc[i]) if not pd.isna(vwap.iloc[i]) else 0
                if cur_vwap <= 0:
                    continue
                
                # Trend filter (EMA)
                cur_ema_fast = float(ema_fast.iloc[i]) if not pd.isna(ema_fast.iloc[i]) else 0
                cur_ema_slow = float(ema_slow.iloc[i]) if not pd.isna(ema_slow.iloc[i]) else 0
                
                # Market regime — skip choppy
                if cur_ema_fast > 0 and cur_ema_slow > 0:
                    spread = abs(cur_ema_fast - cur_ema_slow) / price
                    if spread < params.get("trend_min_spread", 0.001):
                        continue  # choppy
                
                buy_thresh = cur_vwap * params["vwap_buy_threshold"]
                sell_thresh = cur_vwap * params["vwap_sell_threshold"]
                
                direction = None
                if price > buy_thresh:
                    direction = "BUY"
                    # Trend filter: only buy in uptrend
                    if cur_ema_fast > 0 and cur_ema_slow > 0 and cur_ema_fast < cur_ema_slow:
                        continue
                elif price < sell_thresh:
                    direction = "SELL"
                    # Trend filter: only sell in downtrend
                    if cur_ema_fast > 0 and cur_ema_slow > 0 and cur_ema_fast > cur_ema_slow:
                        continue
                
                if not direction:
                    continue
                
                # RSI filter
                if direction == "BUY" and cur_rsi > params["rsi_overbought"]:
                    continue
                if direction == "SELL" and cur_rsi < params["rsi_oversold"]:
                    continue
                
                _open(direction, price, date, cur_atr)
                last_trade_bar = i
            continue
        
        # ===== POSITION OPEN =====
        bars_in_trade += 1
        
        # Update best price
        if pos == 1:
            best = max(best, high)
        elif pos == -1:
            best = min(best, low) if best > 0 else low
        
        # Profit in points
        if pos == 1:
            profit_pts = best - ep
        else:
            profit_pts = ep - best
        
        # Activate trailing?
        if not trail_on and e_atr > 0 and profit_pts >= params["trail_activate"] * e_atr:
            trail_on = True
        
        # Trailing stop
        if trail_on and e_atr > 0:
            trail_dist = params["trail_distance"] * e_atr
            if pos == 1:
                new_sl = best - trail_dist
                if new_sl > sl_price:
                    sl_price = new_sl
            elif pos == -1:
                new_sl = best + trail_dist
                if new_sl < sl_price:
                    sl_price = new_sl
        
        # SL check
        if sl_price > 0:
            if pos == 1 and low <= sl_price:
                _close(sl_price, "SL"); continue
            elif pos == -1 and high >= sl_price:
                _close(sl_price, "SL"); continue
        
        # 16:45 close
        if hour > CLOSE_HOUR or (hour == CLOSE_HOUR and minute >= CLOSE_MINUTE):
            _close(price, "1645"); continue
    
    # Force close
    if pos != 0:
        _close(float(df["close"].iloc[-1]), "FORCE")
    
    # Stats
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
    
    wins_p = [t["pnl"] for t in trade_log if t["pnl"] > 0]
    losses_p = [t["pnl"] for t in trade_log if t["pnl"] <= 0]
    avg_win = np.mean(wins_p) if wins_p else 0
    avg_loss = abs(np.mean(losses_p)) if losses_p else 1
    payoff = avg_win / avg_loss if avg_loss > 0 else 0
    
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
        "avg_bars": avg_bars,
        "reasons": pnl_by_reason,
        "trade_log": trade_log, "equity": eq,
        "daily_pnl": daily_pnl_dict,
    }


def run():
    print("\n" + "═" * 100)
    print("  🤖 BACKTEST v7 — SPLIT STRATEGY (EMA+ADX for WIN, VWAP for WDO)")
    print("  " + "─" * 96)
    print("  ✅ WIN: EMA(12/21) + ADX>15 + RSI filter")
    print("  ✅ WDO: VWAP(15) + EMA trend filter + RSI filter")
    print("  ✅ SL: 1.5x ATR (WIN) / 1.0x ATR (WDO)")
    print("  ✅ Trailing: WIN 1.0x/0.2x | WDO 1.5x/0.2x")
    print("  ✅ Cooldown: 3 bars between trades")
    print("  ✅ 1 contrato | Fecha 16:45 BRT")
    print("═" * 100)
    
    combos = [
        ("WIN$", "M5", 500),
        ("WIN$", "M15", 500),
        ("WDO$", "M5", 500),
        ("WDO$", "M15", 500),
    ]
    all_results = []
    
    for sym, tf, n_bars in combos:
        spec = CONTRACT_SPECS[sym]
        print(f"\n📡 {sym} ({spec['name']}) {tf} — {n_bars} barras...")
        df = fetch(sym, tf, n_bars)
        if df.empty:
            print("  ❌ Sem dados"); continue
        
        p0, p1 = float(df["close"].iloc[0]), float(df["close"].iloc[-1])
        n_days = df["date"].nunique()
        atr_avg = calc_atr(df, 14).mean()
        
        print(f"  ✅ {len(df)} barras, {n_days} dias | {df.index[0].strftime('%d/%m')} → {df.index[-1].strftime('%d/%m')}")
        print(f"     {p0:.2f} → {p1:.2f} ({(p1/p0-1)*100:+.2f}%)")
        print(f"     ATR médio: {atr_avg:.1f} pts")
        
        root = "WIN" if "WIN" in sym else "WDO"
        r = backtest_split(df, sym)
        if r["ok"]:
            strat_name = "EMA(12/21)+ADX" if root == "WIN" else "VWAP(15)"
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
    
    # RANKING
    if all_results:
        ranking = sorted(all_results, key=lambda x: x["ret"], reverse=True)
        
        print("\n\n" + "═" * 100)
        print("  📋 RANKING GERAL — v7 (Split Strategy)")
        print("═" * 100)
        print(f"\n{'#':>2} {'Ativo':<7} {'TF':<4} {'Strategy':<16} {'Ret%':>7} {'T':>4} {'WR':>6} {'Sharpe':>7} {'DD':>7} {'PF':>6} {'R$/dia':>9}")
        print("─" * 100)
        for i, r in enumerate(ranking, 1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i:>2}"
            print(f"{medal:>2} {r['symbol']:<7} {r['tf']:<4} {r['strategy']:<16} "
                  f"{r['ret']:>+6.2f}% {r['trades']:>3}  "
                  f"{r['wr']:>5.1f}% {r['sharpe']:>6.2f}  "
                  f"{r['max_dd']:>6.2f}% {r['pf']:>5.2f} "
                  f"R${r['avg_daily']:>+8.1f}")
        
        # Comparison with v6
        v6_data = {
            ("WIN$", "M5"): {"ret": -0.48, "wr": 26.7, "sharpe": -23.01, "pf": 0.44, "r_dia": -85.6},
            ("WIN$", "M15"): {"ret": -1.33, "wr": 37.9, "sharpe": -7.34, "pf": 0.64, "r_dia": -80.7},
            ("WDO$", "M5"): {"ret": 0.57, "wr": 77.8, "sharpe": 20.56, "pf": 6.29, "r_dia": 127.0},
            ("WDO$", "M15"): {"ret": 0.14, "wr": 57.1, "sharpe": 1.84, "pf": 1.30, "r_dia": 21.0},
        }
        
        print("\n\n" + "═" * 100)
        print("  📈 COMPARAÇÃO v7 vs v6 (Autotrader)")
        print("═" * 100)
        print(f"\n{'Ativo':<10} {'TF':<4} {'v6 Ret%':>8} {'v7 Ret%':>8} {'Δ':>7} {'v6 WR':>7} {'v7 WR':>7} {'v6 Sharpe':>9} {'v7 Sharpe':>9}")
        print("─" * 100)
        for r in all_results:
            key = (r["symbol"], r["tf"])
            if key in v6_data:
                v6 = v6_data[key]
                delta = r["ret"] - v6["ret"]
                icon = "✅" if delta > 0 else "❌" if delta < -0.5 else "➡️"
                print(f"  {r['symbol']:<7} {r['tf']:<4} {v6['ret']:>+7.2f}% {r['ret']:>+7.2f}% {delta:>+6.2f}% {v6['wr']:>6.1f}% {r['wr']:>6.1f}% {v6['sharpe']:>8.2f} {r['sharpe']:>8.2f} {icon}")
        
        # Total
        total_ret_v6 = sum(v["ret"] for v in v6_data.values())
        total_ret_v7 = sum(r["ret"] for r in all_results)
        print(f"\n  📊 Total v6: {total_ret_v6:+.2f}% | Total v7: {total_ret_v7:+.2f}% | Δ: {total_ret_v7-total_ret_v6:+.2f}%")
        
        profitable = [x for x in all_results if x["ret"] > 0]
        print(f"  💰 Lucrativos: {len(profitable)}/{len(all_results)}")
        for p in profitable:
            print(f"    ✅ {p['symbol']} {p['tf']} — {p['ret']:+.2f}% | "
                  f"Sharpe {p['sharpe']:.2f} | PF {p['pf']:.2f} | R${p['avg_daily']:+.0f}/dia")
        
        # Save CSV
        out = Path("/tmp/backtest_v7_iter1.csv")
        rows_csv = []
        for r in all_results:
            rows_csv.append({
                "symbol": r["symbol"], "tf": r["tf"], "strategy": r["strategy"],
                "ret": r["ret"], "trades": r["trades"], "wr": r["wr"],
                "sharpe": r["sharpe"], "max_dd": r["max_dd"], "pf": r["pf"],
                "avg_daily": r["avg_daily"], "n_days": r["n_days"],
                "payoff": r["payoff"], "avg_bars": r["avg_bars"],
            })
        pd.DataFrame(rows_csv).to_csv(out, index=False)
        print(f"\n  💾 CSV: {out}")
    
    print("\n" + "═" * 100 + "\n")


if __name__ == "__main__":
    run()
