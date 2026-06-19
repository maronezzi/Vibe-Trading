"""
Estratégia PIVOT_POINTS — Pivot Points clássicos (S1/S2/S3, R1/R2/R3).
Suporte e resistência derivados do fechamento anterior.

Sinal de entrada:
- BUY: preço toca S1 ou S2 (suporte) e reverte com RSI oversold
- SELL: preço toca R1 ou R2 (resistência) e reverte com RSI overbought

Parâmetros (via vt_config.json):
  rsi_period, rsi_overbought, rsi_oversold, touch_pct
  sl_atr_mult, trail_activate, trail_distance
"""

STRATEGY_NAME = "PIVOT_POINTS"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada PIVOT_POINTS.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calc_sl = utils["calc_sl"]
    calculate_rsi = utils["calculate_rsi"]

    rsi_period = params.get("rsi_period", 14)
    rsi_ob = params.get("rsi_overbought", 70)
    rsi_os = params.get("rsi_oversold", 30)
    touch_pct = params.get("touch_pct", 0.002)  # 0.2% proximity to level

    if not bars or len(bars) < 2:
        return None

    # Previous bar OHLC for pivot calculation
    prev = bars[1]
    prev_high = prev.get("high", 0)
    prev_low = prev.get("low", 0)
    prev_close = prev.get("close", 0)

    if prev_high == 0 or prev_low == 0 or prev_close == 0:
        return None

    # Classic Pivot Points
    pivot = (prev_high + prev_low + prev_close) / 3.0
    r1 = 2 * pivot - prev_low
    s1 = 2 * pivot - prev_high
    r2 = pivot + (prev_high - prev_low)
    s2 = pivot - (prev_high - prev_low)
    r3 = prev_high + 2 * (pivot - prev_low)
    s3 = prev_low - 2 * (prev_high - pivot)

    rsi = calculate_rsi(bars, rsi_period)
    if rsi is None or rsi == 0:
        return None

    direction = None

    # BUY: price near support levels
    for level in [s1, s2, s3]:
        if level > 0 and abs(price - level) / level < touch_pct:
            if rsi < rsi_os + 10:  # RSI near oversold
                direction = "BUY"
                break

    # SELL: price near resistance levels
    if not direction:
        for level in [r1, r2, r3]:
            if level > 0 and abs(price - level) / level < touch_pct:
                if rsi > rsi_ob - 10:  # RSI near overbought
                    direction = "SELL"
                    break

    if not direction:
        return None

    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "PIVOT_POINTS",
            "pivot": round(pivot, 2),
            "r1": round(r1, 2), "r2": round(r2, 2), "r3": round(r3, 2),
            "s1": round(s1, 2), "s2": round(s2, 2), "s3": round(s3, 2),
            "rsi": round(rsi, 2),
        },
    }
