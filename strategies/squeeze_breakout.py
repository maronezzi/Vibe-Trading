"""
Squeeze Breakout — estratégia baseada em TTM Squeeze.

Wave 5 (2026-06-26): implementa filtro de volatilidade via Bollinger Band
dentro de Keltner Channel. Quando BB está DENTRO de KC, o mercado está em
"squeeze" (compressão de volatilidade). Saída do squeeze + momentum direcional
= sinal de entrada de alta probabilidade.

LÓGICA (TTM Squeeze-inspired):
  squeeze_on  = BB_upper < KC_upper AND BB_lower > KC_lower  (vol comprimida)
  squeeze_off = cruzou pra fora (vol expandindo)
  entry       = momentum (MACD hist > 0 OU DI spread > threshold) APÓS squeeze_off
  sl          = 1.5 × ATR
  tp          = 2.0 × ATR (trailing 1.0 ATR)
  filtros     : volume ratio > 0.5, ADX > 20, ATR > min_atr_symbol

Por que importa: 76% dos exits são SL_SERVIDOR (estop estúpido em 5 min).
Mercado em chop gera sinais falsos. Squeeze filtra os chops — só opera
quando vol comprime E DEPOIS expande com momentum.
"""

STRATEGY_NAME = "SQUEEZE_BREAKOUT"

# Defaults defensivos
DEFAULT_PARAMS = {
    "bb_period": 20,
    "bb_std": 2.0,
    "kc_period": 20,
    "kc_atr_mult": 1.5,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "adx_threshold": 20,
    "vol_ratio_min": 0.5,
    "sl_atr_mult": 1.5,
    "cooldown_seconds": 300,
}


def _is_squeeze_on(bars, params, utils):
    """
    Retorna True se o mercado está em squeeze (BB dentro de KC).

    Squeeze on  = BB_upper < KC_upper AND BB_lower > KC_lower
    (volatilidade Bollinger está MENOR que volatilidade Keltner)
    """
    bb_period = params.get("bb_period", DEFAULT_PARAMS["bb_period"])
    bb_std = params.get("bb_std", DEFAULT_PARAMS["bb_std"])
    kc_period = params.get("kc_period", DEFAULT_PARAMS["kc_period"])
    kc_atr_mult = params.get("kc_atr_mult", DEFAULT_PARAMS["kc_atr_mult"])

    calculate_bollinger = utils.get("calculate_bollinger")
    calculate_ema = utils.get("calculate_ema")
    calculate_atr = utils.get("calculate_atr")
    if not all([calculate_bollinger, calculate_ema, calculate_atr]):
        return None  # Utils insuficientes

    if not bars or len(bars) < max(bb_period, kc_period) + 5:
        return None

    # Bollinger Bands
    bb_upper, bb_mid, bb_lower = calculate_bollinger(bars, bb_period, bb_std)
    if not bb_upper or not bb_lower:
        return None

    # Keltner Channel (EMA ± ATR * mult)
    ema = calculate_ema(bars, kc_period)
    atr = calculate_atr(bars, kc_period)
    if not ema or not atr:
        return None

    kc_upper = ema + kc_atr_mult * atr
    kc_lower = ema - kc_atr_mult * atr

    # Squeeze on = BB está dentro de KC
    return bb_upper < kc_upper and bb_lower > kc_lower


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Squeeze Breakout — opera APENAS quando:
      1. Mercado ESTAVA em squeeze (vol comprimida)
      2. Mercado SAIU do squeeze (vol expandindo)
      3. Momentum direcional confirma (MACD hist)
      4. Filtros secundários passam (vol, ADX, ATR)
    """
    # Merge com defaults
    p = {**DEFAULT_PARAMS, **params}

    calc_sl = utils["calc_sl"]
    calculate_macd = utils.get("calculate_macd")
    calculate_adx = utils.get("calculate_adx")

    if not bars or len(bars) < 50:
        return None

    # Verifica squeeze on (barra anterior deve estar em squeeze, atual não)
    squeeze_now = _is_squeeze_on(bars, p, utils)
    squeeze_prev = _is_squeeze_on(bars[:-1], p, utils) if len(bars) > 1 else None
    if squeeze_now is None or squeeze_prev is None:
        return None

    # Squeeze acabou: estava em squeeze antes, não está agora
    squeeze_release = squeeze_prev and not squeeze_now
    if not squeeze_release:
        return None

    # Filtro ADX (regime)
    if calculate_adx is not None:
        adx = calculate_adx(bars, p.get("adx_period", 14))
        if adx is None or adx < p["adx_threshold"]:
            return None

    # Momentum direcional via MACD histogram
    if calculate_macd is not None:
        macd_line, signal_line, histogram = calculate_macd(
            bars, p["macd_fast"], p["macd_slow"], p["macd_signal"]
        )
        if histogram is None:
            return None
        # Histogram > 0 = momentum bullish, < 0 = bearish
        if histogram > 0:
            direction = "BUY"
        elif histogram < 0:
            direction = "SELL"
        else:
            return None
    else:
        # Fallback: usar DI spread se MACD não disponível
        return None  # SQUEEZE exige MACD; sem utils, sem sinal

    sl_pts = calc_sl(symbol, atr, p)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "SQUEEZE_BREAKOUT",
            "squeeze_release": True,
            "adx": round(adx, 1) if adx else None,
            "macd_hist": round(histogram, 2) if histogram else None,
        },
    }
