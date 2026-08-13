"""
AGI4_WDO_H1_MANUAL — Bollinger reversion + RSI para WDO_H1 (stand-in LLM, Wave 881).

Motivo: WDO_H1 estava com RSI_REVERSION (baseline da família mean-reversion),
mas em disabled_timeframes (simulando negativo). Esta versão reforça o
mean-reversion com confirmação de banda Bollinger, mantendo máximo 2
condições de gatilho para evitar o problema de 0 trades em ~260 barras H1.

Sinal de entrada:
- BUY: preço <= banda inferior de Bollinger AND RSI < rsi_oversold
- SELL: preço >= banda superior AND RSI > rsi_overbought

Sem filtro de distância ATR/EMA (ao contrário das auto-geradas de hoje,
que empilhavam overlays e nunca disparavam). Único guard: warmup e ATR > 0.

Parâmetros:
  bb_period, bb_std
  rsi_period, rsi_overbought, rsi_oversold
  sl_atr_mult (via params/calc_sl)

Contrato: calculate_bollinger retorna tupla (upper, mid, lower) escalares.
calculate_rsi retorna float. calc_sl → Lei 3.
"""

STRATEGY_NAME = "AGI4_WDO_H1_MANUAL"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Verifica sinal AGI4_WDO_H1_MANUAL. Retorna None ou dict de sinal."""
    calculate_bollinger = utils["calculate_bollinger"]
    calculate_rsi = utils["calculate_rsi"]
    calc_sl = utils["calc_sl"]

    # Parâmetros
    bb_period = params.get("bb_period", 20)
    bb_std = params.get("bb_std", 2.0)
    rsi_period = params.get("rsi_period", 14)
    rsi_ob = params.get("rsi_overbought", 70)
    rsi_os = params.get("rsi_oversold", 30)

    # Guarda de warmup
    min_bars = max(bb_period, rsi_period) + 5
    if not bars or len(bars) < min_bars:
        return None

    if atr <= 0:
        return None

    bb_upper, bb_mid, bb_lower = calculate_bollinger(bars, bb_period, bb_std)
    if not bb_upper or not bb_lower or bb_mid == 0:
        return None

    rsi = calculate_rsi(bars, rsi_period)
    if rsi is None or rsi == 0:
        return None

    # Condição 1 (BB) AND condição 2 (RSI) — reversão confirmada
    direction = None
    if price <= bb_lower and rsi < rsi_os:
        direction = "BUY"
    elif price >= bb_upper and rsi > rsi_ob:
        direction = "SELL"

    if not direction:
        return None

    # Lei 3: SL obrigatório via calc_sl
    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "AGI4_WDO_H1_MANUAL",
            "bb_upper": bb_upper,
            "bb_lower": bb_lower,
            "rsi": rsi,
            "atr": atr,
        },
    }
