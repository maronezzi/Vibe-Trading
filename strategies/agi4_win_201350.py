"""
AGI4_WIN_201350 - Estratégia EMA Crossover com Filtro ADX para WIN M5
"""

STRATEGY_NAME = "AGI4_WIN_201350"

TUNABLE_PARAMS = {
    "ema_fast": (int, 5, 15),
    "ema_slow": (int, 15, 30),
    "adx_min": (float, 18.0, 30.0),
    "adx_period": (int, 10, 20),
    "rsi_period": (int, 10, 20),
}


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    calculate_ema = utils["calculate_ema"]
    calculate_adx = utils["calculate_adx"]
    calculate_rsi = utils["calculate_rsi"]
    calc_sl = utils["calc_sl"]

    ema_fast = params.get("ema_fast", 9)
    ema_slow = params.get("ema_slow", 21)
    adx_min = params.get("adx_min", 20.0)
    adx_period = params.get("adx_period", 14)
    rsi_period = params.get("rsi_period", 14)

    min_bars = max(ema_slow * 2, adx_period * 3, rsi_period * 2) + 10
    if not bars or len(bars) < min_bars:
        return None
    if atr <= 0 or price <= 0:
        return None

    ema_fast_val = calculate_ema(bars, ema_fast)
    ema_slow_val = calculate_ema(bars, ema_slow)
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    rsi = calculate_rsi(bars, rsi_period)

    if ema_fast_val == 0 or ema_slow_val == 0 or adx_val == 0:
        return None

    # Gate de regime: ADX > mínimo (tendência saudável)
    if adx_val < adx_min:
        return None

    # Filtro de polaridade via DI
    direction = None
    if ema_fast_val > ema_slow_val and plus_di > minus_di:
        direction = "BUY"
    elif ema_fast_val < ema_slow_val and minus_di > plus_di:
        direction = "SELL"

    if direction is None:
        return None

    # Gate adicional: RSI não extremo para evitar exaustão
    if direction == "BUY" and rsi > 75:
        return None
    if direction == "SELL" and rsi < 25:
        return None

    sl_pts = calc_sl(symbol, atr, params)
    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {"adx": adx_val, "plus_di": plus_di, "minus_di": minus_di, "rsi": rsi}
    }