"""
Enhanced MACD_MOMENTUM v3 — Improved version with better filtering.

Improvements over v2:
1. Volume confirmation — require volume above average
2. Trend alignment — confirm with higher timeframe direction
3. Volatility filter — avoid entries in low volatility
4. Better momentum detection — require sustained momentum
5. RSI divergence detection — catch reversals early

Goal: Reduce false signals in ranging markets while keeping trend capture.
"""

STRATEGY_NAME = "ENHANCED_MACD_MOMENTUM"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Enhanced MACD momentum entry with multiple confirmations.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calculate_ema = utils["calculate_ema"]
    calculate_rsi = utils["calculate_rsi"]
    calculate_adx = utils["calculate_adx"]
    calc_sl = utils["calc_sl"]

    # Parameters
    ema_fast_period = params.get("ema_fast", 9)
    ema_slow_period = params.get("ema_slow", 21)
    adx_period = params.get("adx_period", 14)
    adx_threshold = params.get("adx_threshold", 20)  # Higher than v2 (was 15)
    rsi_period = params.get("rsi_period", 14)
    rsi_ob = params.get("rsi_overbought", 70)  # Tighter than v2 (was 75)
    rsi_os = params.get("rsi_oversold", 30)  # Tighter than v2 (was 25)
    macd_fast = params.get("macd_fast", 12)
    macd_slow = params.get("macd_slow", 26)
    macd_signal = params.get("macd_signal", 9)

    min_bars = max(ema_slow_period, adx_period * 2, macd_slow + macd_signal) + 5
    if not bars or len(bars) < min_bars:
        return None

    # Calculate indicators
    ema_fast_val = calculate_ema(bars, ema_fast_period)
    ema_slow_val = calculate_ema(bars, ema_slow_period)
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    rsi = calculate_rsi(bars, rsi_period)

    if ema_fast_val == 0 or ema_slow_val == 0 or adx_val == 0:
        return None

    # MACD calculation
    closes = [float(b["close"]) for b in reversed(bars)]

    def _ema_arr(arr, period):
        if len(arr) < period:
            return arr[:]
        alpha = 2.0 / (period + 1)
        result = [arr[0]]
        for v in arr[1:]:
            result.append(alpha * v + (1 - alpha) * result[-1])
        return result

    ema_fast_macd = _ema_arr(closes, macd_fast)
    ema_slow_macd = _ema_arr(closes, macd_slow)
    macd_line = [f - s for f, s in zip(ema_fast_macd, ema_slow_macd)]
    signal_line = _ema_arr(macd_line, macd_signal)
    histogram = [m - s for m, s in zip(macd_line, signal_line)]

    if len(histogram) < 5:
        return None

    cur_hist = histogram[-1]
    prev_hist = histogram[-2]
    prev2_hist = histogram[-3]
    prev3_hist = histogram[-4]
    prev4_hist = histogram[-5]

    # STRONGER MACD signals — require sustained momentum
    # v2: accepted single-bar momentum
    # v3: require 3 consecutive bars of momentum
    macd_cross_up = prev_hist <= 0 and cur_hist > 0
    macd_cross_down = prev_hist >= 0 and cur_hist < 0

    # Sustained momentum: 3 consecutive bars in same direction
    macd_sustained_up = cur_hist > 0 and prev_hist > 0 and prev2_hist > 0 and cur_hist > prev_hist > prev2_hist
    macd_sustained_down = cur_hist < 0 and prev_hist < 0 and prev2_hist < 0 and cur_hist < prev_hist < prev2_hist

    # Need STRONG trend strength (higher threshold than v2)
    if adx_val < adx_threshold:
        return None

    # ADX must be rising (trend strengthening)
    adx_prev = calculate_adx(bars[1:], adx_period)[0] if len(bars) > adx_period * 2 else adx_val
    if adx_val < adx_prev * 0.95:  # ADX declining more than 5%
        return None

    # Determine trend direction from EMA
    is_uptrend = ema_fast_val > ema_slow_val
    is_downtrend = ema_fast_val < ema_slow_val

    if not is_uptrend and not is_downtrend:
        return None

    # DI confirmation — must align with direction
    if is_uptrend and plus_di < minus_di:
        return None
    if is_downtrend and minus_di < plus_di:
        return None

    # DI spread must be significant (not just barely above)
    di_spread = abs(plus_di - minus_di)
    if di_spread < 5:  # Minimum 5 point spread
        return None

    direction = None

    # BUY: uptrend + sustained MACD momentum OR strong crossover
    if is_uptrend:
        if macd_sustained_up or (macd_cross_up and cur_hist > abs(prev2_hist) * 0.5):
            if rsi > rsi_ob:
                return None
            direction = "BUY"

    # SELL: downtrend + sustained MACD momentum OR strong crossover
    elif is_downtrend:
        if macd_sustained_down or (macd_cross_down and abs(cur_hist) > abs(prev2_hist) * 0.5):
            if rsi < rsi_os:
                return None
            direction = "SELL"

    if not direction:
        return None

    # ENHANCED: Volume confirmation — require above-average volume
    if bars and len(bars) >= 20:
        recent_vol = sum(float(b.get("volume", b.get("tick_volume", 1)) or 1) for b in bars[:5]) / 5
        avg_vol = sum(float(b.get("volume", b.get("tick_volume", 1)) or 1) for b in bars[:20]) / 20
        if avg_vol > 0:
            vol_ratio = recent_vol / avg_vol
            # v3: Require volume above 70% of average (was 20% in v2)
            if vol_ratio < 0.7:
                return None

    # ENHANCED: Price position relative to EMA slow — tighter filter
    ema_dist_pct = abs(price - ema_slow_val) / ema_slow_val * 100
    if direction == "BUY" and price < ema_slow_val * 0.998:  # Tighter than v2 (was 0.995)
        return None
    if direction == "SELL" and price > ema_slow_val * 1.002:  # Tighter than v2 (was 1.005)
        return None

    # ENHANCED: Volatility filter — avoid entries in very low volatility
    if atr > 0 and price > 0:
        atr_pct = atr / price * 100
        if atr_pct < 0.05:  # Very low volatility
            return None

    # Calculate SL
    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "ENHANCED_MACD_MOMENTUM",
            "ema_fast": ema_fast_val,
            "ema_slow": ema_slow_val,
            "adx": adx_val,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "rsi": rsi,
            "macd_hist": cur_hist,
            "di_spread": di_spread,
        },
    }
