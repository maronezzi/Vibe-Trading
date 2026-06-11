"""
Estratégia EMA_CROSSOVER — Trend-following com EMA + ADX + RSI.
Para WIN (mercado em tendência).

Parâmetros (via vt_config.json → win):
  ema_fast, ema_slow, adx_period, adx_threshold
  rsi_period, rsi_overbought, rsi_oversold
  sl_atr_mult, trail_activate, trail_distance
"""

STRATEGY_NAME = "EMA_CROSSOVER"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada EMA Crossover + ADX.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calculate_ema = utils["calculate_ema"]
    calculate_rsi = utils["calculate_rsi"]
    calculate_adx = utils["calculate_adx"]
    calc_sl = utils["calc_sl"]

    ema_fast_period = params.get("ema_fast", 12)
    ema_slow_period = params.get("ema_slow", 21)
    adx_period = params.get("adx_period", 14)
    adx_threshold = params.get("adx_threshold", 20)
    rsi_period = params.get("rsi_period", 14)
    rsi_ob = params.get("rsi_overbought", 70)
    rsi_os = params.get("rsi_oversold", 30)

    ema_fast_val = calculate_ema(bars, ema_fast_period)
    ema_slow_val = calculate_ema(bars, ema_slow_period)
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    rsi = calculate_rsi(bars, rsi_period)

    if ema_fast_val == 0 or ema_slow_val == 0 or adx_val == 0:
        return None

    # ADX filter — precisa de tendência forte
    if adx_val < adx_threshold:
        return None

    # Crossover detection
    prev_fast = calculate_ema(bars[1:], ema_fast_period) if len(bars) > ema_fast_period else ema_fast_val
    prev_slow = calculate_ema(bars[1:], ema_slow_period) if len(bars) > ema_slow_period else ema_slow_val

    direction = None
    if prev_fast <= prev_slow and ema_fast_val > ema_slow_val:
        direction = "BUY"
    elif prev_fast >= prev_slow and ema_fast_val < ema_slow_val:
        direction = "SELL"

    if not direction:
        return None

    # RSI filter
    if direction == "BUY" and rsi > rsi_ob:
        return None
    if direction == "SELL" and rsi < rsi_os:
        return None

    # DI filter — confirma direção da tendência
    if direction == "BUY" and plus_di < minus_di:
        return None
    if direction == "SELL" and minus_di < plus_di:
        return None

    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "EMA_CROSSOVER",
            "ema_fast": ema_fast_val,
            "ema_slow": ema_slow_val,
            "adx": adx_val,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "rsi": rsi,
        },
    }
