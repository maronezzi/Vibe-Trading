"""
Estratégia SUPERTREND — ATR-based trend indicator com trailing dinâmico.

Sinal de entrada:
- BUY: preço cruza acima do Supertrend (muda de downtrend pra uptrend)
- SELL: preço cruza abaixo do Supertrend (muda de uptrend pra downtrend)

Lógica:
1. Calcula o Supertrend usando ATR e multiplier
2. Detecta mudança de direção (flip de tendência)
3. Confirma que o flip é recente (cruzamento na barra atual ou anterior)

Parâmetros:
  atr_period=10, multiplier=3.0
"""

STRATEGY_NAME = "SUPERTREND"


def _calculate_supertrend(bars, atr_period, multiplier):
    """
    Calcula o valor do Supertrend para as barras mais recentes.

    bars: newest-first → inverte pra processar oldest-first, depois devolve
    Retorna (supertrend_valor, direção_atual) onde direção: 1=up, -1=down
    """
    n = len(bars)
    if n < atr_period + 2:
        return 0.0, 0

    # Inverter para oldest-first (processamento sequencial)
    oldest_first = list(reversed(bars))

    # Calcular ATR manualmente (True Range → SMA)
    tr_list = []
    for i in range(1, len(oldest_first)):
        high = oldest_first[i]["high"]
        low = oldest_first[i]["low"]
        prev_close = oldest_first[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)

    if len(tr_list) < atr_period:
        return 0.0, 0

    # ATR rolling (SMA simples)
    atr_values = []
    for i in range(len(tr_list)):
        if i < atr_period - 1:
            atr_values.append(sum(tr_list[: i + 1]) / (i + 1))
        else:
            atr_values.append(sum(tr_list[i - atr_period + 1 : i + 1]) / atr_period)

    # Supertrend calculation
    supertrend = [0.0] * len(oldest_first)
    direction = [0] * len(oldest_first)  # 1=up, -1=down

    for i in range(atr_period, len(oldest_first)):
        hl2 = (oldest_first[i]["high"] + oldest_first[i]["low"]) / 2.0
        atr_i = atr_values[i - 1]  # offset porque tr_list starts at index 1

        upper_band = hl2 + multiplier * atr_i
        lower_band = hl2 - multiplier * atr_i

        if i == atr_period:
            supertrend[i] = upper_band
            direction[i] = 1
            continue

        prev_st = supertrend[i - 1]
        prev_dir = direction[i - 1]

        close = oldest_first[i]["close"]

        if prev_dir == 1:  # Uptrend anterior
            if close <= prev_st:
                # Flip to downtrend
                supertrend[i] = upper_band
                direction[i] = -1
            else:
                # Continue uptrend
                supertrend[i] = max(lower_band, prev_st)
                direction[i] = 1
        else:  # Downtrend anterior
            if close >= prev_st:
                # Flip to uptrend
                supertrend[i] = lower_band
                direction[i] = 1
            else:
                # Continue downtrend
                supertrend[i] = min(upper_band, prev_st)
                direction[i] = -1

    # Retornar últimos valores (inverter de volta)
    st_val = supertrend[-1]
    dir_val = direction[-1]

    return st_val, dir_val


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada SUPERTREND.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calc_sl = utils["calc_sl"]

    atr_period = params.get("atr_period", 10)
    multiplier = params.get("multiplier", 3.0)

    min_bars = atr_period + 5
    if not bars or len(bars) < min_bars:
        return None

    if atr <= 0:
        return None

    # Supertrend atual (barra 0)
    st_val, st_dir = _calculate_supertrend(bars, atr_period, multiplier)
    if st_val == 0:
        return None

    # Supertrend da barra anterior (bars[1:])
    st_val_prev, st_dir_prev = _calculate_supertrend(bars[1:], atr_period, multiplier)

    direction = None

    # BUY: flip de downtrend (-1) pra uptrend (1)
    if st_dir_prev == -1 and st_dir == 1:
        direction = "BUY"

    # SELL: flip de uptrend (1) pra downtrend (-1)
    elif st_dir_prev == 1 and st_dir == -1:
        direction = "SELL"

    if not direction:
        return None

    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "SUPERTREND",
            "supertrend": st_val,
            "supertrend_prev": st_val_prev,
            "direction": st_dir,
            "atr": atr,
            "multiplier": multiplier,
        },
    }
