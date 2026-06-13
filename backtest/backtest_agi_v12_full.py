"""
backtest_agi_v12_full.py — AGI v12 completo: 6 ativos × 4 timeframes.

Ativos: WIN, WDO, BIT, DOL, WSP, IND
Timeframes: M5, M15, M30, H1
Estratégia por ativo/TF: a melhor descoberta nos testes anteriores
"""

import sys, csv, io, subprocess, os, json
from pathlib import Path
from datetime import datetime, time
import numpy as np
import pandas as pd

WINE_PYTHON = os.path.expanduser("~/.wine/drive_c/Python311/python.exe")
FETCH_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mt5_fetch.py")

CONTRACT_SPECS = {
    "WIN$":  {"mult": 0.20, "name": "Mini Índice",    "margin": 155,  "slip_r": 1.0},
    "WDO$":  {"mult": 10.0, "name": "Mini Dólar",     "margin": 140,  "slip_r": 5.0},
    "BIT$":  {"mult": 0.50, "name": "Bitcoin",        "margin": 45,   "slip_r": 10.0},
    "DOL$":  {"mult": 50.0, "name": "Dólar Cheio",    "margin": 700,  "slip_r": 10.0},
    "WSP$":  {"mult": 2.5,  "name": "Micro S&P 500",  "margin": 100,  "slip_r": 2.5},
    "IND$":  {"mult": 1.00, "name": "Índice Bovespa", "margin": 775,  "slip_r": 5.0},
}

COMMISSION = 1.2
CLOSE_HOUR, CLOSE_MINUTE = 16, 45
START_HOUR, START_MINUTE = 9, 5
ATR_PERIOD = 14

# Melhor estratégia por ativo (descoberta no teste anterior)
BEST_STRATEGY = {
    "WIN$": "BOLLINGER",
    "WDO$": "EMA_PULLBACK",
    "BIT$": "VWAP",
    "DOL$": "EMA_PULLBACK",
    "WSP$": "MACD_MOMENTUM",
    "IND$": "BOLLINGER",
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


# ─── Indicators ──────────────────────────────────────────────────────────────

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

def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))

def calc_adx(df, period=14):
    high, low, close = df["high"], df["low"], df["close"]
    plus_dm = high.diff()
    minus_dm = low.diff().mul(-1)
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
    tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/period, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1/period, min_periods=period).mean() / atr.replace(0, 1e-10)
    minus_di = 100 * minus_dm.ewm(alpha=1/period, min_periods=period).mean() / atr.replace(0, 1e-10)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-10)
    adx = dx.ewm(alpha=1/period, min_periods=period).mean()
    return adx, plus_di, minus_di

def calc_macd(df, fast=12, slow=26, signal=9):
    ema_fast = calc_ema(df["close"], fast)
    ema_slow = calc_ema(df["close"], slow)
    macd_line = ema_fast - ema_slow
    signal_line = calc_ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram

def calc_bollinger(df, period=20, std=2.0):
    mid = df["close"].rolling(period).mean()
    std_val = df["close"].rolling(period).std()
    upper = mid + std * std_val
    lower = mid - std * std_val
    return upper, mid, lower


# ─── Strategy Checks ─────────────────────────────────────────────────────────

def check_vwap(price, cur_atr_pct, ema_f, ema_s, vwap_val, rsi_val):
    if vwap_val == 0: return None
    if cur_atr_pct < 0.0015: bm, sm = 1.0005, 0.9995
    elif cur_atr_pct < 0.003: bm, sm = 1.0015, 0.9985
    else: bm, sm = 1.002, 0.998
    d = None
    if price > vwap_val * bm: d = "BUY"
    elif price < vwap_val * sm: d = "SELL"
    if not d: return None
    if ema_f > 0 and ema_s > 0:
        if d == "BUY" and ema_f < ema_s: return None
        if d == "SELL" and ema_f > ema_s: return None
    if not pd.isna(rsi_val):
        if d == "BUY" and rsi_val > 85: return None
        if d == "SELL" and rsi_val < 15: return None
    return d

