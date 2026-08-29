"""
Estratégia AGI4_BIT_201305 — Momentum assimétrico com filtro de inércia
projetada especificamente para BIT_M5 (Mini Bitcoin / B3).

Motivação
---------
27 estratégias já testadas em BIT_M5 não entregaram edge: ADX_TREND, EMA_CROSSOVER,
DONCHIAN_BREAKOUT, MOMENTUM_BREAKOUT, SUPERTREND, TRIPLE_EMA e STRONG_TREND
(muitas variantes de trend-following) tendem a entrar tarde e tomar SL em
micro-reversões do BTC. Já VWAP, MEAN_REVERSION_ZSCORE, RSI_REVERSION, BOLLINGER,
ENHANCED_RSI_REVERSION, ENHANCED_BOLLINGER e WIN_REVERSION (reversão à média)
perdem porque o BTC não respeita médias em M5 — ele tende, em vez de reverter.

BIT em M5 tem duas características dominantes:
  (1) Direção é definida por candles de corpo longo com pavios curtos
      (convicção institucional); dojis e spinning tops não devem gerar trade.
  (2) Movimentos tendem a *acelerar* depois de um squeeze de Bollinger:
      quando a banda aperta e rompe, o break-out costuma continuar.

Lógica (todas as condições devem ser satisfeitas):
  1. Razão corpo / range (high-low) >= body_ratio_min        → convicção direcional
  2. Pavios curtos nas duas pontas (upper_wick_ratio, lower_wick_ratio limites)
                                                            → rejeição de indecisão
  3. Slope da EMA rápida > slope_min em pontos/vela          → tendência acelerando
  4. Slope alinhado com a direção do corpo da vela          → EMA confirma o candle
  5. Largura de Bollinger normalizada < squeeze_max         → estamos saindo de squeeze
  6. RSI em zona neutra (rsi_lo < rsi < rsi_hi)             → não estamos em extremo
  7. Posição do preço relativa à EMA slow coerente          → viés macro

Direção:
  BUY  → corpo bullish + slope EMA rápido positivo + preço > EMA slow
  SELL → corpo bearish + slope EMA rápido negativo + preço < EMA slow

Parâmetros (via vt_config.json → bit_m5):
  ema_fast, ema_slow, slope_lookback
  body_ratio_min, upper_wick_max, lower_wick_max
  bb_period, bb_std, squeeze_max
  rsi_period, rsi_lo, rsi_hi
  sl_atr_mult, trail_activate, trail_distance
"""

