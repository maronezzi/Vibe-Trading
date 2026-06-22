"""
backtest_agi_v11.py — Backtest com config AGI 17h v11.

Estratégias:
  WDO → VWAP (period 20, thresholds 1.002/0.998, RSI 85/15)
  WIN → STRONG_TREND (EMA 9/21, ADX>30, DI confirm)

Lê dados CSV do data/ e replica a lógica dos plugins.
"""

import sys, csv, io, subprocess, os, json
from pathlib import Path
from datetime import datetime, time
import numpy as np
import pandas as pd

# ─── MT5 fetch ───────────────────────────────────────────────────────────────
WINE_PYTHON = os.path.expanduser("~/.wine/drive_c/Python311/python.exe")
FETCH_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mt5_fetch.py")

CONTRACT_SPECS = {
    "WIN$": {"mult": 0.20, "name": "Mini Índice", "margin": 5000, "tick": 5, "slip_r": 1.0},
    "WDO$": {"mult": 10.0, "name": "Mini Dólar", "margin": 3000, "tick": 0.5, "slip_r": 5.0},
}
COMMISSION = 1.2  # emolumentos + corretagem por contrato

CLOSE_HOUR, CLOSE_MINUTE = 16, 45
START_HOUR, START_MINUTE = 9, 5
ATR_PERIOD = 14


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
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_adx(df, period=14):
    high = df["high"]
    low = df["low"]
    close = df["close"]
    
    plus_dm = high.diff()
    minus_dm = low.diff().mul(-1)
    
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
    
    tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
    
    atr = tr.ewm(alpha=1/period, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1/period, min_periods=period).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1/period, min_periods=period).mean() / atr
    
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = dx.ewm(alpha=1/period, min_periods=period).mean()
    
    return adx, plus_di, minus_di


# ─── Strategy: VWAP (WDO) ────────────────────────────────────────────────────

def check_vwap(price, atr_val, cur_atr_pct, ema_fast_val, ema_slow_val, vwap_val, rsi_val, params):
    """Replica strategies/vwap.py"""
    vwap_period = params.get("vwap_period", 20)
    rsi_ob = params.get("rsi_overbought", 85)
    rsi_os = params.get("rsi_oversold", 15)
    
    if vwap_val == 0:
        return None
    
    # Adaptive thresholds (igual ao plugin)
    if cur_atr_pct < 0.0015:
        buy_mult = 1.0005
        sell_mult = 0.9995
    elif cur_atr_pct < 0.003:
        buy_mult = 1.0015
        sell_mult = 0.9985
    else:
        buy_mult = params.get("vwap_buy_threshold", 1.002)
        sell_mult = params.get("vwap_sell_threshold", 0.998)
    
    buy_thresh = vwap_val * buy_mult
    sell_thresh = vwap_val * sell_mult
    
    direction = None
    if price > buy_thresh:
        direction = "BUY"
    elif price < sell_thresh:
        direction = "SELL"
    
    if not direction:
        return None
    
    # EMA trend filter
    if ema_fast_val > 0 and ema_slow_val > 0:
        if direction == "BUY" and ema_fast_val < ema_slow_val:
            return None
        if direction == "SELL" and ema_fast_val > ema_slow_val:
            return None
    
    # RSI filter
    if not pd.isna(rsi_val):
        if direction == "BUY" and rsi_val > rsi_ob:
            return None
        if direction == "SELL" and rsi_val < rsi_os:
            return None
    
    return direction


# ─── Strategy: STRONG_TREND (WIN) ────────────────────────────────────────────