def check_ema_pullback(price, ema_f, ema_s, adx, pdi, mdi, rsi):
    if pd.isna(adx) or adx < 20: return None
    if pd.isna(ema_f) or pd.isna(ema_s) or ema_s == 0: return None
    up = ema_f > ema_s; dn = ema_f < ema_s
    if not up and not dn: return None
    if not pd.isna(pdi) and not pd.isna(mdi):
        if up and pdi < mdi: return None
        if dn and mdi < pdi: return None
    d = "BUY" if up else "SELL"
    if d == "BUY" and price < ema_s * 0.998: return None
    if d == "SELL" and price > ema_s * 1.002: return None
    if not pd.isna(rsi):
        if d == "BUY" and rsi > 80: return None
        if d == "SELL" and rsi < 20: return None
    return d

def check_macd_momentum(price, ema_f, ema_s, adx, rsi, h, ph, p2h):
    if pd.isna(adx) or adx < 15: return None
    if pd.isna(ema_f) or pd.isna(ema_s) or ema_s == 0: return None
    up = ema_f > ema_s; dn = ema_f < ema_s
    if not up and not dn: return None
    cu = ph <= 0 and h > 0; cd = ph >= 0 and h < 0
    mu = h > 0 and h > ph and ph > p2h; md = h < 0 and h < ph and ph < p2h
    d = None
    if up and (cu or mu):
        if not pd.isna(rsi) and rsi > 75: return None
        d = "BUY"
    elif dn and (cd or md):
        if not pd.isna(rsi) and rsi < 25: return None
        d = "SELL"
    if not d: return None
    if d == "BUY" and price < ema_s * 0.995: return None
    if d == "SELL" and price > ema_s * 1.005: return None
    return d

def check_bollinger(price, rsi, bu, bl):
    if bu == 0 or bl == 0 or pd.isna(bu): return None
    d = None
    if price <= bl: d = "BUY"
    elif price >= bu: d = "SELL"
    if not d: return None
    if not pd.isna(rsi):
        if d == "BUY" and rsi > 30: return None
        if d == "SELL" and rsi < 70: return None
    return d


# ─── Backtest Engine ─────────────────────────────────────────────────────────

