"""
AGI4_WSP_073013 — Estratégia "Statistical Squeeze Reversion" para WSP_M15.

Abordagem diferenciada das 28 estratégias testadas sem sucesso em WSP:
- Em vez de trend-following (a maioria) ou breakout puro, busca REVERSÃO
  estatística após PERÍODOS DE COMPRESSÃO DE VOLATILIDADE (Bollinger
  squeeze).
- Três filtros independentes em cascata, todos precisam confirmar:
    1. Squeeze: BB width < N% da ATR (volatility compression)
    2. Regime: ADX < threshold + ADX caindo (sem tendência forte = seguro reverter)
    3. Estrutura: posição do preço vs EMA200 (só reverte a favor da estrutura)
    4. Trigger: RSI em zona extrema + candle direcional (close vs open)
- SL via utils.calc_sl (Lei 3) com bônus por width do squeeze.

Inspirada em estatística de cointegração intra-dia: depois de compactar,
o ativo expande — entramos na direção da MÉDIA (reversão à média), não
na direção do rompimento.
"""

STRATEGY_NAME = "AGI4_WSP_073013"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    # --- guards mínimos ---
    if not bars or len(bars) < 80 or atr is None or atr <= 0:
        return None

    calc_sl = utils["calc_sl"]
    calculate_rsi = utils["calculate_rsi"]
    calculate_ema = utils["calculate_ema"]
    calculate_bollinger = utils["calculate_bollinger"]
    calculate_adx = utils["calculate_adx"]

    # --- parâmetros (todos via params.get c/ default) ---
    bb_period = int(params.get("bb_period", 20))
    bb_std = float(params.get("bb_std", 2.0))
    squeeze_width_atr = float(params.get("squeeze_width_atr", 0.55))
    adx_period = int(params.get("adx_period", 14))
    adx_max = float(params.get("adx_max", 22.0))
    adx_drop_lookback = int(params.get("adx_drop_lookback", 8))
    ema_struct_period = int(params.get("ema_struct_period", 200))
    rsi_period = int(params.get("rsi_period", 14))
    rsi_overbought = float(params.get("rsi_overbought", 72.0))
    rsi_oversold = float(params.get("rsi_oversold", 28.0))
    candle_body_atr_min = float(params.get("candle_body_atr_min", 0.35))
    min_sl_pts_bonus = int(params.get("min_sl_pts_bonus", 35))
    atr_sl_mult = float(params.get("atr_sl_mult", 1.8))
    squeeze_widen_atr = float(params.get("squeeze_widen_atr", 0.85))

    # --- Bollinger Bands ---
    bb = calculate_bollinger(bars, bb_period, bb_std)
    if bb is None:
        return None
    bb_upper, bb_mid, bb_lower = bb
    if bb_upper is None or bb_mid is None or bb_lower is None:
        return None

    # --- EMA estrutural ---
    ema_struct = calculate_ema(bars, ema_struct_period)
    if ema_struct is None or len(ema_struct) < 2:
        return None

    # --- RSI ---
    rsi = calculate_rsi(bars, rsi_period)
    if rsi is None:
        return None

    # --- ADX + variação ---
    adx_pack = calculate_adx(bars, adx_period)
    if adx_pack is None:
        return None
    adx_val = adx_pack[0]
    if adx_val is None:
        return None

    adx_now = float(adx_val)
    # ADX "caindo": comparamos com leitura de N barras atrás
    adx_prev = None
    if len(bars) > adx_drop_lookback + adx_period:
        adx_prev_pack = calculate_adx(bars[: -(adx_drop_lookback)], adx_period)
        if adx_prev_pack is not None and adx_prev_pack[0] is not None:
            adx_prev = float(adx_prev_pack[0])

    # --- 1) SQUEEZE: BB width (upper - lower) < fração do ATR ---
    bb_width = float(bb_upper) - float(bb_lower)
    squeeze_ratio = bb_width / atr
    is_squeeze = squeeze_ratio < squeeze_width_atr
    if not is_squeeze:
        return None

    # --- 2) REGIME: ADX baixo + ADX caindo (sem tendência perigosa) ---
    regime_ok = adx_now < adx_max
    if adx_prev is not None:
        regime_ok = regime_ok and (adx_now < adx_prev)
    if not regime_ok:
        return None

    # --- 3) ESTRUTURA: preço vs EMA200 ---
    struct_above = price > float(ema_struct[-1])

    # --- 4) CANDLE DIRECIONAL: corpo grande confirma intenção ---
    last_bar = bars[-1]
    close = float(last_bar.get("close", price))
    opn = float(last_bar.get("open", close))
    candle_body = abs(close - opn)
    body_ok = candle_body >= atr * candle_body_atr_min

    # --- Decisão LONG (preço abaixo do meio, RSI oversold, estrutura altista) ---
    long_zone = (close <= float(bb_mid)) and (rsi <= rsi_oversold)
    long_trigger = body_ok and (close > opn)  # candle verde de reversão
    long_struct = struct_above
    if long_zone and long_trigger and long_struct:
        # SL via utils.calc_sl (Lei 3) com bônus se o squeeze já começou a abrir
        sl_atr_pts = int(round(atr * atr_sl_mult))
        bonus = 0
        if squeeze_ratio > squeeze_widen_atr:
            bonus = min_sl_pts_bonus
        sl_pts = max(sl_atr_pts, calc_sl(symbol, atr, params) + bonus)
        return {
            "direction": "BUY",
            "sl_pts": int(sl_pts),
            "info": {
                "rationale": "AGI4_WSP squeeze reversion long",
                "squeeze_ratio": round(squeeze_ratio, 3),
                "adx": round(adx_now, 1),
                "adx_prev": round(adx_prev, 1) if adx_prev is not None else None,
                "rsi": round(rsi, 1),
                "struct_above_ema200": bool(struct_above),
                "bb_pos": "lower_half" if close <= bb_mid else "above_mid",
                "candle_body_atr": round(candle_body / atr, 2),
                "atr": round(atr, 2),
            },
        }

    # --- Decisão SHORT (simétrico) ---
    short_zone = (close >= float(bb_mid)) and (rsi >= rsi_overbought)
    short_trigger = body_ok and (close < opn)  # candle vermelho de reversão
    short_struct = (not struct_above)
    if short_zone and short_trigger and short_struct:
        sl_atr_pts = int(round(atr * atr_sl_mult))
        bonus = 0
        if squeeze_ratio > squeeze_widen_atr:
            bonus = min_sl_pts_bonus
        sl_pts = max(sl_atr_pts, calc_sl(symbol, atr, params) + bonus)
        return {
            "direction": "SELL",
            "sl_pts": int(sl_pts),
            "info": {
                "rationale": "AGI4_WSP squeeze reversion short",
                "squeeze_ratio": round(squeeze_ratio, 3),
                "adx": round(adx_now, 1),
                "adx_prev": round(adx_prev, 1) if adx_prev is not None else None,
                "rsi": round(rsi, 1),
                "struct_above_ema200": bool(struct_above),
                "bb_pos": "upper_half" if close >= bb_mid else "below_mid",
                "candle_body_atr": round(candle_body / atr, 2),
                "atr": round(atr, 2),
            },
        }

    return None
