"""
Estratégia KELTNER_CHANNEL — Canal de Keltner com bandas de ATR.

Sinal de entrada:
- BUY: preço toca banda inferior (mean reversion) OU rompe acima da superior (breakout)
- SELL: preço toca banda superior OU rompe abaixo da inferior

Lógica:
1. Calcula EMA central (geralmente 20 períodos)
2. Calcula bandas superior/inferior = EMA ± (ATR * multiplier)
3. Detecta toque/rompimento nas bandas
4. RSI confirma reversão ou momentum

Canal de Keltner:
- Central = EMA(close, ema_period)
- Upper = Central + ATR(atr_period) * atr_multiplier
- Lower = Central - ATR(atr_period) * atr_multiplier

Parâmetros:
  ema_period=20, atr_period=10, atr_multiplier=2.0
"""

STRATEGY_NAME = "KELTNER_CHANNEL"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada KELTNER_CHANNEL.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calculate_ema = utils["calculate_ema"]
    calculate_rsi = utils["calculate_rsi"]
    calc_sl = utils["calc_sl"]

    ema_period = params.get("ema_period", 20)
    atr_period = params.get("atr_period", 10)
    atr_multiplier = params.get("atr_multiplier", 2.0)

    min_bars = max(ema_period, atr_period) + 5
    if not bars or len(bars) < min_bars:
        return None

    if atr <= 0:
        return None

    # EMA central
    ema_val = calculate_ema(bars, ema_period)
    if ema_val == 0:
        return None

    # ATR para as bandas (usa o utils se disponível, senão o ATR recebido)
    calculate_atr = utils.get("calculate_atr")
    if calculate_atr:
        atr_val = calculate_atr(bars, atr_period)
    else:
        atr_val = atr

    if atr_val <= 0:
        return None

    # Bandas de Keltner
    upper_band = ema_val + atr_val * atr_multiplier
    lower_band = ema_val - atr_val * atr_multiplier

    # Bandas da barra anterior (pra detectar rompimento novo)
    ema_prev = calculate_ema(bars[1:], ema_period) if len(bars) > ema_period else ema_val
    atr_prev = calculate_atr(bars[1:], atr_period) if calculate_atr and len(bars) > atr_period else atr_val
    upper_prev = ema_prev + atr_prev * atr_multiplier
    lower_prev = ema_prev - atr_prev * atr_multiplier

    direction = None

    # Modo 1: Mean Reversion — preço toca a banda e volta
    rsi_period = params.get("rsi_period", 14)
    rsi_os = params.get("rsi_oversold", 35)
    rsi_ob = params.get("rsi_overbought", 65)
    rsi = calculate_rsi(bars, rsi_period)

    # BUY: preço tocou banda inferior (mean reversion)
    if price <= lower_band:
        # RSI deve indicar oversold
        if rsi is not None and rsi < rsi_os:
            direction = "BUY"

    # SELL: preço tocou banda superior (mean reversion)
    elif price >= upper_band:
        # RSI deve indicar overbought
        if rsi is not None and rsi > rsi_ob:
            direction = "SELL"

    # Modo 2: Breakout — preço rompe a banda com momentum
    if not direction:
        # BUY: rompimento acima da banda superior (breakout)
        if price > upper_band and bars[1]["close"] <= upper_prev:
            direction = "BUY"

        # SELL: rompimento abaixo da banda inferior (breakout)
        elif price < lower_band and bars[1]["close"] >= lower_prev:
            direction = "SELL"

    if not direction:
        return None

    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "KELTNER_CHANNEL",
            "ema": ema_val,
            "upper_band": upper_band,
            "lower_band": lower_band,
            "atr": atr_val,
            "rsi": rsi,
        },
    }
