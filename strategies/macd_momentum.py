"""
Estratégia MACD_MOMENTUM v2 — Versão melhorada para M15.

Diferente da v1 (que requer crossover exato), esta versão:
1. Aceita crossover E momentum crescente
2. Usa filtro de tendência mais suave
3. Adiciona confirmação de preço (close na direção da tendência)
4. Mantém proteções: ADX, RSI, volume

Lógica:
- BUY: EMA fast > slow + ADX > 15 + (MACD hist cruza pra cima OU hist > 0 e crescente) + RSI < 75
- SELL: EMA fast < slow + ADX > 15 + (MACD hist cruza pra baixo OU hist < 0 e decrescente) + RSI > 25

Parâmetros (via vt_config.json → win/wdo):
  ema_fast, ema_slow, adx_period, adx_threshold
  rsi_period, rsi_overbought, rsi_oversold
  macd_fast, macd_slow, macd_signal
  sl_atr_mult, trail_activate, trail_distance
"""

STRATEGY_NAME = "MACD_MOMENTUM"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada MACD_MOMENTUM v2.
    
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
    adx_threshold = params.get("adx_threshold", 15)  # Lowered from 20
    rsi_period = params.get("rsi_period", 14)
    rsi_ob = params.get("rsi_overbought", 75)
    rsi_os = params.get("rsi_oversold", 25)
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
    
    if len(histogram) < 3:
        return None
    
    cur_hist = histogram[-1]
    prev_hist = histogram[-2]
    prev2_hist = histogram[-3]
    
    # MACD signals — more lenient than v1
    macd_cross_up = prev_hist <= 0 and cur_hist > 0
    macd_cross_down = prev_hist >= 0 and cur_hist < 0
    
    # Also accept momentum increasing in right direction
    macd_momentum_up = cur_hist > 0 and cur_hist > prev_hist and prev_hist > prev2_hist
    macd_momentum_down = cur_hist < 0 and cur_hist < prev_hist and prev_hist < prev2_hist
    
    # Need minimum trend strength (lowered threshold)
    if adx_val < adx_threshold:
        return None
    
    # Determine trend direction from EMA
    is_uptrend = ema_fast_val > ema_slow_val
    is_downtrend = ema_fast_val < ema_slow_val
    
    if not is_uptrend and not is_downtrend:
        return None
    
    # DI confirmation
    if is_uptrend and plus_di < minus_di:
        return None
    if is_downtrend and minus_di < plus_di:
        return None
    
    direction = None
    
    # BUY: uptrend + MACD momentum turning up
    if is_uptrend and (macd_cross_up or macd_momentum_up):
        if rsi > rsi_ob:
            return None
        direction = "BUY"
    
    # SELL: downtrend + MACD momentum turning down
    elif is_downtrend and (macd_cross_down or macd_momentum_down):
        if rsi < rsi_os:
            return None
        direction = "SELL"
    
    if not direction:
        return None
    
    # Volume confirmation
    if bars and len(bars) >= 20:
        recent_vol = sum(float(b.get("volume", 1) or 1) for b in bars[:5]) / 5
        avg_vol = sum(float(b.get("volume", 1) or 1) for b in bars[:20]) / 20
        if avg_vol > 0 and recent_vol < avg_vol * 0.2:
            return None
    
    # Price position relative to EMA slow
    if direction == "BUY" and price < ema_slow_val * 0.995:
        return None
    if direction == "SELL" and price > ema_slow_val * 1.005:
        return None
    
    # Calculate SL
    sl_pts = calc_sl(symbol, atr, params)
    
    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "MACD_MOMENTUM",
            "ema_fast": ema_fast_val,
            "ema_slow": ema_slow_val,
            "adx": adx_val,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "rsi": rsi,
            "macd_hist": cur_hist,
        },
    }
