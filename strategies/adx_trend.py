"""
Estratégia ADX_TREND — Trend-following agressivo com ADX + EMA + DI.
Para WIN (mercado trending forte).

Diferente do EMA_PULLBACK (muito restritivo), esta estratégia entra
mais agressivamente quando a tendência está confirmada.

Lógica:
1. ADX > threshold (tendência forte)
2. EMA fast vs slow confirma direção
3. +DI/-DI confirma direção
4. RSI filtra extremos
5. Preço acima/abaixo EMA slow (confirmação)

Parâmetros (via vt_config.json → win):
  ema_fast, ema_slow, adx_period, adx_threshold
  rsi_period, rsi_overbought, rsi_oversold
  sl_atr_mult, trail_activate, trail_distance
"""

STRATEGY_NAME = "ADX_TREND"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada ADX_TREND.
    
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
    adx_threshold = params.get("adx_threshold", 25)
    rsi_period = params.get("rsi_period", 14)
    rsi_ob = params.get("rsi_overbought", 70)
    rsi_os = params.get("rsi_oversold", 30)
    
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
    
    # Determine direction from EMA crossover
    direction = None
    if ema_fast_val > ema_slow_val:
        direction = "BUY"
    elif ema_fast_val < ema_slow_val:
        direction = "SELL"
    
    if not direction:
        return None
    
    # DI confirmation — must align with direction
    if direction == "BUY" and plus_di < minus_di:
        return None
    if direction == "SELL" and minus_di < plus_di:
        return None
    
    # Price position relative to EMA slow
    if direction == "BUY" and price < ema_slow_val:
        return None  # Price below slow EMA — trend not confirmed
    if direction == "SELL" and price > ema_slow_val:
        return None  # Price above slow EMA — trend not confirmed
    
    # RSI filter — don't enter at extremes
    if direction == "BUY" and rsi > rsi_ob:
        return None  # Overbought
    if direction == "SELL" and rsi < rsi_os:
        return None  # Oversold
    
    # Additional RSI filter — don't chase momentum
    if direction == "BUY" and rsi > 65:
        return None  # Too high, wait for pullback
    if direction == "SELL" and rsi < 35:
        return None  # Too low, wait for bounce
    
    # Volume confirmation (optional) — check if recent volume is above average
    if bars and len(bars) >= 20:
        recent_vol = sum(b.get("tick_volume", b.get("volume", 1)) for b in bars[:5]) / 5
        avg_vol = sum(b.get("tick_volume", b.get("volume", 1)) for b in bars[:20]) / 20
        if avg_vol > 0 and recent_vol < avg_vol * 0.5:
            return None  # Low volume — no conviction
    
    # Calculate SL
    sl_pts = calc_sl(symbol, atr, params)
    
    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "ADX_TREND",
            "ema_fast": ema_fast_val,
            "ema_slow": ema_slow_val,
            "adx": adx_val,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "rsi": rsi,
        },
    }