def check_strong_trend(price, atr_val, ema_fast_val, ema_slow_val, adx_val, plus_di, minus_di, rsi_val, params):
    """Replica strategies/strong_trend.py"""
    adx_threshold = params.get("adx_threshold", 30)
    
    if pd.isna(adx_val) or adx_val == 0:
        return None
    if pd.isna(ema_fast_val) or pd.isna(ema_slow_val) or ema_slow_val == 0:
        return None
    
    # Need minimum ADX
    if adx_val < adx_threshold:
        return None
    
    # Direction from EMA
    if ema_fast_val > ema_slow_val:
        direction = "BUY"
    elif ema_fast_val < ema_slow_val:
        direction = "SELL"
    else:
        return None
    
    # DI confirmation
    if not pd.isna(plus_di) and not pd.isna(minus_di):
        if direction == "BUY" and plus_di < minus_di:
            return None
        if direction == "SELL" and minus_di < plus_di:
            return None
    
    # Price position check
    if direction == "BUY" and price < ema_slow_val * 0.998:
        return None
    if direction == "SELL" and price > ema_slow_val * 1.002:
        return None
    
    # RSI filter — only for moderate trends
    if adx_val < 40:
        if not pd.isna(rsi_val):
            if direction == "BUY" and rsi_val > 80:
                return None
            if direction == "SELL" and rsi_val < 20:
                return None
    
    return direction


# ─── Backtest Engine ─────────────────────────────────────────────────────────

