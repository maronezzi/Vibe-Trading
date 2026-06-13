"""
Estratégia SMART_EMA — Híbrida adaptativa por timeframe.

M5 (curto prazo): Comportamento EMA_PULLBACK — conservador, espera pullback.
M15 (médio prazo): Comportamento STRONG_TREND — agressivo, segue tendência forte.

Lógica:
- Identifica tendência via EMA(9,21) + ADX + DI direction
- M5: Espera pullback + RSI confirmação (conservador)
- M15: Entra direto na tendência forte (agressivo) — ignora RSI se ADX > 40
- SL: 1.0x ATR (M5) ou 1.5x ATR (M15)
- Trailing: ativa 1.5x ATR, distância 0.5x ATR

Parâmetros (via vt_config.json → win/wdo):
  ema_fast, ema_slow, adx_period, adx_threshold
  rsi_period, rsi_overbought, rsi_oversold
  sl_atr_mult, trail_activate, trail_distance
  pullback_pct
"""

STRATEGY_NAME = "SMART_EMA"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada SMART_EMA.
    Comportamento varia por timeframe:
      M5 → EMA_PULLBACK (conservador)
      M15 → STRONG_TREND (agressivo)
    
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
    rsi_ob = params.get("rsi_overbought", 80)
    rsi_os = params.get("rsi_oversold", 20)
    pullback_pct = params.get("pullback_pct", 0.15) / 100.0
    
    if not bars or len(bars) < max(ema_slow_period, adx_period * 2) + 5:
        return None
    
    # Calculate indicators
    ema_fast_val = calculate_ema(bars, ema_fast_period)
    ema_slow_val = calculate_ema(bars, ema_slow_period)
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    rsi = calculate_rsi(bars, rsi_period)
    
    if ema_fast_val == 0 or ema_slow_val == 0 or adx_val == 0:
        return None
    
    # Determine trend direction
    is_uptrend = ema_fast_val > ema_slow_val
    is_downtrend = ema_fast_val < ema_slow_val
    
    if not is_uptrend and not is_downtrend:
        return None
    
    # DI confirmation
    if is_uptrend and plus_di < minus_di:
        return None
    if is_downtrend and minus_di < plus_di:
        return None
    
    is_m15 = tf.upper() in ("M15", "M30", "H1")
    
    if is_m15:
        # ===== M15: STRONG_TREND mode — agressivo =====
        # Need minimum trend strength
        if adx_val < adx_threshold:
            return None
        
        # Price must be on the right side of EMA slow
        if is_uptrend and price < ema_slow_val * 0.998:
            return None
        if is_downtrend and price > ema_slow_val * 1.002:
            return None
        
        # RSI filter — ONLY filter extremes in moderate trends
        # In strong trends (ADX >= 40), let it ride
        if adx_val < 40:
            if is_uptrend and rsi > 80:
                return None
            if is_downtrend and rsi < 20:
                return None
        
        # Volume confirmation
        if bars and len(bars) >= 20:
            recent_vol = sum(float(b.get("volume", 1) or 1) for b in bars[:5]) / 5
            avg_vol = sum(float(b.get("volume", 1) or 1) for b in bars[:20]) / 20
            if avg_vol > 0 and recent_vol < avg_vol * 0.3:
                return None
        
        direction = "BUY" if is_uptrend else "SELL"
        
    else:
        # ===== M5: EMA_PULLBACK mode — conservador =====
        # Need minimum trend strength
        if adx_val < adx_threshold:
            return None
        
        if is_uptrend:
            # Pullback: recent high was higher, now price is lower
            recent_high = max(float(b["high"]) for b in bars[:5])
            pullback_from_high = (recent_high - price) / recent_high
            ema_distance = (price - ema_slow_val) / ema_slow_val
            
            if pullback_from_high < pullback_pct:
                return None
            if ema_distance < -pullback_pct:
                return None
            
            # RSI filter
            if rsi > rsi_ob:
                return None
            if rsi > 65:
                return None
            
            direction = "BUY"
        
        else:  # downtrend
            recent_low = min(float(b["low"]) for b in bars[:5])
            pullback_from_low = (price - recent_low) / recent_low
            ema_distance = (ema_slow_val - price) / ema_slow_val
            
            if pullback_from_low < pullback_pct:
                return None
            if ema_distance < -pullback_pct:
                return None
            
            if rsi < rsi_os:
                return None
            if rsi < 35:
                return None
            
            direction = "SELL"
    
    # Calculate SL
    sl_pts = calc_sl(symbol, atr, params)
    
    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "SMART_EMA",
            "mode": "STRONG" if is_m15 else "PULLBACK",
            "ema_fast": ema_fast_val,
            "ema_slow": ema_slow_val,
            "adx": adx_val,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "rsi": rsi,
        },
    }
