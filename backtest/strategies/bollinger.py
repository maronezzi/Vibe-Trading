"""
Estratégia BOLLINGER — Reversão à média com Bollinger Bands.
Para mercados choppy (range-bound).

Parâmetros (via vt_config.json):
  bb_period, bb_std, rsi_period, rsi_overbought, rsi_oversold
  sl_atr_mult, trail_activate, trail_distance
"""

STRATEGY_NAME = "BOLLINGER"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada Bollinger Bands.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calculate_bollinger = utils["calculate_bollinger"]
    calculate_rsi = utils["calculate_rsi"]
    calc_sl = utils["calc_sl"]

    bb_period = params.get("bb_period", 20)
    bb_std = params.get("bb_std", 2.0)
    rsi_period = params.get("rsi_period", 14)
    rsi_ob = params.get("rsi_overbought", 70)
    rsi_os = params.get("rsi_oversold", 30)

    bb_upper, bb_mid, bb_lower = calculate_bollinger(bars, bb_period, bb_std)

    if bb_upper == 0 or bb_lower == 0:
        return None

    rsi = calculate_rsi(bars, rsi_period)

    direction = None
    if price <= bb_lower:
        direction = "BUY"  # Preço tocou banda inferior → compra (reversão)
    elif price >= bb_upper:
        direction = "SELL"  # Preço tocou banda superior → vende (reversão)

    if not direction:
        return None

    # RSI confirmation
    if direction == "BUY" and rsi > rsi_os:
        return None  # RSI não tá oversold o suficiente
    if direction == "SELL" and rsi < rsi_ob:
        return None  # RSI não tá overbought o suficiente

    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "BOLLINGER",
            "bb_upper": bb_upper,
            "bb_mid": bb_mid,
            "bb_lower": bb_lower,
            "rsi": rsi,
            "bb_mid_val": bb_mid,  # para trailing tighten
        },
    }
