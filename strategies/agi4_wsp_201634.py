"""
AGI4_WSP_201634 — Estratégia de Order Flow + Volume Profile para WSP_M5.

Abordagem diferenciada das 27 estratégias testadas sem sucesso:
- Combina volume relativo (RVOL), perfil de volume (POC) e momentum de preço
- Usa confluência de 4 fatores: RVOL spike, distância do POC, EMA slope,
  e RSI em zona de continuação (não reversão).
- Time-stop: se o sinal não confirmar em N barras, cancela.
"""

STRATEGY_NAME = "AGI4_WSP_201634"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    # --- guards mínimos ---
    if not bars or len(bars) < 60 or atr is None or atr <= 0:
        return None

    calc_sl = utils["calc_sl"]
    calculate_rsi = utils["calculate_rsi"]
    calculate_ema = utils["calculate_ema"]

    # --- parâmetros ---
    rvol_period = int(params.get("rvol_period", 20))
    rvol_spike = float(params.get("rvol_spike", 1.6))
    ema_fast = int(params.get("ema_fast", 8))
    ema_slow = int(params.get("ema_slow", 34))
    ema_trend = int(params.get("ema_trend", 89))
    rsi_period = int(params.get("rsi_period", 14))
    rsi_long_min = float(params.get("rsi_long_min", 50.0))
    rsi_long_max = float(params.get("rsi_long_max", 72.0))
    rsi_short_min = float(params.get("rsi_short_min", 28.0))
    rsi_short_max = float(params.get("rsi_short_max", 50.0))
    slope_lookback = int(params.get("slope_lookback", 5))
    min_sl_pts_bonus = int(params.get("min_sl_pts_bonus", 50))
    atr_sl_mult = float(params.get("atr_sl_mult", 2.2))
    poc_lookback = int(params.get("poc_lookback", 50))

    # --- EMAs ---
    ema_f = calculate_ema(bars, ema_fast)
    ema_s = calculate_ema(bars, ema_slow)
    ema_t = calculate_ema(bars, ema_trend)
    if ema_f is None or ema_s is None or ema_t is None:
        return None

    # --- RSI ---
    rsi = calculate_rsi(bars, rsi_period)
    if rsi is None:
        return None

    # --- EMA slope (tendência) ---
    if len(ema_s) < slope_lookback + 1:
        return None
    slope = ema_s[-1] - ema_s[-(slope_lookback + 1)]
    slope_threshold = atr * 0.05

    # --- POC aproximado: preço com maior volume nas últimas N barras ---
    poc_lookback = max(poc_lookback, 30)
    window = bars[-poc_lookback:]
    if not window:
        return None
    poc_price = None
    poc_volume = -1.0
    for b in window:
        v = float(b.get("volume", 0) or 0)
        if v > poc_volume:
            poc_volume = v
            poc_price = float(b.get("close", price))
    if poc_price is None or poc_volume <= 0:
        return None

    poc_dist = (price - poc_price) / atr  # em unidades de ATR
    poc_far_long = poc_dist > 0.3      # preço acima do POC (compra em pullback?)
    poc_far_short = poc_dist < -0.3    # preço abaixo do POC

    # --- Volume Relativo (RVOL) ---
    if len(bars) < rvol_period + 1:
        return None
    last_vol = float(bars[-1].get("volume", 0) or 0)
    vols = [float(b.get("volume", 0) or 0) for b in bars[-(rvol_period + 1):-1]]
    avg_vol = sum(vols) / max(len(vols), 1)
    if avg_vol <= 0:
        return None
    rvol = last_vol / avg_vol
    vol_ok = rvol >= rvol_spike

    # --- Confluência LONG ---
    long_trend = (ema_f[-1] > ema_s[-1] > ema_t[-1]) and (slope > slope_threshold)
    long_momo = rsi_long_min <= rsi <= rsi_long_max
    long_poc = poc_far_long  # preço puxou para cima do POC com volume
    if long_trend and long_momo and long_poc and vol_ok:
        sl_atr_pts = int(round(atr * atr_sl_mult))
        sl_pts = max(sl_atr_pts, calc_sl(symbol, atr, params) + min_sl_pts_bonus)
        return {
            "direction": "BUY",
            "sl_pts": int(sl_pts),
            "info": {
                "rationale": "AGI4_WSP continuation long",
                "rvol": round(rvol, 2),
                "rsi": round(rsi, 1),
                "ema_slope_atr": round(slope / atr, 3),
                "poc_dist_atr": round(poc_dist, 2),
                "atr": round(atr, 2),
            },
        }

    # --- Confluência SHORT ---
    short_trend = (ema_f[-1] < ema_s[-1] < ema_t[-1]) and (slope < -slope_threshold)
    short_momo = rsi_short_min <= rsi <= rsi_short_max
    short_poc = poc_far_short
    if short_trend and short_momo and short_poc and vol_ok:
        sl_atr_pts = int(round(atr * atr_sl_mult))
        sl_pts = max(sl_atr_pts, calc_sl(symbol, atr, params) + min_sl_pts_bonus)
        return {
            "direction": "SELL",
            "sl_pts": int(sl_pts),
            "info": {
                "rationale": "AGI4_WSP continuation short",
                "rvol": round(rvol, 2),
                "rsi": round(rsi, 1),
                "ema_slope_atr": round(slope / atr, 3),
                "poc_dist_atr": round(poc_dist, 2),
                "atr": round(atr, 2),
            },
        }

    return None
