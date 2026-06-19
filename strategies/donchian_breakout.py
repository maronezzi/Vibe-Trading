"""
Estratégia DONCHIAN_BREAKOUT — Breakout de canal de Donchian (turtle trading).

Sinal de entrada:
- BUY: preço rompe acima do upper channel (highest high de N períodos)
- SELL: preço rompe abaixo do lower channel (lowest low de N períodos)

Lógica:
1. Calcula o canal de Donchian (highest high / lowest low de N períodos)
2. Detecta rompimento do preço além do canal
3. Filtro opcional: canal de saída mais curto pra confirmar momentum

Canal de Donchian:
- Upper = highest high dos últimos N períodos
- Lower = lowest low dos últimos N períodos
- Mid = (upper + lower) / 2

Parâmetros:
  period=20, exit_period=10
"""

STRATEGY_NAME = "DONCHIAN_BREAKOUT"


def _donchian_channel(bars, period):
    """
    Calcula o canal de Donchian (upper, lower, mid).

    bars: newest-first → usa bars[:period]
    Retorna (upper, lower, mid) ou (0, 0, 0) se insuficiente.
    """
    if len(bars) < period:
        return 0.0, 0.0, 0.0

    segment = bars[:period]
    upper = max(b["high"] for b in segment)
    lower = min(b["low"] for b in segment)
    mid = (upper + lower) / 2.0

    return upper, lower, mid


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada DONCHIAN_BREAKOUT.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calc_sl = utils["calc_sl"]

    period = params.get("period", 20)
    exit_period = params.get("exit_period", 10)

    min_bars = max(period, exit_period) + 5
    if not bars or len(bars) < min_bars:
        return None

    if atr <= 0:
        return None

    # Canal principal (N períodos)
    upper, lower, mid = _donchian_channel(bars, period)
    if upper == 0 or lower == 0:
        return None

    # Canal anterior (pra confirmar que o breakout é novo)
    upper_prev, lower_prev, _ = _donchian_channel(bars[1:], period)

    # Canal de saída (mais curto, pra confirmar momentum)
    exit_upper, exit_lower, _ = _donchian_channel(bars, exit_period)

    direction = None

    # BUY: preço rompe acima do upper channel (novo breakout)
    if price >= upper and (upper_prev == 0 or bars[1]["close"] < upper_prev):
        direction = "BUY"

    # SELL: preço rompe abaixo do lower channel (novo breakout)
    elif price <= lower and (lower_prev == 0 or bars[1]["close"] > lower_prev):
        direction = "SELL"

    if not direction:
        return None

    # Filtro: confirmar com canal de saída (evitar falsos breakouts)
    if direction == "BUY" and exit_upper > 0 and price < exit_upper * 0.998:
        return None  # Rompimento fraco
    if direction == "SELL" and exit_lower > 0 and price > exit_lower * 1.002:
        return None  # Rompimento fraco

    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "DONCHIAN_BREAKOUT",
            "upper": upper,
            "lower": lower,
            "mid": mid,
            "exit_upper": exit_upper,
            "exit_lower": exit_lower,
            "atr": atr,
        },
    }
