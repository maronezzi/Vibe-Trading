"""
backtest_v7_split.py — Split Strategy Backtest matching vt_autotrader.py
WDO: VWAP(20) mean-reversion with adaptive thresholds
WIN: EMA(12/21) Crossover + ADX filter (trend-following)
"""

import sys, csv, io, subprocess, os
from pathlib import Path
import numpy as np, pandas as pd

WINE_PYTHON = os.path.expanduser("~/.wine/drive_c/Python311/python.exe")
FETCH_SCRIPT = os.path.join(os.path.dirname(__file__), "mt5_fetch.py")
CLOSE_HOUR, CLOSE_MINUTE = 16, 45

CONTRACT_SPECS = {
    "WIN$": {"mult": 0.20, "name": "Mini Indice", "margin": 5000, "tick": 5, "slip_r": 1.0},
    "WDO$": {"mult": 10.0, "name": "Mini Dolar", "margin": 3000, "tick": 0.5, "slip_r": 5.0},
}
COMMISSION = 2.5

# ===== PARAMS =====
# WDO: VWAP strategy
WDO_VWAP_PERIOD = 20
WDO_VWAP_BUY_THRESHOLD = 1.003
WDO_VWAP_SELL_THRESHOLD = 0.997
WDO_SL_ATR_MULT = 1.0
WDO_TRAIL_ACTIVATE = 1.5
WDO_TRAIL_DISTANCE = 0.3
WDO_COOLDOWN = 600  # seconds
WDO_MAX_TRADES = 8
WDO_EMA_FAST = 9
WDO_EMA_SLOW = 21
WDO_TREND_MIN_SPREAD = 0.001

# WIN: EMA Crossover + ADX
WIN_EMA_FAST = 12
WIN_EMA_SLOW = 21
WIN_ADX_PERIOD = 14
WIN_ADX_THRESHOLD = 20
WIN_RSI_PERIOD = 14
WIN_RSI_OB = 70
WIN_RSI_OS = 30
WIN_SL_ATR_MULT = 1.5
WIN_TRAIL_ACTIVATE = 1.5
WIN_TRAIL_DISTANCE = 0.3
WIN_COOLDOWN = 900
WIN_MAX_TRADES = 6

ATR_PERIOD = 14
MAX_CT = 1
SL_MIN_WIN = 200
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
    plus_di = 100 * (plus_dm.rolling(period).mean() / atr_smooth)
    minus_di = 100 * (minus_dm.rolling(period).mean() / atr_smooth)
    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di))
    adx = dx.rolling(period).mean()
    return adx, plus_di, minus_di


def calc_ema(df, period):
    return df["close"].ewm(span=period, adjust=False).mean()