STRATEGY_NAME = "AGI4_BIT_201305"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada AGI4_BIT_201305.

    Args:
        symbol:  ticker B3 (ex: "BITM26")
        tf:      timeframe string (ex: "M5")
        price:   preço de fechamento da vela atual (float)
        atr:     ATR já calculado para o símbolo (float)
        bar_ts:  timestamp da vela atual (int/str)
        bars:    lista de candles newest-first [{"open","high","low","close","volume"}, ...]
        params:  dict de parâmetros configurados
        utils:   dict com calculate_rsi / calculate_ema / calculate_bollinger /
                 calculate_adx / calc_sl

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calculate_ema = utils["calculate_ema"]
    calculate_rsi = utils["calculate_rsi"]
    calculate_bollinger = utils["calculate_bollinger"]
    calc_sl = utils["calc_sl"]

    # --- Parâmetros -----------------------------------------------------------
    ema_fast_period = params.get("ema_fast", 8)
    ema_slow_period = params.get("ema_slow", 34)
    slope_lookback = params.get("slope_lookback", 3)

    body_ratio_min = params.get("body_ratio_min", 0.55)
    upper_wick_max = params.get("upper_wick_max", 0.30)
    lower_wick_max = params.get("lower_wick_max", 0.30)

    bb_period = params.get("bb_period", 20)
    bb_std = params.get("bb_std", 2.0)
    squeeze_max = params.get("squeeze_max", 0.012)  # banda/média; ~1.2%

    rsi_period = params.get("rsi_period", 14)
    rsi_lo = params.get("rsi_lo", 40)
    rsi_hi = params.get("rsi_hi", 60)

    # --- Guardas --------------------------------------------------------------
    min_bars = max(ema_slow_period + slope_lookback, bb_period, rsi_period + 1) + 5
    if not bars or len(bars) < min_bars:
        return None
    if price is None or atr is None or atr <= 0:
        return None

    cur = bars[0]
    o = float(cur.get("open", 0))
    h = float(cur.get("high", 0))
    lo = float(cur.get("low", 0))
    c = float(cur.get("close", 0))
    if h <= lo or o == 0 or c == 0:
        return None

    # --- Indicadores base -----------------------------------------------------
    ema_fast_now = calculate_ema(bars, ema_fast_period)
    ema_slow_now = calculate_ema(bars, ema_slow_period)
    bb_upper, bb_mid, bb_lower = calculate_bollinger(bars, bb_period, bb_std)
    rsi = calculate_rsi(bars, rsi_period)

    if ema_fast_now == 0 or ema_slow_now == 0 or bb_mid == 0:
        return None

    # Slope da EMA rápida = diferença entre EMA atual e EMA de N velas atrás.
    # bars é newest-first; bars[slope_lookback] é a vela (slope_lookback) atrás.
    if len(bars) <= slope_lookback:
        return None
    ema_fast_prev = calculate_ema(bars[slope_lookback:], ema_fast_period)
    ema_slope = ema_fast_now - ema_fast_prev
    slope_min = params.get("slope_min", 0.05) * atr  # escalado pelo ATR (vol-ajustado)

    # --- Anatomia da vela atual ----------------------------------------------
    candle_range = h - lo
    body = c - o
    body_abs = abs(body)
    body_ratio = body_abs / candle_range  # 0..1
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - lo
    upper_wick_ratio = upper_wick / candle_range
    lower_wick_ratio = lower_wick / candle_range

    if body_ratio < body_ratio_min:
        return None
    if upper_wick_ratio > upper_wick_max:
        return None
    if lower_wick_ratio > lower_wick_max:
        return None

    # --- Largura Bollinger normalizada (squeeze detection) --------------------
    bb_width = (bb_upper - bb_lower) / bb_mid if bb_mid > 0 else 999
    in_squeeze_or_just_out = bb_width < squeeze_max

    # --- RSI em zona neutra ---------------------------------------------------
    if rsi <= rsi_lo or rsi >= rsi_hi:
        return None

    # --- Slope significativo (vol-adjusted) -----------------------------------
    if abs(ema_slope) < slope_min:
        return None

    # --- Determinar direção a partir do corpo + slope ------------------------
    # Body bullish → UP; slope da EMA confirma a continuação
    if body > 0 and ema_slope > 0 and price > ema_slow_now:
        direction = "BUY"
    elif body < 0 and ema_slope < 0 and price < ema_slow_now:
        direction = "SELL"
    else:
        return None

    # --- Reforço: candle deve "empurrar" além do EMA rápido ------------------
    # Garante que a EMA não está puxando de volta o trade (curva exausta).
    if direction == "BUY" and c < ema_fast_now:
        return None
    if direction == "SELL" and c > ema_fast_now:
        return None

    # --- Reforço: break-out direcional do BB ---------------------------------
    # Se estamos apertados (squeeze), o close precisa estar do lado correto
    # da média — sinal de escape direcional.
    if in_squeeze_or_just_out:
        if direction == "BUY" and c < bb_mid:
            return None
        if direction == "SELL" and c > bb_mid:
            return None

    # --- LEI 3: SL sempre via utils["calc_sl"] -------------------------------
    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": STRATEGY_NAME,
            "ema_fast": ema_fast_now,
            "ema_slow": ema_slow_now,
            "ema_slope": ema_slope,
            "bb_upper": bb_upper,
            "bb_mid": bb_mid,
            "bb_lower": bb_lower,
            "bb_width": bb_width,
            "rsi": rsi,
            "body_ratio": body_ratio,
            "upper_wick_ratio": upper_wick_ratio,
            "lower_wick_ratio": lower_wick_ratio,
        },
    }