def backtest(df, symbol, tf, strategy, params, *, capital=1_000_000.0):
    spec = CONTRACT_SPECS[symbol]
    mult = spec["mult"]
    margin = spec["margin"]
    slip_r = spec["slip_r"]
    is_win = "WIN" in symbol
    is_wdo = "WDO" in symbol
    
    # Calculate indicators
    atr = calc_atr(df, ATR_PERIOD)
    
    if strategy == "VWAP":
        vwap = calc_vwap(df, params.get("vwap_period", 20))
        ema_fast = calc_ema(df["close"], params.get("ema_fast", 9))
        ema_slow = calc_ema(df["close"], params.get("ema_slow", 21))
        rsi = calc_rsi(df["close"], params.get("rsi_period", 14))
    elif strategy == "STRONG_TREND":
        ema_fast = calc_ema(df["close"], params.get("ema_fast", 9))
        ema_slow = calc_ema(df["close"], params.get("ema_slow", 21))
        rsi = calc_rsi(df["close"], params.get("rsi_period", 14))
        adx_val, plus_di, minus_di = calc_adx(df, params.get("adx_period", 14))
        vwap = pd.Series(0, index=df.index)
    
    # Config
    sl_atr_mult = params.get("sl_atr_mult", 1.0)
    trail_activate = params.get("trail_activate", 1.5)
    trail_distance = params.get("trail_distance", 0.5)
    cooldown = params.get("cooldown_seconds", 300)
    max_daily = params.get("max_daily_trades", 8)
    breakeven_min = params.get("breakeven_minutes", 0)  # 0 = disabled by default
    time_trail_min = params.get("time_trail_minutes", 0)  # 0 = disabled
    max_pos_min = params.get("max_position_minutes", 999)  # 999 = disabled
    
    # State
    cash = capital
    pos = 0  # 0=flat, 1=long, -1=short
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
        
        if pos == 0:
            return
        
        sl_cost = slip_r
        comm = COMMISSION
        
        if pos == 1:
            pnl = (price - ep) * mult - sl_cost - comm
        else:
            pnl = (ep - price) * mult - sl_cost - comm
        
        cash += pnl
        trade_log.append({
            "dir": "BUY" if pos == 1 else "SELL",
            "entry_time": e_date,
            "exit_time": date,
            "ep": ep,
            "xp": price,
            "pnl": pnl,
            "reason": reason,
            "sl_pts": sl_pts,
            "strategy": strategy,
            "bars": bars_in_trade,
        })
        
        pos = 0
        ep = 0
        best_price = 0
        sl_price = 0
        trail_on = False
        bars_in_trade = 0
    
    def _open(direction, price, date, cur_atr):
        nonlocal cash, pos, ep, e_date, best_price, sl_price, trail_on, e_atr, sl_pts, last_trade_time
        
        if pos != 0:
            return False
        
        # Cooldown check
        if last_trade_time is not None:
            elapsed = (date - last_trade_time).total_seconds()
            if elapsed < cooldown:
                return False
        
        # Daily limit
        d = date.date() if hasattr(date, 'date') else date
        if daily_trades.get(d, 0) >= max_daily:
            return False
        
        # SL calculation
        raw_sl = int(cur_atr * sl_atr_mult)
        if is_win:
            raw_sl = max(raw_sl, 100)
        elif is_wdo:
            raw_sl = max(raw_sl, 200)
        raw_sl = ((raw_sl + 4) // 5) * 5  # múltiplo de 5
        
        if raw_sl <= 0:
            return False
        
        pos = 1 if direction == "BUY" else -1
        ep = price
        e_date = date
        e_atr = cur_atr
        sl_pts = raw_sl
        best_price = price
        trail_on = False
        
        if pos == 1:
            sl_price = price - raw_sl
        else:
            sl_price = price + raw_sl
        
        daily_trades[d] = daily_trades.get(d, 0) + 1
        last_trade_time = date
        return True
    
    # ─── Main loop ───
    for i, (date, row) in enumerate(df.iterrows()):
        price = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        hour = int(row["hour"])
        minute = int(row["minute"])
        
        cur_atr = float(atr.iloc[i]) if i > 0 and not pd.isna(atr.iloc[i]) else 0
        
        # Skip pre-market
        if hour < START_HOUR or (hour == START_HOUR and minute < START_MINUTE):
            continue
        
        # ─── Position management ───
        if pos != 0:
            bars_in_trade += 1
            
            # Update best
            if pos == 1:
                best_price = max(best_price, high)
            else:
                best_price = min(best_price, low) if best_price > 0 else low
            
            # Profit in pts
            if pos == 1:
                profit_pts = best_price - ep
            else:
                profit_pts = ep - best_price
            
            # Position time in minutes (M5=5min/bar, M15=15min/bar)
            tf_minutes = 5 if tf == "M5" else 15
            pos_minutes = bars_in_trade * tf_minutes
            
            # ===== TRAILING POR LUCRO (original) =====
            if not trail_on and e_atr > 0 and profit_pts >= trail_activate * e_atr:
                trail_on = True
            
            # ===== PROTEÇÃO 1: BREAKEVEN =====
            if not trail_on and breakeven_min > 0 and pos_minutes >= breakeven_min and e_atr > 0:
                cost_pts = int(5 / (0.001 if is_wdo else 1.0))
                if pos == 1:
                    be_price = ep + cost_pts * (0.001 if is_wdo else 1.0)
                    be_dist = ep - be_price
                    if be_dist < 0:
                        new_sl_pts = max(1, int(abs(be_dist) / (0.001 if is_wdo else 1.0)))
                        if new_sl_pts < sl_pts:
                            sl_pts = new_sl_pts
                            sl_price = ep + sl_pts * (0.001 if is_wdo else 1.0) if pos == -1 else ep - sl_pts * (0.001 if is_wdo else 1.0)
                else:
                    be_price = ep - cost_pts * (0.001 if is_wdo else 1.0)
                    be_dist = be_price - ep
                    if be_dist < 0:
                        new_sl_pts = max(1, int(abs(be_dist) / (0.001 if is_wdo else 1.0)))
                        if new_sl_pts < sl_pts:
                            sl_pts = new_sl_pts
                            sl_price = ep + sl_pts * (0.001 if is_wdo else 1.0) if pos == -1 else ep - sl_pts * (0.001 if is_wdo else 1.0)
            
            # ===== PROTEÇÃO 2: TIME-BASED TRAILING =====
            if not trail_on and time_trail_min > 0 and pos_minutes >= time_trail_min and profit_pts > 0:
                trail_on = True
            
            # ===== TRAILING STOP =====
            if trail_on and e_atr > 0:
                # Proteção 3: após max_position_minutes, trailing mais apertado
                if pos_minutes >= max_pos_min:
                    trail_dist = 0.3 * e_atr  # agressivo
                else:
                    trail_dist = trail_distance * e_atr
                
                if pos == 1:
                    new_sl = best_price - trail_dist
                    if new_sl > sl_price:
                        sl_price = new_sl
                else:
                    new_sl = best_price + trail_dist
                    if new_sl < sl_price:
                        sl_price = new_sl
            
            # SL check
            if sl_price > 0:
                if pos == 1 and low <= sl_price:
                    _close(sl_price, "SL", date)
                    continue
                elif pos == -1 and high >= sl_price:
                    _close(sl_price, "SL", date)
                    continue
            
            # 16:45 close
            if hour > CLOSE_HOUR or (hour == CLOSE_HOUR and minute >= CLOSE_MINUTE):
                _close(price, "1645", date)
                continue
            
            continue  # Already in position
        
        # ─── Entry check ───
        if cur_atr <= 0:
            continue
        
        direction = None
        
        if strategy == "VWAP":
            cur_vwap = float(vwap.iloc[i]) if not pd.isna(vwap.iloc[i]) else 0
            cur_ema_fast = float(ema_fast.iloc[i]) if not pd.isna(ema_fast.iloc[i]) else 0
            cur_ema_slow = float(ema_slow.iloc[i]) if not pd.isna(ema_slow.iloc[i]) else 0
            cur_rsi = float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else 50
            cur_atr_pct = cur_atr / price if price > 0 else 0
            
            direction = check_vwap(price, cur_atr, cur_atr_pct, cur_ema_fast, cur_ema_slow, cur_vwap, cur_rsi, params)
        
        elif strategy == "STRONG_TREND":
            cur_ema_fast = float(ema_fast.iloc[i]) if not pd.isna(ema_fast.iloc[i]) else 0
            cur_ema_slow = float(ema_slow.iloc[i]) if not pd.isna(ema_slow.iloc[i]) else 0
            cur_adx = float(adx_val.iloc[i]) if not pd.isna(adx_val.iloc[i]) else 0
            cur_plus_di = float(plus_di.iloc[i]) if not pd.isna(plus_di.iloc[i]) else 0
            cur_minus_di = float(minus_di.iloc[i]) if not pd.isna(minus_di.iloc[i]) else 0
            cur_rsi = float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else 50
            
            direction = check_strong_trend(price, cur_atr, cur_ema_fast, cur_ema_slow, cur_adx, cur_plus_di, cur_minus_di, cur_rsi, params)
        
        if direction:
            _open(direction, price, date, cur_atr)
    
    # Force close
    if pos != 0:
        _close(float(df["close"].iloc[-1]), "FORCE", df.index[-1])
    
    return trade_log


def run():
    # Load config
    config_path = Path(__file__).parent.parent / "vt_config.json"
    with open(config_path) as f:
        config = json.load(f)
    
    print("\n" + "═" * 80)
    print("  🧪 BACKTEST AGI v11 — Config do dia 11/06/2026")
    print("  " + "─" * 76)
    print(f"  WDO → {config['strategy']['WDO']} (VWAP period {config['wdo']['vwap_period']})")
    print(f"  WIN → {config['strategy']['WIN']} (ADX>{config['win']['adx_threshold']})")
    print("═" * 80)
    
    # Fetch data — only today
    combos = [
        ("WDO$", "M5", "VWAP", config["wdo"]),
        ("WDO$", "M15", "VWAP", config["wdo"]),
        ("WIN$", "M5", "STRONG_TREND", config["win"]),
        ("WIN$", "M15", "STRONG_TREND", config["win"]),
    ]
    
    # Also fetch with old config for comparison
    old_wdo = {
        "vwap_period": 10,
        "vwap_buy_threshold": 1.002,
        "vwap_sell_threshold": 0.995,
        "sl_atr_mult": 1.0,
        "trail_activate": 2.0,
        "trail_distance": 0.2,
        "cooldown_seconds": 300,
        "max_daily_trades": 8,
        "ema_fast": 9,
        "ema_slow": 21,
        "rsi_period": 14,
        "rsi_overbought": 70,
        "rsi_oversold": 30,
    }
    
    old_win = {
        "bb_period": 20,
        "bb_std": 2.0,
        "rsi_period": 14,
        "rsi_overbought": 70,
        "rsi_oversold": 30,
        "sl_atr_mult": 2.0,
        "trail_activate": 0.5,
        "trail_distance": 0.1,
        "cooldown_seconds": 600,
        "max_daily_trades": 10,
    }
    
    all_results = []
    
    for sym, tf, strategy, params in combos:
        spec = CONTRACT_SPECS[sym]
        print(f"\n📡 {sym} ({spec['name']}) {tf} — {strategy}")
        
        # Fetch today + warmup (need ~100 bars for indicators)
        df = fetch(sym, tf, 500)
        if df.empty:
            print("  ❌ Sem dados")
            continue
        
        # Filter today only for display, but keep warmup
        today = pd.Timestamp("2026-06-11").date()
        n_days = df["date"].nunique()
        p0, p1 = float(df["close"].iloc[0]), float(df["close"].iloc[-1])
        
        print(f"  ✅ {len(df)} barras, {n_days} dias | {df.index[0].strftime('%d/%m')} → {df.index[-1].strftime('%d/%m')}")
        print(f"     {p0:.2f} → {p1:.2f} ({(p1/p0-1)*100:+.2f}%)")
        
        # Run with NEW config (AGI v11)
        trades_new = backtest(df, sym, tf, strategy, params)
        
        # Run with OLD config for comparison
        if "WDO" in sym:
            old_strat = "VWAP"
            old_params = old_wdo
        else:
            old_strat = "BOLLINGER"
            old_params = old_win
        
        trades_old = backtest(df, sym, tf, old_strat, old_params)
        
        # Filter trades to today only
        trades_new_today = [t for t in trades_new if hasattr(t["entry_time"], 'date') and t["entry_time"].date() == today]
        trades_old_today = [t for t in trades_old if hasattr(t["entry_time"], 'date') and t["entry_time"].date() == today]
        
        # Also show all-period stats
        def summarize(trades, label):
            if not trades:
                return {"label": label, "n": 0, "pnl": 0, "wr": 0, "wins": 0, "losses": 0}
            n = len(trades)
            wins = sum(1 for t in trades if t["pnl"] > 0)
            pnl = sum(t["pnl"] for t in trades)
            wr = wins / n * 100 if n else 0
            return {"label": label, "n": n, "pnl": pnl, "wr": wr, "wins": wins, "losses": n - wins,
                    "avg_win": np.mean([t["pnl"] for t in trades if t["pnl"] > 0]) if wins else 0,
                    "avg_loss": np.mean([t["pnl"] for t in trades if t["pnl"] <= 0]) if n > wins else 0}
        
        # All-period
        s_new = summarize(trades_new, f"NEW ({strategy})")
        s_old = summarize(trades_old, f"OLD ({old_strat})")
        
        # Today only
        s_new_today = summarize(trades_new_today, f"NEW today")
        s_old_today = summarize(trades_old_today, f"OLD today")
        
        print(f"\n  📊 RESULTADO PERÍODO COMPLETO")
        print(f"  {'─' * 60}")
        for s in [s_new, s_old]:
            icon = "🟢" if s["pnl"] > 0 else "🔴"
            print(f"  {icon} {s['label']:<20} | {s['n']:>3}t | WR {s['wr']:>5.1f}% | PnL R$ {s['pnl']:>+8.1f}")
        
        print(f"\n  📊 RESULTADO SÓ HOJE (11/06)")
        print(f"  {'─' * 60}")
        for s in [s_new_today, s_old_today]:
            icon = "🟢" if s["pnl"] > 0 else "🔴"
            print(f"  {icon} {s['label']:<20} | {s['n']:>3}t | WR {s['wr']:>5.1f}% | PnL R$ {s['pnl']:>+8.1f}")
        
        # Trade-by-trade today (NEW)
        if trades_new_today:
            print(f"\n  📋 TRADES HOJE — {strategy}:")
            for j, t in enumerate(trades_new_today, 1):
                icon = "✅" if t["pnl"] > 0 else "❌"
                et = t["entry_time"].strftime("%H:%M") if hasattr(t["entry_time"], "strftime") else "?"
                xt = t["exit_time"].strftime("%H:%M") if hasattr(t["exit_time"], "strftime") else "?"
                print(f"    {icon} {t['dir']:<4} @ {t['ep']:.1f} → {t['xp']:.1f} | {et}→{xt} | R$ {t['pnl']:+.1f} | {t['reason']}")
        
        all_results.append({
            "sym": sym, "tf": tf, "strategy": strategy,
            "new": s_new, "old": s_old,
            "new_today": s_new_today, "old_today": s_old_today,
            "trades_new_today": trades_new_today,
            "trades_old_today": trades_old_today,
        })
    
    # ─── SUMMARY ───
    print("\n\n" + "═" * 80)
    print("  📋 RESUMO COMPARATIVO — AGI v11 vs OLD (Bollinger/VWAP-10)")
    print("═" * 80)
    
    print(f"\n  {'Ativo':<8} {'TF':<4} │ {'NEW PnL':>10} {'NEW T':>5} {'NEW WR':>7} │ {'OLD PnL':>10} {'OLD T':>5} {'OLD WR':>7} │ {'Delta':>10}")
    print("  " + "─" * 80)
    
    total_new = 0
    total_old = 0
    total_new_today = 0
    total_old_today = 0
    
    for r in all_results:
        n = r["new_today"]
        o = r["old_today"]
        delta = n["pnl"] - o["pnl"]
        total_new_today += n["pnl"]
        total_old_today += o["pnl"]
        
        nicon = "🟢" if n["pnl"] > 0 else "🔴"
        oicon = "🟢" if o["pnl"] > 0 else "🔴"
        dicon = "📈" if delta > 0 else "📉" if delta < 0 else "➡️"
        
        print(f"  {r['sym']:<8} {r['tf']:<4} │ {nicon} {n['pnl']:>+8.1f} {n['n']:>4}t {n['wr']:>5.1f}% │ {oicon} {o['pnl']:>+8.1f} {o['n']:>4}t {o['wr']:>5.1f}% │ {dicon} {delta:>+8.1f}")
    
    delta_total = total_new_today - total_old_today
    print("  " + "─" * 80)
    ni = "🟢" if total_new_today > 0 else "🔴"
    oi = "🟢" if total_old_today > 0 else "🔴"
    di = "📈" if delta_total > 0 else "📉" if delta_total < 0 else "➡️"
    print(f"  {'TOTAL':<8} {'':4} │ {ni} {total_new_today:>+8.1f} {'':>4}  {'':>6} │ {oi} {total_old_today:>+8.1f} {'':>4}  {'':>6} │ {di} {delta_total:>+8.1f}")
    
    print(f"\n  💡 AGI v11 hoje: R$ {total_new_today:+.1f}")
    print(f"  💡 Config antiga hoje: R$ {total_old_today:+.1f}")
    print(f"  💡 Diferença: R$ {delta_total:+.1f}")
    
    if delta_total > 0:
        print(f"\n  ✅ AGI v11 GANHOU da config antiga no dia 11/06!")
    else:
        print(f"\n  ⚠️ Config antiga foi melhor no dia 11/06")
    
    print("\n" + "═" * 80 + "\n")


if __name__ == "__main__":
    run()
