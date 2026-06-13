"""
backtest_multi_index.py — Backtest completo nos principais futuros B3.

Testa WIN, WDO, BIT, ISP, GLD, IND, DOL, WSP com múltiplas estratégias.
Seleciona os melhores para day trade.
"""

import sys, csv, io, subprocess, os, json
from pathlib import Path
from datetime import datetime, time
import numpy as np
import pandas as pd

# ─── MT5 fetch ───────────────────────────────────────────────────────────────
WINE_PYTHON = os.path.expanduser("~/.wine/drive_c/Python311/python.exe")
FETCH_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mt5_fetch.py")

# Contract specs: symbol → {mult, name, margin, tick, slip_r}
CONTRACT_SPECS = {
    "WIN$":  {"mult": 0.20, "name": "Mini Índice",      "margin": 155,  "slip_r": 1.0},
    "WDO$":  {"mult": 10.0, "name": "Mini Dólar",       "margin": 140,  "slip_r": 5.0},
    "BIT$":  {"mult": 0.50, "name": "Bitcoin",          "margin": 45,   "slip_r": 10.0},
    "ISP$":  {"mult": 5.0,  "name": "Mini S&P 500",     "margin": 200,  "slip_r": 5.0},
    "GLD$":  {"mult": 1.0,  "name": "Ouro",             "margin": 135,  "slip_r": 5.0},
    "IND$":  {"mult": 1.00, "name": "Índice Bovespa",   "margin": 775,  "slip_r": 5.0},
    "DOL$":  {"mult": 50.0, "name": "Dólar Cheio",      "margin": 700,  "slip_r": 10.0},
    "WSP$":  {"mult": 2.5,  "name": "Micro S&P 500",    "margin": 100,  "slip_r": 2.5},
}

COMMISSION = 1.2
CLOSE_HOUR, CLOSE_MINUTE = 16, 45
START_HOUR, START_MINUTE = 9, 5
ATR_PERIOD = 14

# Strategies to test per symbol
STRATEGIES = ["VWAP", "EMA_PULLBACK", "MACD_MOMENTUM", "BOLLINGER"]


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


# ─── Strategy Functions ──────────────────────────────────────────────────────

def check_vwap(price, cur_atr_pct, ema_fast_val, ema_slow_val, vwap_val, rsi_val):
    if vwap_val == 0: return None
    if cur_atr_pct < 0.0015: buy_mult, sell_mult = 1.0005, 0.9995
    elif cur_atr_pct < 0.003: buy_mult, sell_mult = 1.0015, 0.9985
    else: buy_mult, sell_mult = 1.002, 0.998
    
    direction = None
    if price > vwap_val * buy_mult: direction = "BUY"
    elif price < vwap_val * sell_mult: direction = "SELL"
    if not direction: return None
    
    if ema_fast_val > 0 and ema_slow_val > 0:
        if direction == "BUY" and ema_fast_val < ema_slow_val: return None
        if direction == "SELL" and ema_fast_val > ema_slow_val: return None
    
    if not pd.isna(rsi_val):
        if direction == "BUY" and rsi_val > 85: return None
        if direction == "SELL" and rsi_val < 15: return None
    return direction


def check_ema_pullback(price, ema_fast_val, ema_slow_val, adx_val, plus_di, minus_di, rsi_val):
    if pd.isna(adx_val) or adx_val < 20: return None
    if pd.isna(ema_fast_val) or pd.isna(ema_slow_val) or ema_slow_val == 0: return None
    
    is_up = ema_fast_val > ema_slow_val
    is_down = ema_fast_val < ema_slow_val
    if not is_up and not is_down: return None
    
    if not pd.isna(plus_di) and not pd.isna(minus_di):
        if is_up and plus_di < minus_di: return None
        if is_down and minus_di < plus_di: return None
    
    direction = "BUY" if is_up else "SELL"
    if direction == "BUY" and price < ema_slow_val * 0.998: return None
    if direction == "SELL" and price > ema_slow_val * 1.002: return None
    
    if not pd.isna(rsi_val):
        if direction == "BUY" and rsi_val > 80: return None
        if direction == "SELL" and rsi_val < 20: return None
    return direction


