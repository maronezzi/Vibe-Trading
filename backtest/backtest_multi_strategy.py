"""
backtest_multi_strategy.py — Testa todas as estratégias disponíveis em todos os combos.
Usa o mesmo motor do backtest_v6 mas com a interface de plugins (check_entry).
"""
import sys, csv, io, subprocess, os
from pathlib import Path
import numpy as np, pandas as pd

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

WINE_PYTHON = os.path.expanduser("~/.wine/drive_c/Python311/python.exe")
FETCH_SCRIPT = os.path.join(os.path.dirname(__file__), "mt5_fetch.py")

CLOSE_HOUR, CLOSE_MINUTE = 16, 45

CONTRACT_SPECS = {
    "WIN$": {"mult": 0.20, "name": "Mini Índice", "margin": 5000, "tick": 5, "slip_r": 1.0},
    "WDO$": {"mult": 10.0, "name": "Mini Dólar", "margin": 3000, "tick": 0.5, "slip_r": 5.0},
}
COMMISSION = 2.5

# Default params per symbol
DEFAULT_PARAMS = {
    "win": {
        "ema_fast": 9, "ema_slow": 21, "adx_period": 14, "adx_threshold": 30,
        "rsi_period": 14, "rsi_overbought": 80, "rsi_oversold": 20,
        "sl_atr_mult": 1.5, "trail_activate": 1.5, "trail_distance": 0.5,
        "cooldown_seconds": 600, "max_daily_trades": 10, "trend_min_spread": 0.001,
        "pullback_pct": 0.15,
    },
    "wdo": {
        "vwap_period": 20, "vwap_buy_threshold": 1.002, "vwap_sell_threshold": 0.998,
        "ema_fast": 9, "ema_slow": 21, "adx_period": 14, "adx_threshold": 25,
        "rsi_period": 14, "rsi_overbought": 85, "rsi_oversold": 15,
        "sl_atr_mult": 1.0, "trail_activate": 1.5, "trail_distance": 0.5,
        "pullback_pct": 0.15,
    },
}

STRATEGIES_DIR = Path(__file__).parent.parent / "strategies"


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


# ===== Indicator utils (mimicking autotrader) =====
def _ema(values, period):
    """Calculate EMA from a list of floats."""
    if len(values) < period:
        return 0
    arr = np.array(values, dtype=float)
    alpha = 2.0 / (period + 1)
    ema = arr[0]
    for v in arr[1:]:
        ema = alpha * v + (1 - alpha) * ema
    return ema


