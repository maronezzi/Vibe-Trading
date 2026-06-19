"""
Estratégia FIBONACCI_RETRACEMENT — Retração de Fibonacci.
Entra em níveis de retração (38.2%, 50%, 61.8%) de um swing recente.

Sinal de entrada:
- BUY: preço toca nível de retração em uptrend (swing low → high)
- SELL: preço toca nível de retração em downtrend (swing high → low)

Parâmetros (via vt_config.json):
  lookback, fib_level_1, fib_level_2, fib_level_3, touch_pct
  ema_period, rsi_period, rsi_overbought, rsi_oversold
  sl_atr_mult, trail_activate, trail_distance
"""

STRATEGY_NAME = "FIBONACCI_RETRACEMENT"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada FIBONACCI_RETRACEMENT.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calc_sl = utils["calc_sl"]
    calculate_ema = utils["calculate_ema"]
    calculate_rsi = utils["calculate_rsi"]

    lookback = params.get("lookback", 50)
    fib_levels = [
        params.get("fib_level_1", 0.382),
        params.get("fib_level_2", 0.500),
        params.get("fib_level_3", 0.618),
    ]
    touch_pct = params.get("touch_pct", 0.003)
    ema_period = params.get("ema_period", 50)
    rsi_period = params.get("rsi_period", 14)
    rsi_ob = params.get("rsi_overbought", 70)
    rsi_os = params.get("rsi_oversold", 30)

    min_bars = max(lookback, ema_period) + 5
    if not bars or len(bars) < min_bars:
        return None

    # Find swing high and low in lookback period
    recent = bars[:lookback]
    swing_high = max(b.get("high", 0) for b in recent)
    swing_low = min(b.get("low", float("inf")) for b in recent)

    if swing_high == 0 or swing_low == float("inf") or swing_high <= swing_low:
        return None

    swing_range = swing_high - swing_low

    # Trend direction via EMA
    ema_val = calculate_ema(bars, ema_period)
    if ema_val == 0:
        return None

    rsi = calculate_rsi(bars, rsi_period)
    if rsi is None or rsi == 0:
        return None

    direction = None

    if price > ema_val:
        # Uptrend — look for BUY at retrace levels (high → low retrace)
        for fib in fib_levels:
            retrace_level = swing_high - swing_range * fib
            if abs(price - retrace_level) / retrace_level < touch_pct and rsi < rsi_os + 15:
                direction = "BUY"
                break
    else:
        # Downtrend — look for SELL at retrace levels (low → high retrace)
        for fib in fib_levels:
            retrace_level = swing_low + swing_range * fib
            if abs(price - retrace_level) / retrace_level < touch_pct and rsi > rsi_ob - 15:
                direction = "SELL"
                break

    if not direction:
        return None

    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "FIBONACCI_RETRACEMENT",
            "swing_high": round(swing_high, 2),
            "swing_low": round(swing_low, 2),
            "ema": round(ema_val, 2),
            "rsi": round(rsi, 2),
        },
    }