def backtest(df, symbol, tf, strategy, *, capital=1_000_000.0):
    spec = CONTRACT_SPECS[symbol]
    mult = spec["mult"]
    slip_r = spec["slip_r"]
    
    atr = calc_atr(df, ATR_PERIOD)
    _z = pd.Series(0.0, index=df.index)
    
    # Calculate indicators per strategy
    vwap = calc_vwap(df, 20) if strategy == "VWAP" else _z
    ema_f = calc_ema(df["close"], 9) if strategy in ("VWAP", "EMA_PULLBACK", "MACD_MOMENTUM") else _z
    ema_s = calc_ema(df["close"], 21) if strategy in ("VWAP", "EMA_PULLBACK", "MACD_MOMENTUM") else _z
    rsi = calc_rsi(df["close"], 14)
    adx_v, pdi, mdi = calc_adx(df, 14) if strategy in ("EMA_PULLBACK", "MACD_MOMENTUM") else (_z, _z, _z)
    _, _, hist = calc_macd(df) if strategy == "MACD_MOMENTUM" else (_z, _z, _z)
    bbu, bbm, bbl = calc_bollinger(df) if strategy == "BOLLINGER" else (_z, _z, _z)
    
    sl_mult = 1.0 if strategy == "BOLLINGER" else 1.5
    trail_act = 1.5
    trail_dist = 0.5
    cooldown = 300
    max_daily = 8
    
    cash = capital; pos = 0; ep = 0.0; e_date = None; e_atr = 0.0
    best_p = 0.0; sl_p = 0.0; trail_on = False; sl_pts = 0; bars_in = 0
    trades = []; daily = {}; last_tt = None
    
    def _close(price, reason, date):
        nonlocal cash, pos, ep, e_date, best_p, sl_p, trail_on, e_atr, sl_pts, bars_in
        if pos == 0: return
        pnl = ((price - ep) * mult - slip_r - COMMISSION) if pos == 1 else ((ep - price) * mult - slip_r - COMMISSION)
        cash += pnl
        trades.append({"dir": "BUY" if pos == 1 else "SELL", "et": e_date, "xt": date,
                       "ep": ep, "xp": price, "pnl": pnl, "reason": reason, "bars": bars_in})
        pos = 0; ep = 0; best_p = 0; sl_p = 0; trail_on = False; bars_in = 0
    
    def _open(d, price, date, cur_atr):
        nonlocal cash, pos, ep, e_date, best_p, sl_p, trail_on, e_atr, sl_pts, last_tt
        if pos != 0: return False
        if last_tt is not None and (date - last_tt).total_seconds() < cooldown: return False
        dd = date.date() if hasattr(date, 'date') else date
        if daily.get(dd, 0) >= max_daily: return False
        raw = int(cur_atr * sl_mult)
        raw = max(raw, 50); raw = ((raw + 4) // 5) * 5
        if raw <= 0: return False
        pos = 1 if d == "BUY" else -1
        ep = price; e_date = date; e_atr = cur_atr; sl_pts = raw; best_p = price; trail_on = False
        sl_p = price - raw if pos == 1 else price + raw
        daily[dd] = daily.get(dd, 0) + 1; last_tt = date
        return True
    
    for i, (date, row) in enumerate(df.iterrows()):
        price = float(row["close"]); high = float(row["high"]); low = float(row["low"])
        hour = int(row["hour"]); minute = int(row["minute"])
        cur_atr = float(atr.iloc[i]) if i > 0 and not pd.isna(atr.iloc[i]) else 0
        
        if hour < START_HOUR or (hour == START_HOUR and minute < START_MINUTE): continue
        
        if pos != 0:
            bars_in += 1
            if pos == 1: best_p = max(best_p, high)
            else: best_p = min(best_p, low) if best_p > 0 else low
            profit = (best_p - ep) if pos == 1 else (ep - best_p)
            tfm = {"M5": 5, "M15": 15, "M30": 30, "H1": 60}.get(tf, 5)
            pm = bars_in * tfm
            if not trail_on and e_atr > 0 and profit >= trail_act * e_atr: trail_on = True
            if trail_on and e_atr > 0:
                td = trail_dist * e_atr
                if pos == 1:
                    ns = best_p - td
                    if ns > sl_p: sl_p = ns
                else:
                    ns = best_p + td
                    if ns < sl_p: sl_p = ns
            if sl_p > 0:
                if pos == 1 and low <= sl_p: _close(sl_p, "SL", date); continue
                elif pos == -1 and high >= sl_p: _close(sl_p, "SL", date); continue
            if hour > CLOSE_HOUR or (hour == CLOSE_HOUR and minute >= CLOSE_MINUTE):
                _close(price, "1645", date); continue
            continue
        
        if cur_atr <= 0: continue
        d = None
        
        if strategy == "VWAP":
            cv = float(vwap.iloc[i]) if not pd.isna(vwap.iloc[i]) else 0
            ef = float(ema_f.iloc[i]) if not pd.isna(ema_f.iloc[i]) else 0
            es = float(ema_s.iloc[i]) if not pd.isna(ema_s.iloc[i]) else 0
            cr = float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else 50
            cap = cur_atr / price if price > 0 else 0
            d = check_vwap(price, cap, ef, es, cv, cr)
        elif strategy == "EMA_PULLBACK":
            ef = float(ema_f.iloc[i]) if not pd.isna(ema_f.iloc[i]) else 0
            es = float(ema_s.iloc[i]) if not pd.isna(ema_s.iloc[i]) else 0
            ax = float(adx_v.iloc[i]) if not pd.isna(adx_v.iloc[i]) else 0
            pi = float(pdi.iloc[i]) if not pd.isna(pdi.iloc[i]) else 0
            mi = float(mdi.iloc[i]) if not pd.isna(mdi.iloc[i]) else 0
            cr = float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else 50
            d = check_ema_pullback(price, ef, es, ax, pi, mi, cr)
        elif strategy == "MACD_MOMENTUM":
            ef = float(ema_f.iloc[i]) if not pd.isna(ema_f.iloc[i]) else 0
            es = float(ema_s.iloc[i]) if not pd.isna(ema_s.iloc[i]) else 0
            ax = float(adx_v.iloc[i]) if not pd.isna(adx_v.iloc[i]) else 0
            cr = float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else 50
            ch = float(hist.iloc[i]) if not pd.isna(hist.iloc[i]) else 0
            ph = float(hist.iloc[i-1]) if i > 0 and not pd.isna(hist.iloc[i-1]) else 0
            p2h = float(hist.iloc[i-2]) if i > 1 and not pd.isna(hist.iloc[i-2]) else 0
            d = check_macd_momentum(price, ef, es, ax, cr, ch, ph, p2h)
        elif strategy == "BOLLINGER":
            bu = float(bbu.iloc[i]) if not pd.isna(bbu.iloc[i]) else 0
            bl = float(bbl.iloc[i]) if not pd.isna(bbl.iloc[i]) else 0
            cr = float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else 50
            d = check_bollinger(price, cr, bu, bl)
        
        if d: _open(d, price, date, cur_atr)
    
    if pos != 0: _close(float(df["close"].iloc[-1]), "FORCE", df.index[-1])
    return trades


def run():
    print("\n" + "═" * 95)
    print("  🧪 AGI v12 FULL — 6 Ativos × 4 Timeframes (M5/M15/M30/H1)")
    print("  " + "─" * 91)
    for sym, strat in BEST_STRATEGY.items():
        name = CONTRACT_SPECS[sym]["name"]
        print(f"    {sym} ({name}) → {strat}")
    print("═" * 95)
    
    timeframes = ["M5", "M15", "M30", "H1"]
    tf_bars = {"M5": 500, "M15": 500, "M30": 300, "H1": 200}
    all_results = []
    
    for symbol in CONTRACT_SPECS:
        spec = CONTRACT_SPECS[symbol]
        strategy = BEST_STRATEGY[symbol]
        
        print(f"\n{'━' * 95}")
        print(f"  📡 {symbol} — {spec['name']} (margin R$ {spec['margin']}, mult R$ {spec['mult']}) → {strategy}")
        print(f"{'━' * 95}")
        
        for tf in timeframes:
            n_bars = tf_bars[tf]
            df = fetch(symbol, tf, n_bars)
            
            if df.empty:
                print(f"  {tf:>3} │ ❌ Sem dados")
                continue
            
            n_days = df["date"].nunique()
            p0, p1 = float(df["close"].iloc[0]), float(df["close"].iloc[-1])
            
            trades = backtest(df, symbol, tf, strategy)
            
            if trades:
                n = len(trades)
                wins = sum(1 for t in trades if t["pnl"] > 0)
                pnl = sum(t["pnl"] for t in trades)
                wr = wins / n * 100
                gw = sum(t["pnl"] for t in trades if t["pnl"] > 0)
                gl = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0))
                pf = gw / gl if gl > 0 else 999
                
                # PnL per day
                pnl_per_day = pnl / n_days if n_days > 0 else pnl
                
                # Exit reasons
                reasons = {}
                for t in trades:
                    r = t["reason"]
                    reasons[r] = reasons.get(r, 0) + 1
                reason_str = " ".join(f"{k}:{v}" for k, v in sorted(reasons.items()))
                
                icon = "🟢" if pnl > 0 else "🔴"
                print(f"  {tf:>3} │ {icon} {n:>3}t │ WR {wr:>5.1f}% │ PF {pf:>5.2f} │ R$ {pnl:>+10.1f} │ R$/dia {pnl_per_day:>+8.1f} │ {n_days}d │ {reason_str}")
                
                all_results.append({
                    "symbol": symbol, "name": spec["name"], "tf": tf,
                    "strategy": strategy, "trades": n, "wr": wr, "pf": pf,
                    "pnl": pnl, "pnl_day": pnl_per_day, "days": n_days,
                    "margin": spec["margin"], "mult": spec["mult"],
                })
            else:
                print(f"  {tf:>3} │ ⚪   0t │ sem trades")
        
        # Per-symbol summary
        sym_results = [r for r in all_results if r["symbol"] == symbol]
        if sym_results:
            total_pnl = sum(r["pnl"] for r in sym_results)
            total_trades = sum(r["trades"] for r in sym_results)
            avg_pnl_day = sum(r["pnl_day"] for r in sym_results)
            icon = "🟢" if total_pnl > 0 else "🔴"
            print(f"  {'':>3} │ {icon} ─── TOTAL {symbol}: R$ {total_pnl:+.1f} ({total_trades} trades, ~R$ {avg_pnl_day:+.0f}/dia)")
    
    # ─── GLOBAL SUMMARY ───
    print("\n\n" + "═" * 95)
    print("  📋 RESUMO GLOBAL — AGI v12 FULL (6 ativos × 4 TFs)")
    print("═" * 95)
    
    # Per-symbol total
    print(f"\n  {'Ativo':<6} {'Nome':<16} │ {'Strategy':<16} │ {'Total PnL':>10} │ {'Trades':>6} │ {'R$/dia':>8} │ {'Margin':>7}")
    print("  " + "─" * 85)
    
    grand_pnl = 0; grand_trades = 0; grand_margin = 0
    
    seen = {}
    for r in all_results:
        if r["symbol"] not in seen:
            seen[r["symbol"]] = {"pnl": 0, "trades": 0, "pnl_day": 0, "margin": r["margin"], "strategy": r["strategy"], "name": r["name"]}
        seen[r["symbol"]]["pnl"] += r["pnl"]
        seen[r["symbol"]]["trades"] += r["trades"]
        seen[r["symbol"]]["pnl_day"] += r["pnl_day"]
    
    ranked = sorted(seen.items(), key=lambda x: x[1]["pnl"], reverse=True)
    
    for sym, data in ranked:
        icon = "🟢" if data["pnl"] > 0 else "🔴"
        print(f"  {icon} {sym:<6} {data['name']:<16} │ {data['strategy']:<16} │ R$ {data['pnl']:>+8.1f} │ {data['trades']:>6} │ R$ {data['pnl_day']:>+6.0f} │ R$ {data['margin']:>5}")
        grand_pnl += data["pnl"]
        grand_trades += data["trades"]
        grand_margin += data["margin"]
    
    print("  " + "─" * 85)
    print(f"  {'TOTAL':<6} {'':16} │ {'':16} │ R$ {grand_pnl:>+8.1f} │ {grand_trades:>6} │        │ R$ {grand_margin:>5}")
    
    # Per-TF summary
    print(f"\n\n  ⏱️  RESUMO POR TIMEFRAME:")
    print("  " + "─" * 60)
    for tf in timeframes:
        tf_res = [r for r in all_results if r["tf"] == tf]
        if tf_res:
            tf_pnl = sum(r["pnl"] for r in tf_res)
            tf_trades = sum(r["trades"] for r in tf_res)
            profitable = sum(1 for r in tf_res if r["pnl"] > 0)
            icon = "🟢" if tf_pnl > 0 else "🔴"
            print(f"  {icon} {tf:>3}: R$ {tf_pnl:>+8.1f} ({tf_trades} trades, {profitable}/{len(tf_res)} ativos lucrativos)")
    
    # ROI
    if grand_margin > 0:
        roi = grand_pnl / grand_margin * 100
        print(f"\n  💰 PnL total: R$ {grand_pnl:+.1f}")
        print(f"  💰 Margin total (1 contrato cada): R$ {grand_margin}")
        print(f"  💰 ROI: {roi:.1f}%")
    
    # Recommendation
    print(f"\n\n  💡 RECOMENDAÇÃO AGI v12:")
    print("  " + "─" * 60)
    for sym, data in ranked:
        if data["pnl"] > 0:
            print(f"  ✅ {sym} ({data['name']}) — {data['strategy']} — R$ {data['pnl_day']:+.0f}/dia")
    
    print("\n" + "═" * 95 + "\n")


if __name__ == "__main__":
    run()