def _rsi(bars, period=14):
    """Calculate RSI from bars (list of dicts with 'close')."""
    if len(bars) < period + 2:
        return 50.0
    closes = [float(b["close"]) for b in reversed(bars)]
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _adx(bars, period=14):
    """Calculate ADX, +DI, -DI from bars."""
    if len(bars) < period * 2:
        return 0, 0, 0
    highs = np.array([float(b["high"]) for b in reversed(bars)])
    lows = np.array([float(b["low"]) for b in reversed(bars)])
    closes = np.array([float(b["close"]) for b in reversed(bars)])
    
    n = len(highs)
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    tr = np.zeros(n)
    
    for i in range(1, n):
        up = highs[i] - highs[i-1]
        down = lows[i-1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else 0
        minus_dm[i] = down if (down > up and down > 0) else 0
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
    
    # Smoothed averages
    atr = np.mean(tr[1:period+1]) if n > period else np.mean(tr[1:])
    plus_di_s = np.mean(plus_dm[1:period+1]) if n > period else np.mean(plus_dm[1:])
    minus_di_s = np.mean(minus_dm[1:period+1]) if n > period else np.mean(minus_dm[1:])
    
    dx_vals = []
    for i in range(period, n):
        atr = atr - atr / period + tr[i]
        plus_di_s = plus_di_s - plus_di_s / period + plus_dm[i]
        minus_di_s = minus_di_s - minus_di_s / period + minus_dm[i]
        
        if atr > 0:
            pdi = 100 * plus_di_s / atr
            mdi = 100 * minus_di_s / atr
        else:
            pdi = mdi = 0
        
        denom = pdi + mdi
        dx = 100 * abs(pdi - mdi) / denom if denom > 0 else 0
        dx_vals.append((dx, pdi, mdi))
    
    if not dx_vals:
        return 0, 0, 0
    
    adx = np.mean([d[0] for d in dx_vals[-period:]]) if len(dx_vals) >= period else np.mean([d[0] for d in dx_vals])
    last_pdi = dx_vals[-1][1]
    last_mdi = dx_vals[-1][2]
    
    return adx, last_pdi, last_mdi


def _vwap(bars, period=20):
    """Calculate VWAP from bars."""
    if len(bars) < period:
        return 0
    h = np.array([float(b["high"]) for b in reversed(bars[:period])])
    l = np.array([float(b["low"]) for b in reversed(bars[:period])])
    c = np.array([float(b["close"]) for b in reversed(bars[:period])])
    v = np.array([float(b.get("volume", 1) or 1) for b in reversed(bars[:period])])
    typical = (h + l + c) / 3
    return float(np.sum(typical * v) / np.sum(v))


def _bollinger(bars, period=20, std_mult=2.0):
    """Calculate Bollinger Bands."""
    if len(bars) < period:
        return 0, 0, 0
    closes = np.array([float(b["close"]) for b in reversed(bars[:period])])
    mid = float(np.mean(closes))
    std = float(np.std(closes))
    return mid + std_mult * std, mid, mid - std_mult * std


def _market_regime(bars, params):
    """Detect market regime."""
    if not bars or len(bars) < 30:
        return "UNKNOWN"
    ema_fast = _ema([float(b["close"]) for b in reversed(bars)], params.get("ema_fast", 9))
    ema_slow = _ema([float(b["close"]) for b in reversed(bars)], params.get("ema_slow", 21))
    adx_val, _, _ = _adx(bars, params.get("adx_period", 14))
    
    spread = abs(ema_fast - ema_slow) / ema_slow if ema_slow > 0 else 0
    min_spread = params.get("trend_min_spread", 0.001)
    
    if adx_val > 25 and spread > min_spread:
        return "TRENDING"
    elif spread < min_spread * 0.5:
        return "CHOPPY"
    else:
        return "UNKNOWN"


def _calc_sl(symbol, atr, params):
    """Calculate stop loss in points — matches autotrader _calc_sl logic."""
    is_win = "WIN" in symbol.upper()
    mult = params.get("sl_atr_mult", 1.5)
    raw = int(atr * mult)
    # Autotrader: both WIN and WDO have min 200 pts
    raw = max(raw, 200)
    return ((raw + 4) // 5) * 5


# Build utils dict
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


def load_strategies():
    """Load all strategy plugins."""
    import importlib.util
    strategies = {}
    for py_file in sorted(STRATEGIES_DIR.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        module_name = py_file.stem
        try:
            spec = importlib.util.spec_from_file_location(f"strategies.{module_name}", str(py_file))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            name = getattr(module, "STRATEGY_NAME", module_name.upper())
            check_func = getattr(module, "check_entry", None)
            if check_func:
                strategies[name] = check_func
                print(f"  ✅ Loaded: {name}")
        except Exception as e:
            print(f"  ❌ Failed to load {module_name}: {e}")
    return strategies


def backtest_strategy(df, symbol, strategy_func, params, utils, capital=100_000.0, tf="M5"):
    """Backtest using a strategy plugin."""
    spec = CONTRACT_SPECS[symbol]
    mult, margin, slip_r = spec["mult"], spec["margin"], spec["slip_r"]
    is_win = "WIN" in symbol
    
    atr = calc_atr(df, 14)
    atr_period = 14
    
    cash = capital
    pos = 0
    ep = 0.0
    e_date = None
    e_atr = 0.0
    best = 0.0
    sl_price = 0.0
    trail_on = False
    sl_pts_val = 0
    bars_in_trade = 0
    
    equity = []
    trade_log = []
    daily_pnl_dict = {}
    n_trades = n_wins = n_long = n_short = 0
    n_sl = n_trail = n_close = 0
    gross_win = 0.0
    gross_loss_val = 0.0
    
    # Prepare bars list for strategy (most recent first)
    bars_list = []
    for idx in range(len(df)):
        row = df.iloc[idx]
        bars_list.append({
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["tick_volume"]),
            "time": df.index[idx],
        })
    
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
            n_wins += 1
            gross_win += pnl
        else:
            gross_loss_val += abs(pnl)
        
        trade_log.append({
            "type": "LONG" if pos == 1 else "SHORT",
            "entry": str(e_date), "exit": "",
            "ep": ep, "xp": price, "pnl": pnl, "reason": reason,
            "bars": bars_in_trade, "sl_pts": sl_pts_val,
        })
        
        d = e_date.date() if hasattr(e_date, 'date') else e_date
        if d not in daily_pnl_dict:
            daily_pnl_dict[d] = 0.0
        daily_pnl_dict[d] += pnl
        
        pos = 0; ep = 0; best = 0; sl_price = 0; trail_on = False
        bars_in_trade = 0
    
    def _open(direction, price, date, cur_atr):
        nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, e_atr, sl_pts_val, bars_in_trade
        
        if pos != 0:
            return False
        
        raw_sl = _calc_sl(symbol, cur_atr, params)
        
        cost = slip_r + COMMISSION
        if cash >= margin + cost:
            cash -= margin + cost
            pos = 1 if direction == "BUY" else -1
            ep = price; e_date = date; e_atr = cur_atr; sl_pts_val = raw_sl
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
        cur_atr = float(atr.iloc[i]) if i > 0 and not pd.isna(atr.iloc[i]) else 0
        
        # Mark-to-market
        if pos == 1:
            eq_val = cash + (price - ep) * mult + margin
        elif pos == -1:
            eq_val = cash + (ep - price) * mult + margin
        else:
            eq_val = cash
        equity.append(eq_val)
        
        # No position: check entry via strategy
        if pos == 0:
            if cur_atr > 0 and i >= 30:
                # Build bars for strategy (most recent first) — need enough for ADX (2*period) + EMA
                strat_bars = list(reversed(bars_list[max(0, i-60):i+1]))
                result = strategy_func(symbol, tf, price, cur_atr, date, strat_bars, params, utils)
                if result and result.get("direction"):
                    _open(result["direction"], price, date, cur_atr)
            continue
        
        # Position open
        bars_in_trade += 1
        
        if pos == 1:
            best = max(best, high)
        elif pos == -1:
            best = min(best, low) if best > 0 else low
        
        if pos == 1:
            profit_pts = best - ep
        else:
            profit_pts = ep - best
        
        trail_activate = params.get("trail_activate", 1.5)
        trail_distance = params.get("trail_distance", 0.5)
        breakeven_min = params.get("breakeven_minutes", 15)
        time_trail_min = params.get("time_trail_minutes", 30)
        max_pos_min = params.get("max_position_minutes", 120)
        check_interval = 30  # seconds
        pos_minutes = bars_in_trade * check_interval / 60

        # ===== TRAILING POR LUCRO (original) =====
        if not trail_on and e_atr > 0 and profit_pts >= trail_activate * e_atr:
            trail_on = True

        # ===== BREAKEVEN: após X min sem trailing, move SL pra entry =====
        if not trail_on and pos_minutes >= breakeven_min and e_atr > 0:
            cost_pts = 5  # approximate cost
            if pos == 1:
                be_price = ep + cost_pts
                if be_price < sl_price:
                    sl_price = be_price
            else:
                be_price = ep - cost_pts
                if be_price > sl_price:
                    sl_price = be_price

        # ===== TIME-BASED TRAILING =====
        if not trail_on and pos_minutes >= time_trail_min and profit_pts > 0:
            trail_on = True

        # ===== TRAILING STOP =====
        if trail_on and e_atr > 0:
            if pos_minutes >= max_pos_min:
                trail_dist = 0.3 * e_atr  # agressivo
            else:
                trail_dist = trail_distance * e_atr
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
    
    # Stats
    total_ret = (cash - capital) / capital * 100
    n_days = df["date"].nunique()
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


def run():
    print("\n" + "═" * 100)
    print("  🔬 MULTI-STRATEGY BACKTEST — Testa todas as estratégias em todos os combos")
    print("═" * 100)
    
    # Load strategies
    print("\n📦 Carregando estratégias...")
    strategies = load_strategies()
    print(f"  Total: {len(strategies)} estratégias")
    
    # Combos to test
    combos = [
        ("WIN$", "M5", 500),
        ("WIN$", "M15", 500),
        ("WDO$", "M5", 500),
        ("WDO$", "M15", 500),
    ]
    
    utils = make_utils()
    all_results = {}
    
    for sym, tf, n_bars in combos:
        print(f"\n{'═' * 80}")
        print(f"  📡 {sym} {tf}")
        print(f"{'═' * 80}")
        
        df = fetch(sym, tf, n_bars)
        if df.empty:
            print("  ❌ Sem dados")
            continue
        
        print(f"  ✅ {len(df)} barras, {df['date'].nunique()} dias | {df.index[0].strftime('%d/%m')} → {df.index[-1].strftime('%d/%m')}")
        
        sym_key = "win" if "WIN" in sym else "wdo"
        base_params = DEFAULT_PARAMS.get(sym_key, {}).copy()
        
        combo_results = []
        for name, func in strategies.items():
            try:
                r = backtest_strategy(df, sym, func, base_params, utils, tf=tf)
                if r["ok"]:
                    combo_results.append((name, r))
                    icon = "✅" if r["ret"] > 0 else "❌"
                    print(f"  {icon} {name:<20} Ret {r['ret']:>+7.2f}%  WR {r['wr']:>5.1f}%  "
                          f"Sharpe {r['sharpe']:>7.2f}  PF {r['pf']:>5.2f}  "
                          f"DD {r['max_dd']:>5.2f}%  R${r['avg_daily']:>+7.0f}/dia  T={r['trades']}")
            except Exception as e:
                print(f"  ⚠️  {name:<20} ERRO: {e}")
        
        if combo_results:
            # Sort by return
            combo_results.sort(key=lambda x: x[1]["ret"], reverse=True)
            all_results[f"{sym} {tf}"] = combo_results
    
    # ===== RANKING =====
    print("\n\n" + "═" * 100)
    print("  🏆 RANKING POR COMBO — Melhor estratégia para cada operação")
    print("═" * 100)
    
    winners = {}
    for combo, results in all_results.items():
        if results:
            best_name, best_r = results[0]
            winners[combo] = (best_name, best_r)
            print(f"\n  🎯 {combo}:")
            print(f"     Melhor: {best_name}")
            print(f"     Ret: {best_r['ret']:+.2f}% | WR: {best_r['wr']:.1f}% | Sharpe: {best_r['sharpe']:.2f} | PF: {best_r['pf']:.2f}")
            print(f"     R${best_r['avg_daily']:+.0f}/dia | DD: {best_r['max_dd']:.2f}% | Trades: {best_r['trades']}")
            
            # Top 3
            print(f"     Top 3:")
            for i, (n, r) in enumerate(results[:3], 1):
                print(f"       {i}. {n:<20} Ret {r['ret']:+.2f}%  WR {r['wr']:.1f}%  PF {r['pf']:.2f}")
    
    # ===== SUMMARY TABLE =====
    print("\n\n" + "═" * 100)
    print("  📋 RESUMO — Estratégia recomendada por operação")
    print("═" * 100)
    print(f"\n{'Combo':<14} {'Estratégia':<20} {'Ret%':>7} {'WR':>6} {'Sharpe':>7} {'PF':>6} {'DD':>6} {'R$/dia':>9}")
    print("─" * 80)
    
    for combo, (name, r) in winners.items():
        print(f"{combo:<14} {name:<20} {r['ret']:>+6.2f}% {r['wr']:>5.1f}% {r['sharpe']:>6.2f} {r['pf']:>5.2f} {r['max_dd']:>5.2f}% R${r['avg_daily']:>+7.0f}")
    
    print("\n" + "═" * 100 + "\n")
    return winners


if __name__ == "__main__":
    run()
