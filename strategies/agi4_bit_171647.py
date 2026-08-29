"""
AGI4_BIT_171647 - Estratégia de tendência ADX adaptada ao BIT_M5
"""

STRATEGY_NAME = "AGI4_BIT_171647"

TUNABLE_PARAMS = {
    "ema_fast": (int, 5, 15),
    "ema_slow": (int, 15, 30),
    "adx_period": (int, 10, 20),
    "adx_min": (float, 20.0, 30.0),
    "atr_pct_max": (float, 0.01, 0.03),
    "slope_min": (float, 0.005, 0.02)
}

def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    calculate_ema = utils["calculate_ema"]
    calculate_adx = utils["calculate_adx"]
    calculate_atr = utils["calculate_atr"]
    calculate_linreg_slope = utils["calculate_linreg_slope"]
    calc_sl = utils["calc_sl"]

    ema_fast_p = params.get("ema_fast", 9)
    ema_slow_p = params.get("ema_slow", 21)
    adx_period = params.get("adx_period", 14)
    adx_min = params.get("adx_min", 25.0)
    adx_max = params.get("adx_max", 45.0)
    atr_pct_max = params.get("atr_pct_max", 0.02)
    slope_period = params.get("slope_period", 20)
    slope_min = params.get("slope_min", 0.01)

    min_bars = max(ema_slow_p * 2, adx_period * 2, slope_period) + 5
    if not bars or len(bars) < min_bars:
        return None
    if atr <= 0 or price <= 0:
        return None

    ema_fast = calculate_ema(bars, ema_fast_p)
    ema_slow = calculate_ema(bars, ema_slow_p)
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    atr_val = calculate_atr(bars, adx_period)
    slope = calculate_linreg_slope(bars, slope_period)

    if ema_slow == 0 or adx_val == 0 or atr_val == 0:
        return None

    atr_pct = atr_val / price
    if atr_pct > atr_pct_max:
        return None

    if adx_val < adx_min or adx_val > adx_max:
        return None

    if slope < slope_min and slope > -slope_min:
        return None

    direction = None
    info = {}

    if ema_fast > ema_slow and plus_di > minus_di and slope > slope_min:
        direction = "BUY"
        info = {
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "adx": adx_val,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "atr_pct": atr_pct,
            "slope": slope
        }
    elif ema_fast < ema_slow and minus_di > plus_di and slope < -slope_min:
        direction = "SELL"
        info = {
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "adx": adx_val,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "atr_pct": atr_pct,
            "slope": slope
        }

    if direction is None:
        return None

    sl_pts = calc_sl(symbol, atr, params)
    if sl_pts <= 0:
        return None

    return {"direction": direction, "sl_pts": sl_pts, "info": info}
