"""
Estratégia STRONG_TREND — Trend-following agressivo para WIN.
Permite entradas mesmo com RSI overbought quando ADX confirma tendência forte.

Diferente do ADX_TREND, esta estratégia:
1. Não filtra RSI em tendências fortes (ADX > 40)
2. Entra mais cedo na tendência
3. Usa trailing stop agressivo para capturar momentum

Parâmetros (via vt_config.json → win):
  ema_fast, ema_slow, adx_period, adx_threshold
  rsi_period, rsi_overbought, rsi_oversold
  sl_atr_mult, trail_activate, trail_distance
"""

STRATEGY_NAME = "STRONG_TREND"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada STRONG_TREND.
    
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
    adx_threshold = params.get("adx_threshold", 30)
    rsi_period = params.get("rsi_period", 14)
    
    min_bars = max(ema_slow_period, adx_period * 2) + 5
    if not bars or len(bars) < min_bars:
        return None
    
    # Calculate indicators
    ema_fast_val = calculate_ema(bars, ema_fast_period)
    ema_slow_val = calculate_ema(bars, ema_slow_period)
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    rsi = calculate_rsi(bars, rsi_period)
    
    if ema_fast_val == 0 or ema_slow_val == 0 or adx_val == 0:
        return None
    
    # Need minimum trend strength
    if adx_val < adx_threshold:
        return None
    
    # Determine direction from EMA
    direction = None
    if ema_fast_val > ema_slow_val:
        direction = "BUY"
    elif ema_fast_val < ema_slow_val:
        direction = "SELL"
    
    if not direction:
        return None
    
    # DI confirmation
    if direction == "BUY" and plus_di < minus_di:
        return None
    if direction == "SELL" and minus_di < plus_di:
        return None
    
    # Price position — must be on the right side of EMA slow
    if direction == "BUY" and price < ema_slow_val * 0.998:
        return None  # Too far below EMA — trend broken
    if direction == "SELL" and price > ema_slow_val * 1.002:
        return None
    
    # RSI filter — ONLY filter extremes, not overbought/oversold
    # In strong trends, RSI can stay overbought/oversold for extended periods
    if adx_val < 40:
        # Moderate trend: apply RSI filter
        if direction == "BUY" and rsi > 80:
            return None  # Extreme overbought
        if direction == "SELL" and rsi < 20:
            return None  # Extreme oversold
    # Strong trend (ADX >= 40): no RSI filter — let the trend ride
    
    # Volume confirmation
    if bars and len(bars) >= 20:
        recent_vol = sum(b["volume"] for b in bars[:5]) / 5
        avg_vol = sum(b["volume"] for b in bars[:20]) / 20
        if avg_vol > 0 and recent_vol < avg_vol * 0.3:
            return None  # Very low volume — no conviction
    
    # Calculate SL
    sl_pts = calc_sl(symbol, atr, params)
    
    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "STRONG_TREND",
            "ema_fast": ema_fast_val,
            "ema_slow": ema_slow_val,
            "adx": adx_val,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "rsi": rsi,
        },
    }