def check_macd_momentum(price, ema_fast_val, ema_slow_val, adx_val, rsi_val, hist, prev_hist, prev2_hist):
    if pd.isna(adx_val) or adx_val < 15: return None
    if pd.isna(ema_fast_val) or pd.isna(ema_slow_val) or ema_slow_val == 0: return None
    
    is_up = ema_fast_val > ema_slow_val
    is_down = ema_fast_val < ema_slow_val
    if not is_up and not is_down: return None
    
    cross_up = prev_hist <= 0 and hist > 0
    cross_down = prev_hist >= 0 and hist < 0
    mom_up = hist > 0 and hist > prev_hist and prev_hist > prev2_hist
    mom_down = hist < 0 and hist < prev_hist and prev_hist < prev2_hist
    
    direction = None
    if is_up and (cross_up or mom_up):
        if not pd.isna(rsi_val) and rsi_val > 75: return None
        direction = "BUY"
    elif is_down and (cross_down or mom_down):
        if not pd.isna(rsi_val) and rsi_val < 25: return None
        direction = "SELL"
    
    if not direction: return None
    if direction == "BUY" and price < ema_slow_val * 0.995: return None
    if direction == "SELL" and price > ema_slow_val * 1.005: return None
    return direction


def check_bollinger(price, rsi_val, bb_upper, bb_lower):
    if bb_upper == 0 or bb_lower == 0 or pd.isna(bb_upper): return None
    
    direction = None
    if price <= bb_lower: direction = "BUY"
    elif price >= bb_upper: direction = "SELL"
    if not direction: return None
    
    if not pd.isna(rsi_val):
        if direction == "BUY" and rsi_val > 30: return None
        if direction == "SELL" and rsi_val < 70: return None
    return direction


# ─── Backtest Engine ─────────────────────────────────────────────────────────

