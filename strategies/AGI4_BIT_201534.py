"""
AGI4_BIT_201534 — Estratégia híbrida adaptativa para BIT_H1.

Combina três eixos ortogonais que nenhuma das 30 estratégias tentadas
explora em conjunto:

  1. Regime via ADX + inclinação EMA200: classifica o mercado em
     TREND_UP / TREND_DOWN / RANGE / VOLATILE antes de qualquer sinal.
  2. Entrada direcional via cruzamento EMA9/EMA21 confirmado por RSI
     em zona favorável (50–72 para compra, 28–50 para venda).
  3. Filtro de Bollinger: em RANGE exige toque/piercing de banda;
     em TREND exige distância mínima das bandas para evitar pullback
     contra-tendência.

A justificativa é que todas as estratégias anteriores atacam UM eixo.
Esta combina regime + direção + filtro para reduzir sinais falsos.
"""

STRATEGY_NAME = "AGI4_BIT_201534"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    if bars is None or len(bars) < 210:
        return None

    # Parâmetros configuráveis (defaults calibrados para cripto H1)
    ema_fast = int(params.get("ema_fast", 9))
    ema_slow = int(params.get("ema_slow", 21))
    ema_reg = int(params.get("ema_regime", 200))
    rsi_period = int(params.get("rsi_period", 14))
    adx_period = int(params.get("adx_period", 14))
    bb_period = int(params.get("bb_period", 20))
    bb_std = float(params.get("bb_std", 2.0))

    adx_trend_min = float(params.get("adx_trend_min", 22.0))
    adx_strong = float(params.get("adx_strong", 30.0))
    rsi_buy_min = float(params.get("rsi_buy_min", 50.0))
    rsi_buy_max = float(params.get("rsi_buy_max", 72.0))
    rsi_sell_min = float(params.get("rsi_sell_min", 28.0))
    rsi_sell_max = float(params.get("rsi_sell_max", 50.0))
    bb_touch_pct = float(params.get("bb_touch_pct", 0.001))
    bb_dist_trend_pct = float(params.get("bb_dist_trend_pct", 0.003))
    min_sl_pts = int(params.get("min_sl_pts", 80))
    max_sl_pts = int(params.get("max_sl_pts", 600))

    # SL calculado por utils conforme LEI 3
    calc_sl = utils["calc_sl"]
    sl_pts = int(calc_sl(symbol, atr, params))

    if sl_pts < min_sl_pts or sl_pts > max_sl_pts:
        return None

    # --- Eixo 1: Regime via ADX + inclinação EMA200 ---
    if len(bars) <= ema_reg + 5:
        return None
    ema_r = utils["calculate_ema"](bars, ema_reg)
    ema_r_prev = utils["calculate_ema"](bars[:-5], ema_reg)
    if ema_r is None or ema_r_prev is None:
        return None
    slope = (ema_r - ema_r_prev) / max(ema_r_prev, 1e-9)

    adx_raw = utils["calculate_adx"](bars, adx_period)
    if adx_raw is None:
        return None
    if isinstance(adx_raw, (list, tuple)):
        adx_val = float(adx_raw[-1])
    elif hasattr(adx_raw, "__getitem__") and not isinstance(adx_raw, (int, float)):
        try:
            adx_val = float(adx_raw[-1])
        except (TypeError, KeyError):
            adx_val = float(adx_raw)
    else:
        adx_val = float(adx_raw)

    if adx_val >= adx_strong:
        regime = "VOLATILE"
    elif adx_val >= adx_trend_min:
        regime = "TREND_UP" if slope > 0 else "TREND_DOWN"
    else:
        regime = "RANGE"

    # --- Indicadores de direção e filtro ---
    ema_f = utils["calculate_ema"](bars, ema_fast)
    ema_s = utils["calculate_ema"](bars, ema_slow)
    rsi = utils["calculate_rsi"](bars, rsi_period)
    bb = utils["calculate_bollinger"](bars, bb_period, bb_std)
    if None in (ema_f, ema_s, rsi) or bb is None:
        return None
    bb_mid, bb_upper, bb_lower = bb
    if None in (bb_mid, bb_upper, bb_lower):
        return None

    # Cruzamento real: comparar EMA atual vs anterior
    if len(bars) < ema_slow + 2:
        return None
    ema_f_prev = utils["calculate_ema"](bars[:-1], ema_fast)
    ema_s_prev = utils["calculate_ema"](bars[:-1], ema_slow)
    if ema_f_prev is None or ema_s_prev is None:
        return None

    bull_cross = ema_f_prev <= ema_s_prev and ema_f > ema_s
    bear_cross = ema_f_prev >= ema_s_prev and ema_f < ema_s

    # --- Eixo 3: Filtro Bollinger ---
    band_width = bb_upper - bb_lower
    if band_width <= 0:
        return None
    price_in_lower_pct = (price - bb_lower) / band_width
    price_in_upper_pct = (bb_upper - price) / band_width

    direction = None
    info = {
        "regime": regime,
        "adx": round(adx_val, 2),
        "ema_slope": round(slope, 6),
        "rsi": round(rsi, 2),
        "ema_fast": round(ema_f, 4),
        "ema_slow": round(ema_s, 4),
        "ema_regime": round(ema_r, 4),
        "bb_width": round(band_width, 4),
    }

    if regime == "TREND_UP" and bull_cross:
        if rsi < rsi_buy_min or rsi > rsi_buy_max:
            return None
        # Tendência: exigir distância da banda superior (não comprar topo)
        if price_in_upper_pct < bb_dist_trend_pct:
            return None
        direction = "BUY"

    elif regime == "TREND_DOWN" and bear_cross:
        if rsi > rsi_sell_max or rsi < rsi_sell_min:
            return None
        # Tendência de baixa: exigir distância da banda inferior
        if price_in_lower_pct < bb_dist_trend_pct:
            return None
        direction = "SELL"

    elif regime == "RANGE":
        # Em range: mean reversion nos extremos das bandas
        if rsi < (rsi_sell_min + 2) and price_in_lower_pct <= bb_touch_pct and bear_cross:
            direction = "SELL"
        elif rsi > (rsi_buy_max - 2) and price_in_upper_pct <= bb_touch_pct and bull_cross:
            direction = "BUY"

    elif regime == "VOLATILE":
        # Em alta volatilidade só opera a favor da EMA200 com ADX muito forte
        if adx_val < (adx_strong + 5):
            return None
        if slope > 0 and bull_cross and rsi > 55 and rsi < 75:
            direction = "BUY"
        elif slope < 0 and bear_cross and rsi < 45 and rsi > 25:
            direction = "SELL"

    if direction is None:
        return None

    info["signal_type"] = f"{regime}_{direction}"
    info["bb_pos"] = round(price_in_lower_pct, 4)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": info,
    }
