"""
AGI4_WIN_H1_MANUAL — RSI reversion puro para WIN_H1 (stand-in LLM, Wave 881).

Motivo: estratégias auto-geradas hoje (03/08) falharam com 0 trades em 30d
por empilharem 4+ filtros (cost_atr_mult + BB + EMA + ADX) sobre ~260 barras
H1. Esta versão usa MÁXIMO 2 condições de entrada, espelhando o baseline
ENHANCED_RSI_REVERSION (deployed em WIN_H1) sem o overlay de distância ATR
que matava os sinais.

Sinal de entrada:
- BUY: RSI < rsi_oversold (sobrevendido → reversão esperada pra cima)
- SELL: RSI > rsi_overbought (sobrecomprado → reversão esperada pra baixo)

Único filtro: anti falling-knife via ADX/DI (mesma lógica do RSI_REVERSION
production, fix 2026-07-26): em tendência forte (DX > adx_threshold), só
opera a favor do fluxo, não contra.

Parâmetros:
  rsi_period, rsi_overbought, rsi_oversold
  adx_period, adx_threshold
  sl_atr_mult (via params/calc_sl)

Contrato: utils retorna escalares/tuplas — NUNCA indexar com [-1], len(), ou
.get(). calc_sl para Lei 3 (SL obrigatório).
"""

STRATEGY_NAME = "AGI4_WIN_H1_MANUAL"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Verifica sinal AGI4_WIN_H1_MANUAL. Retorna None ou dict de sinal."""
    calculate_rsi = utils["calculate_rsi"]
    calculate_adx = utils["calculate_adx"]
    calc_sl = utils["calc_sl"]

    # Parâmetros
    rsi_period = params.get("rsi_period", 14)
    rsi_ob = params.get("rsi_overbought", 70)
    rsi_os = params.get("rsi_oversold", 30)
    adx_period = params.get("adx_period", 14)
    adx_threshold = params.get("adx_threshold", 25)

    # Guarda de warmup
    if not bars or len(bars) < max(rsi_period, adx_period) + 10:
        return None

    # ATR mínimo — mercado sem movimento não gera edge
    if atr <= 0:
        return None

    rsi = calculate_rsi(bars, rsi_period)
    if rsi is None or rsi == 0:
        return None

    # Condição 1: RSI extremo
    direction = None
    if rsi < rsi_os:
        direction = "BUY"
    elif rsi > rsi_ob:
        direction = "SELL"

    if not direction:
        return None

    # Condição 2 (anti falling-knife): em tendência forte só opera a favor
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    if adx_val > adx_threshold:
        if direction == "BUY" and plus_di < minus_di:
            return None  # Downtrend forte — não comprar
        if direction == "SELL" and minus_di < plus_di:
            return None  # Uptrend forte — não vender

    # Lei 3: SL obrigatório via calc_sl
    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "AGI4_WIN_H1_MANUAL",
            "rsi": rsi,
            "adx": adx_val,
            "atr": atr,
        },
    }
