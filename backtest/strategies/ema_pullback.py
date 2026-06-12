"""
Estratégia EMA_PULLBACK — Trend-following com pullback para WIN.
Compra em pullbacks na tendência, vendendo em pullbacks na baixa.

Diferente do BOLLINGER (reversão), esta estratégia SEGUE a tendência:
1. Identifica tendência via EMA(9,21) + ADX
2. Espera pullback: preço volta para perto da EMA lenta
3. Confirma com RSI e DI direction
4. SL baseado em ATR

Parâmetros (via vt_config.json → win):
  ema_fast, ema_slow, adx_period, adx_threshold
  rsi_period, rsi_overbought, rsi_oversold
  pullback_pct: % mínimo de pullback para entrar (default 0.15%)
  sl_atr_mult, trail_activate, trail_distance
"""

STRATEGY_NAME = "EMA_PULLBACK"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada EMA_PULLBACK.
    
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
    adx_threshold = params.get("adx_threshold", 20)
    rsi_period = params.get("rsi_period", 14)
    rsi_ob = params.get("rsi_overbought", 70)
    rsi_os = params.get("rsi_oversold", 30)
    pullback_pct = params.get("pullback_pct", 0.15) / 100.0  # convert to decimal
    
    if not bars or len(bars) < max(ema_slow_period, adx_period) + 5:
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
    
    # Determine trend direction
    is_uptrend = ema_fast_val > ema_slow_val and plus_di > minus_di
    is_downtrend = ema_fast_val < ema_slow_val and minus_di > plus_di
    
    if not is_uptrend and not is_downtrend:
        return None
    
    # Check for pullback
    # For uptrend: price pulled back toward EMA slow (but still above)
    # For downtrend: price pulled back toward EMA slow (but still below)
    
    # Get recent high/low for pullback detection
    recent_prices = [float(b["close"]) for b in bars[:5]]
    
    if is_uptrend:
        # Pullback: recent high was higher, now price is lower (pulling back)
        recent_high = max(float(b["high"]) for b in bars[:5])
        pullback_from_high = (recent_high - price) / recent_high
        
        # Price should be near or above EMA slow (not too far below)
        ema_distance = (price - ema_slow_val) / ema_slow_val
        
        # Pullback condition: price pulled back at least pullback_pct from recent high
        # AND still above EMA slow (trend intact)
        if pullback_from_high < pullback_pct:
            return None  # No pullback yet
        
        if ema_distance < -pullback_pct:
            return None  # Price too far below EMA — trend may be broken
        
        # RSI filter: don't buy if already overbought (wait for dip)
        if rsi > rsi_ob:
            return None
        
        # Extra: RSI should show some pullback (not at extreme highs)
        if rsi > 65:
            return None  # Too high, wait for better entry
        
        direction = "BUY"
        
    else:  # is_downtrend
        # Pullback: recent low was lower, now price is higher (pulling back up)
        recent_low = min(float(b["low"]) for b in bars[:5])
        pullback_from_low = (price - recent_low) / recent_low
        
        # Price should be near or below EMA slow
        ema_distance = (ema_slow_val - price) / ema_slow_val
        
        if pullback_from_low < pullback_pct:
            return None
        
        if ema_distance < -pullback_pct:
            return None
        
        # RSI filter: don't sell if already oversold
        if rsi < rsi_os:
            return None
        
        # Extra: RSI should show some pullback up
        if rsi < 35:
            return None
        
        direction = "SELL"
    
    # Calculate SL
    sl_pts = calc_sl(symbol, atr, params)
    
    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "EMA_PULLBACK",
            "ema_fast": ema_fast_val,
            "ema_slow": ema_slow_val,
            "adx": adx_val,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "rsi": rsi,
            "pullback_pct": pullback_pct * 100,
        },
    }
