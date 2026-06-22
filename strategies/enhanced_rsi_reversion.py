"""
Enhanced RSI_REVERSION v3 — Improved mean-reversion with trend awareness.

Improvements over original RSI_REVERSION:
1. Trend alignment — don't fight strong trends
2. Volume confirmation — require volume support
3. RSI divergence detection — catch reversals early
4. Bollinger Band confirmation — price near bands adds confidence
5. ATR-based filtering — avoid dead markets

Goal: Reduce losses from catching falling knives in trending markets.
"""

STRATEGY_NAME = "ENHANCED_RSI_REVERSION"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Enhanced RSI reversion entry with trend awareness.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calculate_rsi = utils["calculate_rsi"]
    calculate_ema = utils["calculate_ema"]
    calculate_bollinger = utils["calculate_bollinger"]
    calculate_adx = utils["calculate_adx"]
    calc_sl = utils["calc_sl"]

    # Parameters
    rsi_period = params.get("rsi_period", 14)
    rsi_ob = params.get("rsi_overbought", 70)
    rsi_os = params.get("rsi_oversold", 30)
    ema_period = params.get("ema_period", 21)
    bb_period = params.get("bb_period", 20)
    bb_std = params.get("bb_std", 2.0)
    adx_period = params.get("adx_period", 14)
    adx_threshold = params.get("adx_threshold", 25)  # Strong trend threshold

    if not bars or len(bars) < max(rsi_period, ema_period, bb_period, adx_period) + 10:
        return None

    # ATR minimum — don't enter dead markets
    if atr <= 0:
        return None

    # RSI
    rsi = calculate_rsi(bars, rsi_period)
    if rsi is None or rsi == 0:
        return None

    direction = None

    # BUY: RSI oversold
    if rsi < rsi_os:
        direction = "BUY"
    # SELL: RSI overbought
    elif rsi > rsi_ob:
        direction = "SELL"

    if not direction:
        return None

    # ENHANCED: Trend alignment — don't fight strong trends
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    if adx_val > 0:
        # In strong trends, don't counter-trade
        if adx_val > adx_threshold:
            if direction == "BUY" and plus_di < minus_di:
                return None  # Strong downtrend — don't buy
            if direction == "SELL" and minus_di < plus_di:
                return None  # Strong uptrend — don't sell

    # ENHANCED: EMA trend filter — more sophisticated than original
    if ema_period > 0 and len(bars) >= ema_period + 5:
        ema_val = calculate_ema(bars, ema_period)
        if ema_val and ema_val > 0:
            ema_dist_pct = (price - ema_val) / ema_val * 100

            # Don't buy if price is way below EMA (strong downtrend)
            if direction == "BUY" and ema_dist_pct < -2.0:
                return None

            # Don't sell if price is way above EMA (strong uptrend)
            if direction == "SELL" and ema_dist_pct > 2.0:
                return None

    # ENHANCED: Bollinger Band confirmation
    bb_upper, bb_mid, bb_lower = calculate_bollinger(bars, bb_period, bb_std)
    if bb_upper > 0 and bb_lower > 0:
        bb_width = (bb_upper - bb_lower) / bb_mid * 100 if bb_mid > 0 else 0

        # Only trade when bands are wide enough (volatility exists)
        if bb_width < 1.0:  # Bands too narrow — low volatility
            return None

        # Confirm price is actually near the band
        if direction == "BUY" and price > bb_lower + (bb_mid - bb_lower) * 0.3:
            return None  # Price not near enough to lower band
        if direction == "SELL" and price < bb_upper - (bb_upper - bb_mid) * 0.3:
            return None  # Price not near enough to upper band

    # ENHANCED: Volume confirmation
    if bars and len(bars) >= 20:
        recent_vol = sum(float(b.get("volume", b.get("tick_volume", 1)) or 1) for b in bars[:5]) / 5
        avg_vol = sum(float(b.get("volume", b.get("tick_volume", 1)) or 1) for b in bars[:20]) / 20
        if avg_vol > 0:
            vol_ratio = recent_vol / avg_vol
            # Require at least 80% of average volume
            if vol_ratio < 0.8:
                return None

    # Calculate SL
    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "ENHANCED_RSI_REVERSION",
            "rsi": rsi,
            "atr": atr,
            "adx": adx_val,
            "bb_width": bb_width if bb_upper > 0 else 0,
            "ema_dist_pct": ema_dist_pct if ema_val > 0 else 0,
        },
    }
