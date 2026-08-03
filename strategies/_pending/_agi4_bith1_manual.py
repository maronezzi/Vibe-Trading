"""
AGI4_BIT_H1_MANUAL — EMA cross + ADX para BIT_H1 (stand-in LLM, Wave 881).

Motivo: estratégia auto-gerada hoje usava regime-switching com branch
trending+ranging e filtros de DI/posicionamento rígidos que nunca
dispararam em BIT H1 (que costuma rangear). Esta versão é um EMA cross
clássico (espelha ADX_TREND, deployed em BIT_H1 hoje) sem regime branch,
para garantir geração de trades.

Sinal de entrada:
- BUY: EMA fast > EMA slow (cross de alta)
- SELL: EMA fast < EMA slow (cross de baixa)

Único filtro: confirmação de tendência via DX (nãoSmoothed, retornado por
calculate_adx como primeiro elemento da tupla) > adx_threshold. Evita entrar
em range sem direção.

Parâmetros:
  ema_fast, ema_slow, adx_period, adx_threshold
  rsi_period, rsi_overbought, rsi_oversold (filtro don't-chase leve)
  sl_atr_mult (via params/calc_sl)

Contrato: utils retorna escalares/tuplas — nunca indexar. calc_sl → Lei 3.
"""

STRATEGY_NAME = "AGI4_BIT_H1_MANUAL"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Verifica sinal AGI4_BIT_H1_MANUAL. Retorna None ou dict de sinal."""
    calculate_ema = utils["calculate_ema"]
    calculate_adx = utils["calculate_adx"]
    calculate_rsi = utils["calculate_rsi"]
    calc_sl = utils["calc_sl"]

    # Parâmetros
    ema_fast = params.get("ema_fast", 9)
    ema_slow = params.get("ema_slow", 21)
    adx_period = params.get("adx_period", 14)
    adx_threshold = params.get("adx_threshold", 20)  # BIT rangear → limiar menor
    rsi_period = params.get("rsi_period", 14)
    rsi_ob = params.get("rsi_overbought", 75)
    rsi_os = params.get("rsi_oversold", 25)

    # Guarda de warmup
    min_bars = max(ema_slow, adx_period * 2, rsi_period) + 5
    if not bars or len(bars) < min_bars:
        return None

    if atr <= 0:
        return None

    ema_f = calculate_ema(bars, ema_fast)
    ema_s = calculate_ema(bars, ema_slow)
    if not ema_f or not ema_s:
        return None

    # Condição 1: EMA cross
    direction = None
    if ema_f > ema_s:
        direction = "BUY"
    elif ema_f < ema_s:
        direction = "SELL"
    else:
        return None

    # Condição 2: confirmação de tendência (DX > threshold)
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    if adx_val < adx_threshold:
        return None  # Sem tendência — range

    # Filtro leve don't-chase: RSI extremo contra direção = exaustão
    rsi = calculate_rsi(bars, rsi_period)
    if rsi and rsi > 0:
        if direction == "BUY" and rsi > rsi_ob:
            return None
        if direction == "SELL" and rsi < rsi_os:
            return None

    # Lei 3: SL obrigatório via calc_sl
    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "AGI4_BIT_H1_MANUAL",
            "ema_fast": ema_f,
            "ema_slow": ema_s,
            "adx": adx_val,
            "rsi": rsi,
        },
    }
