"""
AGI4_WSP_123258 — Estratégia para WSP_M15.

Abordagem: mean-reversion com filtro de regime via ADX.

- Só opera quando ADX < adx_max (mercado sem tendência forte, regime de range)
- RSI sobrevendido/sobrecomprado em combinação com rejeição pela banda de Bollinger
- SL calculado via utils["calc_sl"] (Lei 3)
"""

STRATEGY_NAME = "AGI4_WSP_123258"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Avalia entrada para WSP no timeframe M15.

    Params (com defaults):
        rsi_period:     int = 14
        rsi_oversold:   float = 25.0
        rsi_overbought: float = 75.0
        bb_period:      int = 20
        bb_std:         float = 2.0
        bb_touch_pct:   float = 0.001  # % dentro da banda para considerar "toque"
        adx_period:     int = 14
        adx_max:        float = 22.0   # máximo ADX para permitir entrada
        min_bars:       int = 60
    """
    if not bars or len(bars) < params.get("min_bars", 60):
        return None

    rsi_period = int(params.get("rsi_period", 14))
    bb_period = int(params.get("bb_period", 20))
    adx_period = int(params.get("adx_period", 14))

    calculate_rsi = utils["calculate_rsi"]
    calculate_bollinger = utils["calculate_bollinger"]
    calculate_adx = utils["calculate_adx"]
    calc_sl = utils["calc_sl"]

    rsi = calculate_rsi(bars, rsi_period)
    adx_val = calculate_adx(bars, adx_period)
    bb = calculate_bollinger(bars, bb_period, float(params.get("bb_std", 2.0)))

    if rsi is None or adx_val is None or bb is None:
        return None

    upper, middle, lower = bb
    if upper is None or lower is None:
        return None

    adx_max = float(params.get("adx_max", 22.0))
    if adx_val >= adx_max:
        return None  # mercado tendencioso — fica fora

    rsi_oversold = float(params.get("rsi_oversold", 25.0))
    rsi_overbought = float(params.get("rsi_overbought", 75.0))
    bb_touch_pct = float(params.get("bb_touch_pct", 0.001))

    upper_threshold = upper * (1.0 - bb_touch_pct)
    lower_threshold = lower * (1.0 + bb_touch_pct)

    direction = None
    reason_parts = []

    # BUY: RSI sobrevendido + preço perto/abaixo da banda inferior
    if rsi <= rsi_oversold and price <= lower_threshold:
        direction = "BUY"
        reason_parts.append(f"RSI={rsi:.2f}<=oversold e preco {price:.2f} perto da banda inf")

    # SELL: RSI sobrecomprado + preço perto/acima da banda superior
    elif rsi >= rsi_overbought and price >= upper_threshold:
        direction = "SELL"
        reason_parts.append(f"RSI={rsi:.2f}>=overbought e preco {price:.2f} perto da banda sup")

    if direction is None:
        return None

    sl_pts = calc_sl(symbol, atr, params)
    sl_pts = int(sl_pts)
    if sl_pts <= 0:
        return None  # Lei 3 — SL positivo obrigatório

    info = {
        "rsi": round(rsi, 2),
        "adx": round(adx_val, 2),
        "bb_upper": round(upper, 2),
        "bb_middle": round(middle, 2),
        "bb_lower": round(lower, 2),
        "price": round(price, 2),
        "atr": round(atr, 2),
        "regime": "range",
        "reason": "; ".join(reason_parts),
    }

    return {"direction": direction, "sl_pts": sl_pts, "info": info}
