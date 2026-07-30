"""AGI4_WSP_123258 — WSP M15 híbrido Bollinger-RSI-ADX com filtro EMA200.

Estratégia nova para WSP_M15 (busca nas 30 existentes não achou lucro).
Abordagem diferente: em vez de tentar combos simples, usa regime filter
(EMA200 slope + ADX) para distinguir trending vs ranging, e dentro de
cada regime opera pullbacks nas Bandas de Bollinger com confirmação RSI.
"""

STRATEGY_NAME = "AGI4_WSP_123258"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    # --- guards ---
    if not bars or len(bars) < 60 or atr is None or price is None:
        return None
    try:
        rsi_p = int(params.get("rsi_period", 7))
        bb_p = int(params.get("bb_period", 18))
        bb_std = float(params.get("bb_std", 1.6))
        ema_fast_p = int(params.get("ema_fast", 21))
        ema_slow_p = int(params.get("ema_slow", 200))
        adx_p = int(params.get("adx_period", 14))
        adx_trend = float(params.get("adx_trend", 22.0))
        adx_range = float(params.get("adx_range", 18.0))
        rsi_ob = float(params.get("rsi_overbought", 72.0))
        rsi_os = float(params.get("rsi_oversold", 28.0))
        risk_pts = params.get("risk_pts_target")
    except (TypeError, ValueError):
        return None
    if risk_pts is None:
        risk_pts = int(round(atr * 1.5)) if atr else 200
    else:
        risk_pts = int(risk_pts)

    calc_sl = utils.get("calc_sl")
    if calc_sl is None:
        return None

    try:
        rsi = utils["calculate_rsi"](bars, rsi_p)
        bb = utils["calculate_bollinger"](bars, bb_p, bb_std)
        ema_fast = utils["calculate_ema"](bars, ema_fast_p)
        ema_slow = utils["calculate_ema"](bars, ema_slow_p)
        adx_series = utils["calculate_adx"](bars, adx_p)
    except Exception:
        return None

    if len(bars) < ema_slow_p + 5:
        return None

    # --- regime filter ---
    slow_now = ema_slow[-1]
    slow_prev = ema_slow[-ema_fast_p] if len(ema_slow) > ema_fast_p else slow_now
    slow_slope = slow_now - slow_prev
    adx_now = adx_series[-1]

    if adx_now >= adx_trend and abs(slow_slope) > 0:
        regime = "trend_up" if slow_slope > 0 else "trend_down"
    elif adx_now <= adx_range:
        regime = "range"
    else:
        regime = "no_trade"

    if regime == "no_trade":
        return None

    ema_fast_now = ema_fast[-1]
    bb_upper = bb["upper"][-1]
    bb_lower = bb["lower"][-1]
    bb_mid = bb["middle"][-1]
    rsi_now = rsi[-1]

    direction = None
    trigger = None

    if regime == "trend_up" and ema_fast_now > bb_mid:
        # pullback para lower band ou middle em uptrend
        if price <= bb_mid and rsi_now < rsi_ob - 5:
            direction = "BUY"
            trigger = "pullback_mid_uptrend"
        elif price <= bb_lower * 1.001 and rsi_now < rsi_os + 10:
            direction = "BUY"
            trigger = "lower_band_uptrend"

    elif regime == "trend_down" and ema_fast_now < bb_mid:
        if price >= bb_mid and rsi_now > rsi_ob - 5 + (rsi_ob - (100 - rsi_ob + 5)):
            direction = "SELL"
            trigger = "pullback_mid_downtrend"
        elif price >= bb_upper * 0.999 and rsi_now > 100 - (rsi_os + 10):
            direction = "SELL"
            trigger = "upper_band_downtrend"

    elif regime == "range":
        # mean reversion nas bandas
        if price <= bb_lower and rsi_now < rsi_os:
            direction = "BUY"
            trigger = "range_lower_band"
        elif price >= bb_upper and rsi_now > rsi_ob:
            direction = "SELL"
            trigger = "range_upper_band"

    if direction is None:
        return None

    sl_pts = calc_sl(symbol, atr, params)
    if sl_pts is None or sl_pts <= 0:
        return None

    return {
        "direction": direction,
        "sl_pts": int(sl_pts),
        "info": {
            "regime": regime,
            "trigger": trigger,
            "rsi": round(float(rsi_now), 2),
            "adx": round(float(adx_now), 2),
            "slow_slope": round(float(slow_slope), 2),
            "bb_upper": round(float(bb_upper), 2),
            "bb_lower": round(float(bb_lower), 2),
            "bb_mid": round(float(bb_mid), 2),
            "ema_fast": round(float(ema_fast_now), 2),
            "ema_slow": round(float(slow_now), 2),
            "atr": round(float(atr), 2),
            "risk_pts_target": int(risk_pts),
            "tf": tf,
            "bar_ts": bar_ts,
        },
    }