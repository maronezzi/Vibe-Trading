"""
Estratégia RSI_REVERSION — Mean-reversion pura baseada em RSI extremo.

Sinal de entrada:
- BUY: RSI < rsi_oversold (preço sobrevendido → esperada reversão pra cima)
- SELL: RSI > rsi_overbought (preço sobrecomprado → esperada reversão pra baixo)

Diferencial vs WIN_REVERSION:
- Sem filtro de Bollinger Bands (entrada mais cedo)
- Sem filtro de volume (funciona em qualquer liquidez)
- Filtro de ATR mínimo (evita entrar em mercado sem movimento)
- Filtro opcional de distância da EMA (não lutar contra trend forte)

Parâmetros:
  rsi_period, rsi_overbought, rsi_oversold
  ema_period (filtro de tendência, 0 = desativado)
  max_ema_distance_pct (distância máxima da EMA, padrão 5%)
  sl_atr_mult (do params)
"""

STRATEGY_NAME = "RSI_REVERSION"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada RSI_REVERSION.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calculate_rsi = utils["calculate_rsi"]
    calculate_ema = utils["calculate_ema"]
    calc_sl = utils["calc_sl"]

    # Parameters
    rsi_period = params.get("rsi_period", 14)
    rsi_ob = params.get("rsi_overbought", 70)
    rsi_os = params.get("rsi_oversold", 30)
    ema_period = params.get("ema_period", 0)  # 0 = sem filtro de trend
    max_ema_dist = params.get("max_ema_distance_pct", 5.0) / 100.0

    if not bars or len(bars) < rsi_period + 5:
        return None

    # ATR mínimo — não entrar se mercado está morto
    if atr <= 0:
        return None

    # RSI
    rsi = calculate_rsi(bars, rsi_period)

    if rsi is None or rsi == 0:
        return None

    direction = None

    # BUY: RSI oversold
    if rsi < rsi_os:
        direction = "BUY"

    # SELL: RSI overbought
    elif rsi > rsi_ob:
        direction = "SELL"

    if not direction:
        return None

    # Filtro de tendência opcional: não lutar contra trend forte
    if ema_period > 0 and len(bars) >= ema_period + 5:
        ema_val = calculate_ema(bars, ema_period)
        if ema_val and ema_val > 0:
            ema_dist = abs(price - ema_val) / ema_val
            if ema_dist > max_ema_dist:
                return None  # Preço muito longe da EMA — trend forte

    # Calculate SL
    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "RSI_REVERSION",
            "rsi": rsi,
            "atr": atr,
        },
    }
