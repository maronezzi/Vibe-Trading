"""
Estratégia TRIPLE_EMA — Sistema de 3 EMAs (rápida, média, lenta).
Confirma tendência forte quando as 3 EMAs estão alinhadas.

Sinal de entrada:
- BUY: EMA_fast > EMA_mid > EMA_slow (alinhamento de alta)
- SELL: EMA_fast < EMA_mid < EMA_slow (alinhamento de baixa)

Parâmetros (via vt_config.json):
  ema_fast, ema_mid, ema_slow, adx_period, adx_threshold
  sl_atr_mult, trail_activate, trail_distance
"""

STRATEGY_NAME = "TRIPLE_EMA"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada TRIPLE_EMA.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calc_sl = utils["calc_sl"]
    calculate_ema = utils["calculate_ema"]
    calculate_adx = utils["calculate_adx"]

    ema_fast_period = params.get("ema_fast", 8)
    ema_mid_period = params.get("ema_mid", 21)
    ema_slow_period = params.get("ema_slow", 55)
    adx_period = params.get("adx_period", 14)
    adx_threshold = params.get("adx_threshold", 20)

    min_bars = max(ema_slow_period, adx_period * 2) + 5
    if not bars or len(bars) < min_bars:
        return None

    ema_fast = calculate_ema(bars, ema_fast_period)
    ema_mid = calculate_ema(bars, ema_mid_period)
    ema_slow = calculate_ema(bars, ema_slow_period)

    if ema_fast == 0 or ema_mid == 0 or ema_slow == 0:
        return None

    # ADX filter — need minimum trend strength
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    if adx_val < adx_threshold:
        return None

    direction = None

    # Aligned bullish: fast > mid > slow
    if ema_fast > ema_mid > ema_slow:
        direction = "BUY"
    # Aligned bearish: fast < mid < slow
    elif ema_fast < ema_mid < ema_slow:
        direction = "SELL"

    if not direction:
        return None

    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "TRIPLE_EMA",
            "ema_fast": round(ema_fast, 2),
            "ema_mid": round(ema_mid, 2),
            "ema_slow": round(ema_slow, 2),
            "adx": round(adx_val, 2),
        },
    }
