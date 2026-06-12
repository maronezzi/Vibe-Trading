"""
Estratégia WIN_REVERSION — Mean-reversion para WIN (mercado volátil).

WIN (Mini Index) é mais volátil que WDO. As estratégias de trend-following
perdem porque entram tarde na tendência e são stopadas.

Esta estratégia usa reversão à média com filtros mais rigorosos:
1. Bollinger Bands (2.5 std) — espera extremo
2. RSI extremo — confirma oversold/overbought
3. Volume spike — confirma reversão
4. Preço volta pra direção da EMA — timing de entrada

Lógica:
- BUY: price <= BB_lower(20,2.5) + RSI < 30 + volume > 1.5x avg
- SELL: price >= BB_upper(20,2.5) + RSI > 70 + volume > 1.5x avg

Parâmetros:
  bb_period, bb_std, rsi_period, rsi_overbought, rsi_oversold
  sl_atr_mult, trail_activate, trail_distance
"""

STRATEGY_NAME = "WIN_REVERSION"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada WIN_REVERSION.
    
    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calculate_bollinger = utils["calculate_bollinger"]
    calculate_rsi = utils["calculate_rsi"]
    calculate_ema = utils["calculate_ema"]
    calc_sl = utils["calc_sl"]
    
    # Parameters
    bb_period = params.get("bb_period", 20)
    bb_std = params.get("bb_std", 2.5)  # Wider bands for WIN volatility
    rsi_period = params.get("rsi_period", 14)
    rsi_ob = params.get("rsi_overbought", 70)
    rsi_os = params.get("rsi_oversold", 30)
    
    if not bars or len(bars) < max(bb_period, rsi_period) + 5:
        return None
    
    # Bollinger Bands
    bb_upper, bb_mid, bb_lower = calculate_bollinger(bars, bb_period, bb_std)
    if bb_upper == 0 or bb_lower == 0:
        return None
    
    # RSI
    rsi = calculate_rsi(bars, rsi_period)
    
    # Volume confirmation
    if bars and len(bars) >= 20:
        recent_vol = sum(float(b.get("volume", 1) or 1) for b in bars[:3]) / 3
        avg_vol = sum(float(b.get("volume", 1) or 1) for b in bars[:20]) / 20
        vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1
    else:
        vol_ratio = 1.0
    
    # EMA for trend bias
    ema_val = calculate_ema(bars, params.get("ema_slow", 21))
    
    direction = None
    
    # BUY: price at lower BB + RSI oversold + volume spike
    if price <= bb_lower and rsi < rsi_os and vol_ratio > 1.2:
        direction = "BUY"
    
    # SELL: price at upper BB + RSI overbought + volume spike
    elif price >= bb_upper and rsi > rsi_ob and vol_ratio > 1.2:
        direction = "SELL"
    
    if not direction:
        return None
    
    # Extra safety: don't fight strong trends
    # If EMA is very far from price, trend might be too strong for reversion
    if ema_val > 0:
        ema_dist = abs(price - ema_val) / ema_val
        if ema_dist > 0.03:  # More than 3% from EMA — trend too strong
            return None
    
    # Calculate SL
    sl_pts = calc_sl(symbol, atr, params)
    
    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "WIN_REVERSION",
            "bb_upper": bb_upper,
            "bb_mid": bb_mid,
            "bb_lower": bb_lower,
            "rsi": rsi,
            "vol_ratio": vol_ratio,
            "ema": ema_val,
        },
    }
