"""
Estratégia STOCHASTIC — Oscilador estocástico (%K, %D) para reversão em extremos.

Sinal de entrada:
- BUY: %K cruza %D pra cima abaixo de oversold (20) → preço sobrevendido reversando
- SELL: %K cruza %D pra baixo acima de overbought (80) → preço sobrecomprado reversando

Lógica:
1. Calcula %K = (close - lowest_low) / (highest_high - lowest_low) * 100
2. Suaviza %K com SMA de smooth períodos
3. Calcula %D = SMA(%K, d_period)
4. Detecta cruzamento %K/%D nas zonas de extremo

Parâmetros:
  k_period=14, d_period=3, smooth=3, overbought=80, oversold=20
"""

STRATEGY_NAME = "STOCHASTIC"


def _calculate_stochastic(bars, k_period, smooth):
    """
    Calcula %K suavizado do oscilador estocástico.

    bars: newest-first → usa bars[:k_period] pra cada cálculo
    smooth: suavização de %K (SMA de smooth valores de %K bruto)

    Retorna %K suavizado ou None se dados insuficientes.
    """
    if len(bars) < k_period + smooth:
        return None

    # Calcular %K bruto pra cada posição disponível (smooth valores)
    raw_k_values = []
    for offset in range(smooth):
        segment = bars[offset : offset + k_period]
        if len(segment) < k_period:
            break
        highest = max(b["high"] for b in segment)
        lowest = min(b["low"] for b in segment)
        close = segment[0]["close"]  # mais recente do segmento

        if highest == lowest:
            raw_k_values.append(50.0)
        else:
            raw_k_values.append((close - lowest) / (highest - lowest) * 100.0)

    if len(raw_k_values) < smooth:
        return None

    # %K suavizado = SMA dos %K brutos
    smoothed_k = sum(raw_k_values) / len(raw_k_values)
    return smoothed_k


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada STOCHASTIC.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calc_sl = utils["calc_sl"]

    k_period = params.get("k_period", 14)
    d_period = params.get("d_period", 3)
    smooth = params.get("smooth", 3)
    overbought = params.get("overbought", 80)
    oversold = params.get("oversold", 20)

    min_bars = k_period + smooth + d_period + 2
    if not bars or len(bars) < min_bars:
        return None

    if atr <= 0:
        return None

    # %K atual (suavizado)
    k_now = _calculate_stochastic(bars, k_period, smooth)
    if k_now is None:
        return None

    # %K da barra anterior (pra detectar cruzamento)
    k_prev = _calculate_stochastic(bars[1:], k_period, smooth)
    if k_prev is None:
        return None

    # %D = SMA(%K, d_period) — precisa de d_period valores de %K
    # Calculamos %K em várias posições pra ter d_period valores
    k_values_for_d = []
    for i in range(d_period):
        k_val = _calculate_stochastic(bars[i:], k_period, smooth)
        if k_val is None:
            return None
        k_values_for_d.append(k_val)

    d_now = sum(k_values_for_d) / len(k_values_for_d)

    # %D da barra anterior
    k_values_for_d_prev = []
    for i in range(1, d_period + 1):
        k_val = _calculate_stochastic(bars[i:], k_period, smooth)
        if k_val is None:
            return None
        k_values_for_d_prev.append(k_val)

    d_prev = sum(k_values_for_d_prev) / len(k_values_for_d_prev)

    direction = None

    # BUY: %K cruza %D pra cima abaixo de oversold
    if k_now > d_now and k_prev <= d_prev and k_now < oversold:
        direction = "BUY"

    # SELL: %K cruza %D pra baixo acima de overbought
    elif k_now < d_now and k_prev >= d_prev and k_now > overbought:
        direction = "SELL"

    if not direction:
        return None

    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "STOCHASTIC",
            "k": k_now,
            "d": d_now,
            "k_prev": k_prev,
            "d_prev": d_prev,
            "overbought": overbought,
            "oversold": oversold,
            "atr": atr,
        },
    }
