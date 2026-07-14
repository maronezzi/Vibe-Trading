"""
Wave W874 (2026-07-08) — Estratégia LIQUIDITY_SWEEP_REVERSAL.

Edge: detectar stop hunt / liquidity sweep institucional (SMC).
Mercado varre highs/lows anteriores para coletar stops, depois reverte.
Edge explorado: varredura de liquidez acima/abaixo de swing points.

Sinal:
  - SELL: preço sweep acima do prior N-bar high por buffer ATR,
          mas FECHA abaixo do swing high (rejeição) → reversão para baixo
  - BUY:  preço sweep abaixo do prior N-bar low por buffer ATR,
          mas FECHA acima do swing low → reversão para cima

Defensivo:
  - sl_atr_mult padrão 1.8 (stop acima/abaixo do sweep = RR > 2.5)
  - Sweep buffer = 0.3 × ATR (precisa ser varredura real, não toque)
  - Lookback 20 barras (swing point relevante)
  - Requer ADX > 18 (mercado com volatilidade direcional)

Parâmetros:
  lookback=20, sweep_buffer_atr=0.3, adx_period=14, adx_min=18,
  sl_atr_mult=1.8

Diferencial vs pool atual:
  - ema_crossover/supertrend: trend-following, não detectam reversal em sweep.
  - bollinger/rsi_reversion: mean-reversion em bandas, sem contexto SMC.
  - Esta estratégia captura ineficiência institucional (stop hunt).
"""
STRATEGY_NAME = "LIQUIDITY_SWEEP_REVERSAL"

DEFAULT_PARAMS = {
    "lookback": 20,
    "sweep_buffer_atr": 0.3,
    "adx_period": 14,
    "adx_min": 18,
    "sl_atr_mult": 1.8,
}


def _prior_swing_extremes(bars, lookback):
    """Calcula high/low das N barras anteriores (excluindo barra atual)."""
    if not bars or len(bars) < lookback + 1:
        return None, None
    prior = bars[1:lookback + 1]
    if not prior:
        return None, None
    swing_high = max(b["high"] for b in prior)
    swing_low = min(b["low"] for b in prior)
    return swing_high, swing_low


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Verifica sinal de entrada LIQUIDITY_SWEEP_REVERSAL.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    p = {**DEFAULT_PARAMS, **(params or {})}

    calculate_adx = utils["calculate_adx"]
    calc_sl = utils["calc_sl"]

    if not bars or len(bars) < p["lookback"] + 2:
        return None
    if atr is None or atr <= 0:
        return None

    # ADX: precisa de volatilidade direcional
    adx_tuple = calculate_adx(bars, p["adx_period"])
    if isinstance(adx_tuple, tuple):
        adx_val = adx_tuple[0]
        plus_di = adx_tuple[1] if len(adx_tuple) > 1 else 0
        minus_di = adx_tuple[2] if len(adx_tuple) > 2 else 0
    else:
        adx_val = adx_tuple
        plus_di = minus_di = 0
    if adx_val < p["adx_min"]:
        return None

    # Swing points das últimas N barras
    swing_high, swing_low = _prior_swing_extremes(bars, p["lookback"])
    if swing_high is None or swing_low is None or swing_high <= swing_low:
        return None

    # Buffer para varredura real
    buffer = atr * p["sweep_buffer_atr"]
    current_high = bars[0]["high"]
    current_low = bars[0]["low"]
    current_close = bars[0]["close"]

    direction = None
    sweep_level = None

    # SELL: varreu acima do swing high, mas fechou abaixo (rejeição)
    if current_high >= swing_high + buffer and current_close < swing_high:
        direction = "SELL"
        sweep_level = swing_high

    # BUY: varreu abaixo do swing low, mas fechou acima (rejeição)
    elif current_low <= swing_low - buffer and current_close > swing_low:
        direction = "BUY"
        sweep_level = swing_low

    if not direction:
        return None

    sl_pts = calc_sl(symbol, atr, p)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "LIQUIDITY_SWEEP_REVERSAL",
            "swing_high": round(swing_high, 4),
            "swing_low": round(swing_low, 4),
            "sweep_level": round(sweep_level, 4),
            "sweep_buffer": round(buffer, 4),
            "current_high": round(current_high, 4),
            "current_low": round(current_low, 4),
            "current_close": round(current_close, 4),
            "adx": round(adx_val, 1),
            "plus_di": round(plus_di, 1),
            "minus_di": round(minus_di, 1),
            "atr": atr,
        },
    }
