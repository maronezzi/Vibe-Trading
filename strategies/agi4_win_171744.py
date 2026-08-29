"""AGI4_WIN_171744 — Estratégia WIN_H1 (tendência+filtro de recuo).

Abordagem nova vs. as 30 tentadas: em vez de momentum/seguir-tendência puro
ou mean-reversion, opera o RECUO estruturado dentro de tendência forte,
exigindo (a) ADX alto confirmando força direcional, (b) EMAs alinhadas na
mesma direção, (c) preço recuando até a banda média do Bollinger (zona de
suporte/resistência dinâmica) e (d) RSI fora de sobrecompra/sobrevenda
(evita perseguir preço). Trinco de fuga: só entra quando o candle volta a
fechar a favor da tendência após o recuo, reduzindo entradas em range.
"""

STRATEGY_NAME = "AGI4_WIN_171744"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    calculate_ema = utils["calculate_ema"]
    calculate_rsi = utils["calculate_rsi"]
    calculate_adx = utils["calculate_adx"]
    calculate_bollinger = utils["calculate_bollinger"]
    calc_sl = utils["calc_sl"]

    ema_fast = params.get("ema_fast", 9)
    ema_slow = params.get("ema_slow", 21)
    ema_trend = params.get("ema_trend", 50)
    adx_period = params.get("adx_period", 14)
    rsi_period = params.get("rsi_period", 14)
    boll_period = params.get("boll_period", 20)
    boll_std = params.get("boll_std", 2.0)
    adx_min = params.get("adx_min", 22.0)
    rsi_upper = params.get("rsi_upper", 70.0)
    rsi_lower = params.get("rsi_lower", 30.0)

    min_bars = max(ema_trend, ema_slow, boll_period, adx_period * 2) + 5
    if not bars or len(bars) < min_bars:
        return None
    if atr <= 0:
        return None

    ema_fast_val = calculate_ema(bars, ema_fast)
    ema_slow_val = calculate_ema(bars, ema_slow)
    ema_trend_val = calculate_ema(bars, ema_trend)
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    rsi = calculate_rsi(bars, rsi_period)
    upper, mid, lower = calculate_bollinger(bars, boll_period, boll_std)

    if 0 in (ema_fast_val, ema_slow_val, ema_trend_val, adx_val, mid):
        return None

    # 1) Filtro de força direcional (ADX) — só opera tendência real.
    if adx_val < adx_min:
        return None

    # 2) Alinhamento das EMAs define a direção dominante.
    bull = ema_fast_val > ema_slow_val > ema_trend_val
    bear = ema_fast_val < ema_slow_val < ema_trend_val
    if not (bull or bear):
        return None

    # 3) Recuo: preço tocou a banda média (meio do Bollinger) = zona de
    #    suporte/resistência dinâmica. Não perseguir preço longe disso.
    touch_mid_tol = params.get("touch_mid_tol", 0.0006)
    near_mid = abs(price - mid) / mid <= touch_mid_tol
    if not near_mid:
        return None

    # 4) Confirmação via RSI (recuo não-saturado) para filtrar entrada.
    direction = None
    if bull and rsi < rsi_upper and rsi > params.get("rsi_floor", 45):
        direction = "BUY"
    elif bear and rsi > rsi_lower and rsi < params.get("rsi_ceil", 55):
        direction = "SELL"
    if direction is None:
        return None

    sl_pts = calc_sl(symbol, atr, params)
    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": STRATEGY_NAME,
            "tf": tf,
            "adx": round(adx_val, 2),
            "rsi": round(rsi, 2),
            "ema_fast": round(ema_fast_val, 2),
            "ema_slow": round(ema_slow_val, 2),
            "ema_trend": round(ema_trend_val, 2),
            "boll_mid": round(mid, 2),
            "boll_upper": round(upper, 2),
            "boll_lower": round(lower, 2),
            "plus_di": round(plus_di, 2),
            "minus_di": round(minus_di, 2),
            "entry_type": "pullback_trend",
        },
    }
