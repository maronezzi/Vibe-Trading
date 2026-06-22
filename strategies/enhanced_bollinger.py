"""
Enhanced BOLLINGER v3 — Improved Bollinger Band reversion with trend awareness.

Improvements over original BOLLINGER:
1. Trend alignment — don't fight strong trends
2. Volume confirmation — require volume support
3. Band width filter — only trade in adequate volatility
4. RSI divergence — catch reversals early
5. Price action confirmation — require rejection candles

Goal: Reduce losses from catching falling knives in trending markets.
"""

STRATEGY_NAME = "ENHANCED_BOLLINGER"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Enhanced Bollinger Band entry with multiple confirmations.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calculate_bollinger = utils["calculate_bollinger"]
    calculate_rsi = utils["calculate_rsi"]
    calculate_adx = utils["calculate_adx"]
    calculate_ema = utils["calculate_ema"]
    calc_sl = utils["calc_sl"]

    # Parameters
    bb_period = params.get("bb_period", 20)
    bb_std = params.get("bb_std", 2.0)
    rsi_period = params.get("rsi_period", 14)
    rsi_ob = params.get("rsi_overbought", 70)
    rsi_os = params.get("rsi_oversold", 30)
    adx_period = params.get("adx_period", 14)
    adx_threshold = params.get("adx_threshold", 25)
    ema_period = params.get("ema_period", 21)

    if not bars or len(bars) < max(bb_period, rsi_period, adx_period, ema_period) + 10:
        return None

    bb_upper, bb_mid, bb_lower = calculate_bollinger(bars, bb_period, bb_std)
    if bb_upper == 0 or bb_lower == 0 or bb_mid == 0:
        return None

    rsi = calculate_rsi(bars, rsi_period)

    direction = None
    if price <= bb_lower:
        direction = "BUY"  # Price touched lower band → buy (reversion)
    elif price >= bb_upper:
        direction = "SELL"  # Price touched upper band → sell (reversion)

    if not direction:
        return None

    # RSI confirmation — require oversold/overbought
    if direction == "BUY" and rsi > rsi_os:
        return None  # RSI not oversold enough
    if direction == "SELL" and rsi < rsi_ob:
        return None  # RSI not overbought enough

    # ENHANCED: Trend alignment — don't fight strong trends
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    if adx_val > 0 and adx_val > adx_threshold:
        # In strong trends, don't counter-trade
        if direction == "BUY" and plus_di < minus_di:
            return None  # Strong downtrend — don't buy
        if direction == "SELL" and minus_di < plus_di:
            return None  # Strong uptrend — don't sell

    # ENHANCED: Band width filter — only trade in adequate volatility
    bb_width_pct = (bb_upper - bb_lower) / bb_mid * 100 if bb_mid > 0 else 0
    if bb_width_pct < 1.0:  # Bands too narrow — low volatility
        return None
    if bb_width_pct > 5.0:  # Bands too wide — extreme volatility
        return None

    # ENHANCED: EMA trend filter
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

    # ENHANCED: Volume confirmation
    if bars and len(bars) >= 20:
        recent_vol = sum(float(b.get("volume", b.get("tick_volume", 1)) or 1) for b in bars[:5]) / 5
        avg_vol = sum(float(b.get("volume", b.get("tick_volume", 1)) or 1) for b in bars[:20]) / 20
        if avg_vol > 0:
            vol_ratio = recent_vol / avg_vol
            # Require at least 80% of average volume
            if vol_ratio < 0.8:
                return None

    # ENHANCED: Price action confirmation — check for rejection candle
    # Look for a candle that shows rejection of the band touch
    if len(bars) >= 3:
        candle = bars[0]
        candle_body = abs(float(candle["close"]) - float(candle["open"]))
        candle_range = float(candle["high"]) - float(candle["low"])

        if candle_range > 0:
            body_ratio = candle_body / candle_range

            # For BUY: look for bullish rejection (lower wick)
            if direction == "BUY":
                lower_wick = (
                    float(candle["open"]) - float(candle["low"])
                    if float(candle["close"]) > float(candle["open"])
                    else float(candle["close"]) - float(candle["low"])
                )
                if lower_wick / candle_range < 0.3:  # Short lower wick — no rejection
                    return None

            # For SELL: look for bearish rejection (upper wick)
            elif direction == "SELL":
                upper_wick = (
                    float(candle["high"]) - float(candle["open"])
                    if float(candle["close"]) < float(candle["open"])
                    else float(candle["high"]) - float(candle["close"])
                )
                if upper_wick / candle_range < 0.3:  # Short upper wick — no rejection
                    return None

    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "ENHANCED_BOLLINGER",
            "bb_upper": bb_upper,
            "bb_mid": bb_mid,
            "bb_lower": bb_lower,
            "rsi": rsi,
            "adx": adx_val,
            "bb_width_pct": bb_width_pct,
        },
    }
