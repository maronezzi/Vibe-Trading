"""AGI4_BIT_202523 - Estratégia BIT_H1 com volatilidade + session filter.

Foco: BTC-like pair (BIT) em H1. Combina:
- VWAP intraday (reseta a cada dia)
- Range contraction (squeeze) → breakout direcional
- RSI(2) para confirmação direcional
- Filtro de sessão (Asia/London/NY)
- ATR-based SL via utils.calc_sl
"""

STRATEGY_NAME = "AGI4_BIT_202523"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Avalia sinal em BIT_H1 usando squeeze + VWAP + RSI(2).

    Retorna None ou dict com direction/sl_pts/info.
    """
    # --- Guards básicos ---
    if not bars or len(bars) < 50:
        return None
    if atr is None or atr <= 0:
        return None

    # --- Parâmetros ---
    bb_period = params.get("bb_period", 20)
    bb_std = params.get("bb_std", 2.0)
    rsi_period = params.get("rsi_period", 2)
    rsi_ob = params.get("rsi_overbought", 90)
    rsi_os = params.get("rsi_oversold", 10)
    squeeze_lookback = params.get("squeeze_lookback", 20)
    squeeze_pct = params.get("squeeze_pct", 0.6)
    vwap_threshold_pct = params.get("vwap_threshold_pct", 0.0015)
    # Session filter: hour ranges in UTC. Default = active during London+NY overlap.
    # BIT trades 24/7 but vol + directional moves concentrate in these windows.
    active_hours = params.get("active_hours", [12, 13, 14, 15, 16, 17, 18, 19, 20])

    # --- Session filter ---
    try:
        bar_hour = bar_ts.hour
    except AttributeError:
        return None
    if active_hours and bar_hour not in active_hours:
        return None

    # --- Utils ---
    calculate_rsi = utils["calculate_rsi"]
    calculate_bollinger = utils["calculate_bollinger"]
    calc_sl = utils["calc_sl"]

    # --- Bollinger width (squeeze detection) ---
    bb = calculate_bollinger(bars, bb_period, bb_std)
    if not bb or "width" not in bb:
        return None
    current_width = bb["width"]
    upper = bb["upper"]
    lower = bb["lower"]
    middle = bb["middle"]

    # Compute historical bandwidth average for squeeze detection
    widths = []
    for i in range(max(0, len(bars) - squeeze_lookback), len(bars)):
        hist_bb = calculate_bollinger(bars[: i + 1], bb_period, bb_std)
        if hist_bb and "width" in hist_bb:
            widths.append(hist_bb["width"])
    if len(widths) < 5:
        return None
    avg_width = sum(widths) / len(widths)
    is_squeeze = current_width < avg_width * squeeze_pct

    # --- RSI(2) directional bias ---
    rsi_val = calculate_rsi(bars, rsi_period)
    if rsi_val is None:
        return None

    # --- VWAP proxy via typical price cumulative since session start ---
    # Build intraday VWAP from bars in the same UTC date as bar_ts.
    try:
        same_day_bars = [b for b in bars if b.get("time") is not None and _same_utc_day(b["time"], bar_ts)]
    except Exception:
        same_day_bars = []
    if len(same_day_bars) < 5:
        return None

    pv_sum = 0.0
    v_sum = 0.0
    for b in same_day_bars:
        h = b.get("high", price)
        lo = b.get("low", price)
        c = b.get("close", price)
        typical = (h + lo + c) / 3.0
        vol = b.get("volume", 0) or 0
        pv_sum += typical * vol
        v_sum += vol
    if v_sum <= 0:
        # Fallback: simple mean of typical prices
        vwap = sum((b.get("high", price) + b.get("low", price) + b.get("close", price)) / 3.0 for b in same_day_bars) / len(same_day_bars)
    else:
        vwap = pv_sum / v_sum

    vwap_dist_pct = (price - vwap) / vwap if vwap > 0 else 0.0

    # --- SL calculation (Lei 3) ---
    sl_pts = calc_sl(symbol, atr, params)
    if sl_pts is None or sl_pts <= 0:
        return None

    # --- Entry logic: squeeze release + directional confirmation + VWAP alignment ---
    info = {
        "rsi": round(rsi_val, 2),
        "bb_width": round(current_width, 4),
        "bb_avg_width": round(avg_width, 4),
        "is_squeeze": is_squeeze,
        "vwap": round(vwap, 2),
        "vwap_dist_pct": round(vwap_dist_pct * 100, 4),
        "upper": round(upper, 2),
        "lower": round(lower, 2),
        "middle": round(middle, 2),
        "sl_pts": int(sl_pts),
        "bar_hour_utc": bar_hour,
    }

    # BUY: above VWAP, RSI bullish but not overbought, price above middle band
    if vwap_dist_pct > vwap_threshold_pct and rsi_val > 50 and rsi_val < rsi_ob:
        if price > middle:
            return {
                "direction": "BUY",
                "sl_pts": int(sl_pts),
                "info": {**info, "trigger": "vwap_above_rsi_confirm"},
            }
        if is_squeeze and price > upper:
            return {
                "direction": "BUY",
                "sl_pts": int(sl_pts),
                "info": {**info, "trigger": "squeeze_release_long"},
            }

    # SELL: below VWAP, RSI bearish but not oversold, price below middle band
    if vwap_dist_pct < -vwap_threshold_pct and rsi_val < 50 and rsi_val > rsi_os:
        if price < middle:
            return {
                "direction": "SELL",
                "sl_pts": int(sl_pts),
                "info": {**info, "trigger": "vwap_below_rsi_confirm"},
            }
        if is_squeeze and price < lower:
            return {
                "direction": "SELL",
                "sl_pts": int(sl_pts),
                "info": {**info, "trigger": "squeeze_release_short"},
            }

    return None


def _same_utc_day(ts_a, ts_b):
    """Compara dois timestamps no mesmo dia UTC."""
    try:
        return ts_a.year == ts_b.year and ts_a.month == ts_b.month and ts_a.day == ts_b.day
    except Exception:
        return False
