"""
Estratégia AGI4_WSP_120957 — Pullback em 3 EMAs para WSP_M30.

Substitui o donchian_period=14 (breakout único) por um modelo de 'pullback
em 3 EMAs': a EMA rápida repuxa até a EMA média sem fechar abaixo dela, com
entrada no RETORNO (price volta a subir/descer) e stop 1x ATR além do
pullback. Desenho alinha a entrada ao retorno real da tendência, evitando
entrar no fundo do pullback (causa do 0% WR em 5 trades no DB).

Regras:
1. Tendência alinhada: EMA_fast > EMA_med > EMA_slow (alta) ou inversa (baixa)
2. Pullback: preço recua da EMA rápida até a EMA média (dentro de tolerância x ATR)
   sem que nenhuma barra recente feche abaixo (alta) / acima (baixa) da EMA média
3. Entrada no retorno: preço volta a subir/descer confirmando a tendência
4. Filtro de força: ADX >= threshold
5. Stop: 1x ATR além do pullback via calc_sl (sl_atr_mult)

Parâmetros (via vt_config.json → params_by_tf.WSP_M30):
  ema_fast, ema_medium, ema_slow, adx_period, adx_threshold
  pullback_lookback, pullback_tolerance_atr, sl_atr_mult
"""

STRATEGY_NAME = "AGI4_WSP_120957"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada AGI4_WSP_120957 (pullback em 3 EMAs).

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calculate_ema = utils["calculate_ema"]
    calculate_adx = utils["calculate_adx"]
    calc_sl = utils["calc_sl"]

    # Parâmetros
    ema_fast_period = params.get("ema_fast", 9)
    ema_medium_period = params.get("ema_medium", 21)
    ema_slow_period = params.get("ema_slow", 50)
    adx_period = params.get("adx_period", 14)
    adx_threshold = params.get("adx_threshold", 20)
    pullback_lookback = params.get("pullback_lookback", 8)
    pullback_tolerance_atr = params.get("pullback_tolerance_atr", 0.5)
    rsi_period = params.get("rsi_period", 14)

    min_bars = max(ema_slow_period, adx_period * 2, pullback_lookback) + 5
    if not bars or len(bars) < min_bars:
        return None
    if atr <= 0:
        return None

    # Indicadores (retornos escalares / tupla fixa — usar direto, sem indexar)
    ema_fast_val = calculate_ema(bars, ema_fast_period)
    ema_med_val = calculate_ema(bars, ema_medium_period)
    ema_slow_val = calculate_ema(bars, ema_slow_period)
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)

    if ema_fast_val == 0 or ema_med_val == 0 or ema_slow_val == 0 or adx_val == 0:
        return None

    # Força da tendência
    if adx_val < adx_threshold:
        return None

    # Barras recentes para detectar o pullback
    recent = bars[:pullback_lookback]
    min_low = min(float(b["low"]) for b in recent)
    max_high = max(float(b["high"]) for b in recent)

    # --- Sinal de COMPRA (tendência de alta) ---
    if ema_fast_val > ema_med_val > ema_slow_val:
        # Pullback recuou até a EMA média (dentro da tolerância x ATR)
        if min_low > ema_med_val + pullback_tolerance_atr * atr:
            return None  # não chegou perto da EMA média
        # Nenhuma barra recente fechou abaixo da EMA média
        if any(float(b["close"]) < ema_med_val for b in recent):
            return None  # tendência rompida, não é pullback
        # Recuou da EMA rápida (houve repuxo real)
        if min_low > ema_fast_val:
            return None  # nunca saiu da EMA rápida — sem pullback
        # Entrada no retorno: preço subiu acima do fundo do pullback e fechou
        # acima da EMA média (volta a confirmar a tendência)
        last_close = float(recent[0]["close"])
        if price <= min_low or last_close <= ema_med_val:
            return None  # ainda no fundo / sem retorno
        direction = "BUY"
        info_pull = {
            "retrace_low": round(min_low, 2),
            "ema_medium": round(ema_med_val, 2),
        }

    # --- Sinal de VENDA (tendência de baixa) ---
    elif ema_fast_val < ema_med_val < ema_slow_val:
        # Pullback subiu até a EMA média (dentro da tolerância x ATR)
        if max_high < ema_med_val - pullback_tolerance_atr * atr:
            return None
        # Nenhuma barra recente fechou acima da EMA média
        if any(float(b["close"]) > ema_med_val for b in recent):
            return None
        # Recuou da EMA rápida (houve repuxo real)
        if max_high < ema_fast_val:
            return None
        # Entrada no retorno: preço caiu abaixo da crista do pullback e fechou
        # abaixo da EMA média
        last_close = float(recent[0]["close"])
        if price >= max_high or last_close >= ema_med_val:
            return None
        direction = "SELL"
        info_pull = {
            "retrace_high": round(max_high, 2),
            "ema_medium": round(ema_med_val, 2),
        }

    else:
        return None  # EMAs cruzadas / sem alinhamento

    # LEI 3: stop 1x ATR além do pullback via calc_sl
    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": STRATEGY_NAME,
            "ema_fast": round(ema_fast_val, 2),
            "ema_medium": ema_med_val,
            "ema_slow": round(ema_slow_val, 2),
            "adx": adx_val,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "pullback_tolerance_atr": pullback_tolerance_atr,
            "retrace": info_pull,
        },
    }