def backtest(df, symbol, tf, strategy, *, capital=1_000_000.0):
    spec = CONTRACT_SPECS[symbol]
    mult = spec["mult"]
    slip_r = spec["slip_r"]
    
    atr = calc_atr(df, ATR_PERIOD)
    
    # Calculate all indicators
    _zero = pd.Series(0.0, index=df.index)
    vwap = calc_vwap(df, 20) if strategy == "VWAP" else _zero
    ema_fast = calc_ema(df["close"], 9) if strategy in ("VWAP", "EMA_PULLBACK", "MACD_MOMENTUM") else _zero
    ema_slow = calc_ema(df["close"], 21) if strategy in ("VWAP", "EMA_PULLBACK", "MACD_MOMENTUM") else _zero
    rsi = calc_rsi(df["close"], 14)
    adx_val, plus_di, minus_di = calc_adx(df, 14) if strategy in ("EMA_PULLBACK", "MACD_MOMENTUM") else (_zero, _zero, _zero)
    macd_line, signal_line, histogram = calc_macd(df) if strategy == "MACD_MOMENTUM" else (_zero, _zero, _zero)
    bb_upper, bb_mid, bb_lower = calc_bollinger(df) if strategy == "BOLLINGER" else (_zero, _zero, _zero)
    
    # Config
    sl_atr_mult = 1.0 if symbol in ("WIN$", "IND$") else 1.5
    trail_activate = 1.5
    trail_distance = 0.5
    cooldown = 300
    max_daily = 8
    
    # State
    cash = capital
    pos = 0
    ep = 0.0
    e_date = None
    e_atr = 0.0
    best_price = 0.0
    sl_price = 0.0
    trail_on = False
    sl_pts = 0
    bars_in_trade = 0
    trade_log = []
    daily_trades = {}
    last_trade_time = None
    
    def _close(price, reason, date):
        nonlocal cash, pos, ep, e_date, best_price, sl_price, trail_on, e_atr, sl_pts, bars_in_trade
        if pos == 0: return
        pnl = ((price - ep) * mult - slip_r - COMMISSION) if pos == 1 else ((ep - price) * mult - slip_r - COMMISSION)
        cash += pnl
        trade_log.append({"dir": "BUY" if pos == 1 else "SELL", "entry_time": e_date, "exit_time": date,
                          "ep": ep, "xp": price, "pnl": pnl, "reason": reason, "bars": bars_in_trade})
        pos = 0; ep = 0; best_price = 0; sl_price = 0; trail_on = False; bars_in_trade = 0
    
    def _open(direction, price, date, cur_atr):
        nonlocal cash, pos, ep, e_date, best_price, sl_price, trail_on, e_atr, sl_pts, last_trade_time
        if pos != 0: return False
        if last_trade_time is not None and (date - last_trade_time).total_seconds() < cooldown: return False
        d = date.date() if hasattr(date, 'date') else date
        if daily_trades.get(d, 0) >= max_daily: return False
        
        raw_sl = int(cur_atr * sl_atr_mult)
        raw_sl = max(raw_sl, 50)  # minimum SL
        raw_sl = ((raw_sl + 4) // 5) * 5
        if raw_sl <= 0: return False
        
        pos = 1 if direction == "BUY" else -1
        ep = price; e_date = date; e_atr = cur_atr; sl_pts = raw_sl; best_price = price; trail_on = False
        sl_price = price - raw_sl if pos == 1 else price + raw_sl
        daily_trades[d] = daily_trades.get(d, 0) + 1
        last_trade_time = date
        return True
    
    # Main loop
    for i, (date, row) in enumerate(df.iterrows()):
        price = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        hour = int(row["hour"])
        minute = int(row["minute"])
        
        cur_atr = float(atr.iloc[i]) if i > 0 and not pd.isna(atr.iloc[i]) else 0
        
        if hour < START_HOUR or (hour == START_HOUR and minute < START_MINUTE): continue
        
        if pos != 0:
            bars_in_trade += 1
            if pos == 1: best_price = max(best_price, high)
            else: best_price = min(best_price, low) if best_price > 0 else low
            
            profit_pts = (best_price - ep) if pos == 1 else (ep - best_price)
            tf_min = {"M5": 5, "M15": 15, "M30": 30, "H1": 60}.get(tf, 5)
            pos_minutes = bars_in_trade * tf_min
            
            if not trail_on and e_atr > 0 and profit_pts >= trail_activate * e_atr:
                trail_on = True
            
            if trail_on and e_atr > 0:
                trail_dist = trail_distance * e_atr
                if pos == 1:
                    new_sl = best_price - trail_dist
                    if new_sl > sl_price: sl_price = new_sl
                else:
                    new_sl = best_price + trail_dist
                    if new_sl < sl_price: sl_price = new_sl
            
            if sl_price > 0:
                if pos == 1 and low <= sl_price: _close(sl_price, "SL", date); continue
                elif pos == -1 and high >= sl_price: _close(sl_price, "SL", date); continue
            
            if hour > CLOSE_HOUR or (hour == CLOSE_HOUR and minute >= CLOSE_MINUTE):
                _close(price, "1645", date); continue
            continue
        
        if cur_atr <= 0: continue
        direction = None
        
        if strategy == "VWAP":
            cur_vwap = float(vwap.iloc[i]) if not pd.isna(vwap.iloc[i]) else 0
            cur_ef = float(ema_fast.iloc[i]) if not pd.isna(ema_fast.iloc[i]) else 0
            cur_es = float(ema_slow.iloc[i]) if not pd.isna(ema_slow.iloc[i]) else 0
            cur_rsi = float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else 50
            cur_atr_pct = cur_atr / price if price > 0 else 0
            direction = check_vwap(price, cur_atr_pct, cur_ef, cur_es, cur_vwap, cur_rsi)
        
        elif strategy == "EMA_PULLBACK":
            cur_ef = float(ema_fast.iloc[i]) if not pd.isna(ema_fast.iloc[i]) else 0
            cur_es = float(ema_slow.iloc[i]) if not pd.isna(ema_slow.iloc[i]) else 0
            cur_adx = float(adx_val.iloc[i]) if not pd.isna(adx_val.iloc[i]) else 0
            cur_pd = float(plus_di.iloc[i]) if not pd.isna(plus_di.iloc[i]) else 0
            cur_md = float(minus_di.iloc[i]) if not pd.isna(minus_di.iloc[i]) else 0
            cur_rsi = float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else 50
            direction = check_ema_pullback(price, cur_ef, cur_es, cur_adx, cur_pd, cur_md, cur_rsi)
        
        elif strategy == "MACD_MOMENTUM":
            cur_ef = float(ema_fast.iloc[i]) if not pd.isna(ema_fast.iloc[i]) else 0
            cur_es = float(ema_slow.iloc[i]) if not pd.isna(ema_slow.iloc[i]) else 0
            cur_adx = float(adx_val.iloc[i]) if not pd.isna(adx_val.iloc[i]) else 0
            cur_rsi = float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else 50
            cur_h = float(histogram.iloc[i]) if not pd.isna(histogram.iloc[i]) else 0
            prev_h = float(histogram.iloc[i-1]) if i > 0 and not pd.isna(histogram.iloc[i-1]) else 0
            prev2_h = float(histogram.iloc[i-2]) if i > 1 and not pd.isna(histogram.iloc[i-2]) else 0
            direction = check_macd_momentum(price, cur_ef, cur_es, cur_adx, cur_rsi, cur_h, prev_h, prev2_h)
        
        elif strategy == "BOLLINGER":
            cur_bu = float(bb_upper.iloc[i]) if not pd.isna(bb_upper.iloc[i]) else 0
            cur_bl = float(bb_lower.iloc[i]) if not pd.isna(bb_lower.iloc[i]) else 0
            cur_rsi = float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else 50
            direction = check_bollinger(price, cur_rsi, cur_bu, cur_bl)
        
        if direction: _open(direction, price, date, cur_atr)
    
    if pos != 0: _close(float(df["close"].iloc[-1]), "FORCE", df.index[-1])
    return trade_log


def run():
    print("\n" + "═" * 90)
    print("  🧪 BACKTEST MULTI-INDEX — Teste completo nos principais futuros B3")
    print("  " + "─" * 86)
    print("  Timeframe: M15 (500 barras ≈ 14 dias)")
    print("  Estratégias: VWAP, EMA_PULLBACK, MACD_MOMENTUM, BOLLINGER")
    print("═" * 90)
    
    tf = "M15"
    all_results = []
    
    for symbol, spec in CONTRACT_SPECS.items():
        print(f"\n{'─' * 90}")
        print(f"📡 {symbol} — {spec['name']} (margin R$ {spec['margin']}, mult R$ {spec['mult']})")
        print(f"{'─' * 90}")
        
        df = fetch(symbol, tf, 500)
        if df.empty:
            print("  ❌ Sem dados")
            continue
        
        n_days = df["date"].nunique()
        p0, p1 = float(df["close"].iloc[0]), float(df["close"].iloc[-1])
        print(f"  📊 {len(df)} barras, {n_days} dias | {df.index[0].strftime('%d/%m')} → {df.index[-1].strftime('%d/%m')} | {p0:.2f} → {p1:.2f} ({(p1/p0-1)*100:+.2f}%)")
        
        best_pnl = -999999
        best_strat = ""
        
        for strategy in STRATEGIES:
            trades = backtest(df, symbol, tf, strategy)
            
            if trades:
                n = len(trades)
                wins = sum(1 for t in trades if t["pnl"] > 0)
                pnl = sum(t["pnl"] for t in trades)
                wr = wins / n * 100
                gross_win = sum(t["pnl"] for t in trades if t["pnl"] > 0)
                gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0))
                pf = gross_win / gross_loss if gross_loss > 0 else 999
                
                icon = "🟢" if pnl > 0 else "🔴"
                print(f"  {icon} {strategy:<16} │ {n:>3}t │ WR {wr:>5.1f}% │ PF {pf:>5.2f} │ R$ {pnl:>+10.1f}")
                
                if pnl > best_pnl:
                    best_pnl = pnl
                    best_strat = strategy
                
                all_results.append({
                    "symbol": symbol, "name": spec["name"], "strategy": strategy,
                    "trades": n, "wr": wr, "pf": pf, "pnl": pnl,
                    "margin": spec["margin"], "mult": spec["mult"],
                })
            else:
                print(f"  ⚪ {strategy:<16} │   0t │ sem trades")
        
        if best_strat:
            print(f"  🏆 MELHOR: {best_strat} → R$ {best_pnl:+.1f}")
    
    # ─── GLOBAL SUMMARY ───
    print("\n\n" + "═" * 90)
    print("  📋 RANKING GLOBAL — Todos os ativos × estratégias")
    print("═" * 90)
    
    # Sort by PnL descending
    ranked = sorted(all_results, key=lambda x: x["pnl"], reverse=True)
    
    print(f"\n  {'#':<3} {'Ativo':<6} {'Nome':<16} │ {'Strategy':<16} │ {'T':>4} │ {'WR':>6} │ {'PF':>6} │ {'PnL':>10} │ {'Margin':>7}")
    print("  " + "─" * 88)
    
    for i, r in enumerate(ranked[:20], 1):
        icon = "🟢" if r["pnl"] > 0 else "🔴"
        print(f"  {icon}{i:<2} {r['symbol']:<6} {r['name']:<16} │ {r['strategy']:<16} │ {r['trades']:>4} │ {r['wr']:>5.1f}% │ {r['pf']:>5.2f} │ R$ {r['pnl']:>+8.1f} │ R$ {r['margin']:>5}")
    
    # Per-asset best
    print(f"\n\n  🏆 MELHOR ESTRATÉGIA POR ATIVO:")
    print("  " + "─" * 60)
    
    seen = set()
    for r in ranked:
        if r["symbol"] not in seen and r["pnl"] > 0:
            seen.add(r["symbol"])
            print(f"  {r['symbol']:<6} {r['name']:<16} → {r['strategy']:<16} R$ {r['pnl']:+.1f} (WR {r['wr']:.1f}%, PF {r['pf']:.2f})")
    
    # Recommendation
    profitable = [r for r in ranked if r["pnl"] > 0]
    print(f"\n\n  💡 RECOMENDAÇÃO PARA DAY TRADE:")
    print("  " + "─" * 60)
    if profitable:
        # Group by symbol, pick best strategy per symbol
        best_per_symbol = {}
        for r in ranked:
            if r["symbol"] not in best_per_symbol and r["pnl"] > 0:
                best_per_symbol[r["symbol"]] = r
        
        total_pnl = sum(r["pnl"] for r in best_per_symbol.values())
        total_margin = sum(r["margin"] for r in best_per_symbol.values())
        
        for sym, r in best_per_symbol.items():
            print(f"  ✅ {sym} ({r['name']}) — {r['strategy']} → R$ {r['pnl']:+.1f}/dia")
        
        print(f"\n  💰 PnL total combinado: R$ {total_pnl:+.1f}")
        print(f"  💰 Margin total: R$ {total_margin}")
        print(f"  💰 ROI: {(total_pnl / total_margin * 100) if total_margin > 0 else 0:.1f}%")
    else:
        print("  ⚠️ Nenhum ativo lucrativo no período testado")
    
    print("\n" + "═" * 90 + "\n")


if __name__ == "__main__":
    run()