def backtest_wdo_vwap(df, capital=100000.0):
    """VWAP strategy for WDO — matching autotrader logic."""
    spec = CONTRACT_SPECS["WDO$"]
    mult, margin, slip_r = spec["mult"], spec["margin"], spec["slip_r"]

    atr = calc_atr(df, ATR_PERIOD)
    vwap = calc_vwap(df, WDO_VWAP_PERIOD)
    ema_f = calc_ema(df, WDO_EMA_FAST)
    ema_s = calc_ema(df, WDO_EMA_SLOW)
    rsi = calc_rsi(df, 14)

    cash = capital
    pos = 0; ep = 0; e_date = None; e_atr = 0; best = 0; sl_price = 0
    trail_on = False; sl_pts = 0; bars_in_trade = 0; last_trade_ts = None

    equity, trade_log, daily_pnl = [], [], []
    n_trades = n_wins = n_long = n_short = 0
    n_sl = n_trail = n_close = 0
    gross_win = 0; gross_loss_val = 0
    daily_pnl_dict = {}
    daily_trade_count = 0
    current_date = None

    def _close(price, reason):
        nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, e_atr
        nonlocal n_trades, n_wins, n_long, n_short, n_sl, n_trail, n_close
        nonlocal gross_win, gross_loss_val, bars_in_trade, daily_trade_count
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
        nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, e_atr, sl_pts, bars_in_trade
        nonlocal daily_trade_count, last_trade_ts
        if pos != 0:
            return False
        raw_sl = int(cur_atr * WDO_SL_ATR_MULT)
        raw_sl = max(raw_sl, SL_MIN_WDO)
        raw_sl = ((raw_sl + 4) // 5) * 5
        cost = slip_r * MAX_CT + COMMISSION * MAX_CT
        if cash >= margin * MAX_CT + cost:
            cash -= margin * MAX_CT + cost
            pos = 1 if direction == "BUY" else -1
            ep = price; e_date = date; e_atr = cur_atr; sl_pts = raw_sl
            best = price; trail_on = False; bars_in_trade = 0
            if pos == 1:
                sl_price = price - raw_sl
            else:
                sl_price = price + raw_sl
            daily_trade_count += 1
            last_trade_ts = date
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
        cur_rsi = float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else 50
        cur_ema_f = float(ema_f.iloc[i]) if not pd.isna(ema_f.iloc[i]) else 0
        cur_ema_s = float(ema_s.iloc[i]) if not pd.isna(ema_s.iloc[i]) else 0

        # Reset daily counter
        if current_date != date.date() if hasattr(date, 'date') else date:
            current_date = date.date() if hasattr(date, 'date') else date
            daily_trade_count = 0

        # Skip first 15 min and last 15 min
        cur_min = hour * 60 + minute
        if (9 * 60 + 5 <= cur_min <= 9 * 60 + 20) or (16 * 60 + 30 <= cur_min <= 16 * 60 + 45):
            # Mark-to-market if in position
            if pos == 1:
                equity.append(cash + (price - ep) * mult * MAX_CT + margin * MAX_CT)
            elif pos == -1:
                equity.append(cash + (ep - price) * mult * MAX_CT + margin * MAX_CT)
            else:
                equity.append(cash)
            continue

        # Mark-to-market
        if pos == 1:
            eq_val = cash + (price - ep) * mult * MAX_CT + margin * MAX_CT
        elif pos == -1:
            eq_val = cash + (ep - price) * mult * MAX_CT + margin * MAX_CT
        else:
            eq_val = cash
        equity.append(eq_val)

        # ===== NO POSITION =====
        if pos == 0:
            if cur_atr > 0 and cur_vwap > 0 and daily_trade_count < WDO_MAX_TRADES:
                # Market regime check
                if cur_ema_f > 0 and cur_ema_s > 0:
                    spread = abs(cur_ema_f - cur_ema_s) / price if price > 0 else 0
                    if spread < WDO_TREND_MIN_SPREAD:
                        continue  # CHOPPY — don't trade

                # Adaptive threshold
                atr_pct = cur_atr / price if price > 0 else 0
                if atr_pct < 0.0015:
                    buy_mult = 1.0005; sell_mult = 0.9995
                elif atr_pct < 0.003:
                    buy_mult = 1.0015; sell_mult = 0.9985
                else:
                    buy_mult = WDO_VWAP_BUY_THRESHOLD; sell_mult = WDO_VWAP_SELL_THRESHOLD

                buy_thresh = cur_vwap * buy_mult
                sell_thresh = cur_vwap * sell_mult

                direction = None
                if price > buy_thresh:
                    direction = "BUY"
                elif price < sell_thresh:
                    direction = "SELL"

                if direction:
                    # Trend filter
                    if cur_ema_f > 0 and cur_ema_s > 0:
                        if direction == "BUY" and cur_ema_f < cur_ema_s:
                            continue
                        if direction == "SELL" and cur_ema_f > cur_ema_s:
                            continue

                    # RSI filter
                    if direction == "BUY" and cur_rsi > 70:
                        continue
                    if direction == "SELL" and cur_rsi < 30:
                        continue

                    # Cooldown
                    if last_trade_ts and (date - last_trade_ts).total_seconds() < WDO_COOLDOWN:
                        continue

                    _open(direction, price, date, cur_atr)
            continue

        # ===== POSITION OPEN =====
        bars_in_trade += 1

        if pos == 1:
            best = max(best, high)
        elif pos == -1:
            best = min(best, low) if best > 0 else low

        if pos == 1:
            profit_pts = best - ep
        else:
            profit_pts = ep - best

        if not trail_on and e_atr > 0 and profit_pts >= WDO_TRAIL_ACTIVATE * e_atr:
            trail_on = True

        if trail_on and e_atr > 0:
            trail_dist = WDO_TRAIL_DISTANCE * e_atr
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

    return _compute_stats(equity, trade_log, daily_pnl_dict, cash, capital, n_trades, n_wins,
                          n_long, n_short, n_sl, n_trail, n_close, gross_win, gross_loss_val)


def backtest_win_ema_cross(df, capital=100000.0):
    """EMA Crossover + ADX for WIN — matching autotrader logic."""
    spec = CONTRACT_SPECS["WIN$"]
    mult, margin, slip_r = spec["mult"], spec["margin"], spec["slip_r"]

    atr = calc_atr(df, ATR_PERIOD)
    ema_f = calc_ema(df, WIN_EMA_FAST)
    ema_s = calc_ema(df, WIN_EMA_SLOW)
    adx, plus_di, minus_di = calc_adx(df, WIN_ADX_PERIOD)
    rsi = calc_rsi(df, WIN_RSI_PERIOD)

    cash = capital
    pos = 0; ep = 0; e_date = None; e_atr = 0; best = 0; sl_price = 0
    trail_on = False; sl_pts = 0; bars_in_trade = 0; last_trade_ts = None

    equity, trade_log, daily_pnl = [], [], []
    n_trades = n_wins = n_long = n_short = 0
    n_sl = n_trail = n_close = 0
    gross_win = 0; gross_loss_val = 0
    daily_pnl_dict = {}
    daily_trade_count = 0
    current_date = None

    def _close(price, reason):
        nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, e_atr
        nonlocal n_trades, n_wins, n_long, n_short, n_sl, n_trail, n_close
        nonlocal gross_win, gross_loss_val, bars_in_trade, daily_trade_count
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
        nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, e_atr, sl_pts, bars_in_trade
        nonlocal daily_trade_count, last_trade_ts
        if pos != 0:
            return False
        raw_sl = int(cur_atr * WIN_SL_ATR_MULT)
        raw_sl = max(raw_sl, SL_MIN_WIN)
        raw_sl = ((raw_sl + 4) // 5) * 5
        cost = slip_r * MAX_CT + COMMISSION * MAX_CT
        if cash >= margin * MAX_CT + cost:
            cash -= margin * MAX_CT + cost
            pos = 1 if direction == "BUY" else -1
            ep = price; e_date = date; e_atr = cur_atr; sl_pts = raw_sl
            best = price; trail_on = False; bars_in_trade = 0
            if pos == 1:
                sl_price = price - raw_sl
            else:
                sl_price = price + raw_sl
            daily_trade_count += 1
            last_trade_ts = date
            return True
        return False

    for i, (date, row) in enumerate(df.iterrows()):
        price = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        hour = int(row["hour"])
        minute = int(row["minute"])
        cur_atr = float(atr.iloc[i]) if i > 0 and not pd.isna(atr.iloc[i]) else 0
        cur_ema_f = float(ema_f.iloc[i]) if not pd.isna(ema_f.iloc[i]) else 0
        cur_ema_s = float(ema_s.iloc[i]) if not pd.isna(ema_s.iloc[i]) else 0
        cur_adx = float(adx.iloc[i]) if not pd.isna(adx.iloc[i]) else 0
        cur_plus_di = float(plus_di.iloc[i]) if not pd.isna(plus_di.iloc[i]) else 0
        cur_minus_di = float(minus_di.iloc[i]) if not pd.isna(minus_di.iloc[i]) else 0
        cur_rsi = float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else 50

        # Previous EMA for crossover detection
        prev_ema_f = float(ema_f.iloc[i-1]) if i > 0 and not pd.isna(ema_f.iloc[i-1]) else cur_ema_f
        prev_ema_s = float(ema_s.iloc[i-1]) if i > 0 and not pd.isna(ema_s.iloc[i-1]) else cur_ema_s

        # Reset daily counter
        if current_date != (date.date() if hasattr(date, 'date') else date):
            current_date = date.date() if hasattr(date, 'date') else date
            daily_trade_count = 0

        # Skip first 15 min and last 15 min
        cur_min = hour * 60 + minute
        if (9 * 60 + 5 <= cur_min <= 9 * 60 + 20) or (16 * 60 + 30 <= cur_min <= 16 * 60 + 45):
            if pos == 1:
                equity.append(cash + (price - ep) * mult * MAX_CT + margin * MAX_CT)
            elif pos == -1:
                equity.append(cash + (ep - price) * mult * MAX_CT + margin * MAX_CT)
            else:
                equity.append(cash)
            continue

        # Mark-to-market
        if pos == 1:
            eq_val = cash + (price - ep) * mult * MAX_CT + margin * MAX_CT
        elif pos == -1:
            eq_val = cash + (ep - price) * mult * MAX_CT + margin * MAX_CT
        else:
            eq_val = cash
        equity.append(eq_val)

        # ===== NO POSITION =====
        if pos == 0:
            if cur_atr > 0 and cur_ema_f > 0 and cur_ema_s > 0 and cur_adx > 0:
                if cur_adx < WIN_ADX_THRESHOLD:
                    continue  # Weak trend

                if daily_trade_count >= WIN_MAX_TRADES:
                    continue

                # Crossover detection
                direction = None
                if prev_ema_f <= prev_ema_s and cur_ema_f > cur_ema_s:
                    direction = "BUY"
                elif prev_ema_f >= prev_ema_s and cur_ema_f < cur_ema_s:
                    direction = "SELL"

                if not direction:
                    continue

                # RSI filter
                if direction == "BUY" and cur_rsi > WIN_RSI_OB:
                    continue
                if direction == "SELL" and cur_rsi < WIN_RSI_OS:
                    continue

                # DI filter
                if direction == "BUY" and cur_plus_di < cur_minus_di:
                    continue
                if direction == "SELL" and cur_minus_di < cur_plus_di:
                    continue

                # Cooldown
                if last_trade_ts and (date - last_trade_ts).total_seconds() < WIN_COOLDOWN:
                    continue

                _open(direction, price, date, cur_atr)
            continue

        # ===== POSITION OPEN =====
        bars_in_trade += 1

        if pos == 1:
            best = max(best, high)
        elif pos == -1:
            best = min(best, low) if best > 0 else low

        if pos == 1:
            profit_pts = best - ep
        else:
            profit_pts = ep - best

        if not trail_on and e_atr > 0 and profit_pts >= WIN_TRAIL_ACTIVATE * e_atr:
            trail_on = True

        if trail_on and e_atr > 0:
            trail_dist = WIN_TRAIL_DISTANCE * e_atr
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

    return _compute_stats(equity, trade_log, daily_pnl_dict, cash, capital, n_trades, n_wins,
                          n_long, n_short, n_sl, n_trail, n_close, gross_win, gross_loss_val)


def _compute_stats(equity, trade_log, daily_pnl_dict, cash, capital, n_trades, n_wins,
                   n_long, n_short, n_sl, n_trail, n_close, gross_win, gross_loss_val):
    eq = pd.Series(equity) if equity else pd.Series([capital])
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

    reasons = {}
    for t in trade_log:
        r = t["reason"]
        if r not in reasons:
            reasons[r] = {"count": 0, "pnl": 0, "wins": 0}
        reasons[r]["count"] += 1
        reasons[r]["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            reasons[r]["wins"] += 1

    avg_bars = np.mean([t["bars"] for t in trade_log]) if trade_log else 0

    return {
        "ok": True, "trades": n_trades, "wins": n_wins, "wr": wr,
        "long": n_long, "short": n_short,
        "ret": total_ret, "sharpe": sharpe, "max_dd": max_dd,
        "pf": pf, "avg_daily": avg_daily, "n_days": n_days,
        "avg_win": avg_win, "avg_loss": avg_loss, "payoff": payoff,
        "n_sl": n_sl, "n_trail": n_trail, "n_close": n_close,
        "avg_bars": avg_bars, "reasons": reasons,
        "trade_log": trade_log, "equity": eq, "daily_pnl": daily_pnl_dict,
    }


def run():
    print("\n" + "="*100)
    print("  BACKTEST v7 — SPLIT STRATEGY (WDO=VWAP, WIN=EMA_CROSS+ADX)")
    print("="*100)

    combos = [
        ("WIN$", "M5", 500, "WIN_EMA_CROSSOVER"),
        ("WIN$", "M15", 500, "WIN_EMA_CROSSOVER"),
        ("WDO$", "M5", 500, "WDO_VWAP"),
        ("WDO$", "M15", 500, "WDO_VWAP"),
    ]
    all_results = []

    for sym, tf, n_bars, strat_name in combos:
        spec = CONTRACT_SPECS[sym]
        print(f"\n  {sym} ({spec['name']}) {tf} — {n_bars} bars | Strategy: {strat_name}")
        df = fetch(sym, tf, n_bars)
        if df.empty:
            print("  NO DATA"); continue

        n_days = df["date"].nunique()
        atr_avg = calc_atr(df, ATR_PERIOD).mean()
        print(f"  {len(df)} bars, {n_days} days | {df.index[0].strftime('%d/%m')} -> {df.index[-1].strftime('%d/%m')}")
        print(f"  ATR avg: {atr_avg:.1f}")

        if "WDO" in sym:
            r = backtest_wdo_vwap(df)
        else:
            r = backtest_win_ema_cross(df)

        if r["ok"]:
            print(f"\n  RESULTS — {sym} {tf} ({strat_name})")
            print(f"  {'-'*60}")
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
            print(f"  Avg Bars: {r['avg_bars']:.1f}")

            print(f"\n  Exit Reasons:")
            for reason, data in sorted(r["reasons"].items(), key=lambda x: x[1]["pnl"], reverse=True):
                pct = data["count"] / r["trades"] * 100 if r["trades"] else 0
                wr_r = data["wins"] / data["count"] * 100 if data["count"] else 0
                print(f"    {reason:<6}: {data['count']:>3} ({pct:4.1f}%) WR {wr_r:.0f}% PnL R${data['pnl']:+.0f}")

            all_results.append({
                "symbol": sym, "tf": tf, "strategy": strat_name,
                **r, "reasons": r["reasons"], "daily_pnl": r["daily_pnl"],
            })

    if all_results:
        ranking = sorted(all_results, key=lambda x: x["ret"], reverse=True)
        print("\n\n" + "="*100)
        print("  RANKING — v7 SPLIT STRATEGY")
        print("="*100)
        print(f"\n{'#':>2} {'Ativo':<7} {'TF':<4} {'Strategy':<20} {'Ret%':>7} {'T':>4} {'WR':>6} {'Sharpe':>7} {'DD':>7} {'PF':>6} {'R$/dia':>9}")
        print("-"*100)
        for i, r in enumerate(ranking, 1):
            medal = "1st" if i == 1 else "2nd" if i == 2 else "3rd" if i == 3 else f"{i:>2}"
            print(f"{medal:>2} {r['symbol']:<7} {r['tf']:<4} {r['strategy']:<20} "
                  f"{r['ret']:>+6.2f}% {r['trades']:>3}  "
                  f"{r['wr']:>5.1f}% {r['sharpe']:>6.2f}  "
                  f"{r['max_dd']:>6.2f}% {r['pf']:>5.2f} "
                  f"R${r['avg_daily']:>+8.1f}")

        profitable = [x for x in all_results if x["ret"] > 0]
        print(f"\n  Profitable: {len(profitable)}/{len(all_results)}")
        for p in profitable:
            print(f"    + {p['symbol']} {p['tf']} ({p['strategy']}) — {p['ret']:+.2f}% | "
                  f"Sharpe {p['sharpe']:.2f} | PF {p['pf']:.2f} | R${p['avg_daily']:+.0f}/dia")

    print("\n" + "="*100 + "\n")
    return all_results


if __name__ == "__main__":
    run()
