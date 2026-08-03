"""
AGI4_WDO_M15_MANUAL — Donchian breakout + ATR expansion para WDO_M15 (stand-in LLM, Wave 881).

Motivo: WDO_M15 estava com EMA_PULLBACK (baseline) em disabled_timeframes
(simulando negativo). Esta versão tenta abordagem diferente — breakout de
Donchian com confirmação de expansão de volatilidade. M15 tem ~900 barras
em 30d (vs ~260 do H1), então comporta uma lógica de breakout que precisa
de mais amostra. Máximo 2 condições de gatilho.

Sinal de entrada:
- BUY: preço >= máximo de Donchian (lookback) AND ATR atual >= ATR médio * mult
- SELL: preço <= mínimo de Donchian AND ATR atual >= ATR médio * mult

Filtro de expansão de ATR garante que só entra em breakouts com volatilidade
real (não falsos em range apertado). Sem overlays extras.

Parâmetros:
  donchian_lookback (janela do canal)
  atr_period, atr_expansion_mult (expansão mínima vs média)
  rsi_period, rsi_overbought, rsi_oversold (don't-chase leve)
  sl_atr_mult (via params/calc_sl)

Contrato: utils.calculate_atr retorna float (escalar). calc_sl → Lei 3.
bars[0] é a barra mais recente (newest-first).
"""

STRATEGY_NAME = "AGI4_WDO_M15_MANUAL"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Verifica sinal AGI4_WDO_M15_MANUAL. Retorna None ou dict de sinal."""
    calculate_atr = utils["calculate_atr"]
    calculate_rsi = utils["calculate_rsi"]
    calc_sl = utils["calc_sl"]

    # Parâmetros
    lookback = params.get("donchian_lookback", 20)
    atr_period = params.get("atr_period", 14)
    atr_mult = params.get("atr_expansion_mult", 1.2)
    rsi_period = params.get("rsi_period", 14)
    rsi_ob = params.get("rsi_overbought", 75)
    rsi_os = params.get("rsi_oversold", 25)

    # Guarda de warmup — precisa de lookback para o canal + período de ATR médio
    min_bars = max(lookback, atr_period) + 20
    if not bars or len(bars) < min_bars:
        return None

    if atr <= 0:
        return None

    # Canal de Donchian: usa janela [1 .. lookback] (exclui barra atual p/ evitar
    # self-inclusion no breakout). bars[0] = mais recente.
    window = bars[1 : lookback + 1]
    if len(window) < lookback:
        return None
    recent_high = max(b["high"] for b in window)
    recent_low = min(b["low"] for b in window)

    # Condição 1: breakout do canal
    direction = None
    if price >= recent_high:
        direction = "BUY"
    elif price <= recent_low:
        direction = "SELL"
    else:
        return None

    # Condição 2: expansão de ATR (volatilidade confirma o breakout)
    avg_atr = calculate_atr(bars, atr_period)
    if not avg_atr or avg_atr <= 0:
        return None
    if atr < avg_atr * atr_mult:
        return None  # Sem expansão — provável falso breakout

    # Filtro leve don't-chase: RSI já saturado na direção
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
            "strategy": "AGI4_WDO_M15_MANUAL",
            "recent_high": recent_high,
            "recent_low": recent_low,
            "atr": atr,
            "avg_atr": avg_atr,
        },
    }
