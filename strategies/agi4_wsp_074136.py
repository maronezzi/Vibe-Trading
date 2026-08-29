"""
AGI4_WSP_074136 — Estratégia "VWAP Slope + Bollinger Squeeze Breakout" para WSP_M30.

Abordagem nova: nenhum dos 27 pares anteriores (ADX_TREND, BOLLINGER, CANDLE_PATTERNS,
DIVERGENCE_RSI, DONCHIAN_BREAKOUT, EMA_CROSSOVER, EMA_PULLBACK, ENHANCED_BOLLINGER,
ENHANCED_MACD_MOMENTUM, ENHANCED_RSI_REVERSION, FIBONACCI_RETRACEMENT, HEIKIN_ASHI,
ICHIMOKU, KELTNER_CHANNEL, MACD_MOMENTUM, MEAN_REVERSION_ZSCORE, MOMENTUM_BREAKOUT,
PIVOT_POINTS, RANGE_TRADING, RSI_REVERSION, SMART_EMA, STRONG_TREND, SUPERTREND,
TRIPLE_EMA, VOLATILITY_BREAKOUT, VWAP, WIN_REVERSION) combina os três eixos aqui:

  1. Squeeze de Bollinger (largura relativa < limiar mínimo) → consolidação detectada.
  2. Slope da VWAP (ΔVWAP entre barras recentes) confirma a DIREÇÃO do novo regime.
  3. Breakout de banda com ADX>adx_min garante que a expansão é tendência, não ruído.

Inspiração: WSP em M30 é choppy e os breakouts falsos destroem edge em DONCHIAN/MOMENTUM.
Squeeze-release exige que a VWAP esteja inclinada na direção do breakout — sem isso o
sinal é descartado. O TP fica na banda oposta de Bollinger (alvo de expansão), o SL
na banda de entrada (risco definido pela própria volatilidade recente) com piso em
calc_sl do autotrader (1.5×ATR para WSP).

Lógica:
  width    = (bb_upper - bb_lower) / bb_mid          # largura relativa do BB
  squeeze  = width < squeeze_max                    # banda apertada
  slope    = vwap_now - vwap_prev                   # ΔVWAP entre barras
  adx_now  = ADX(period)
  buy_ok   = squeeze AND slope > slope_min AND adx_now > adx_min AND price > bb_upper
  sell_ok  = squeeze AND slope < -slope_min AND adx_now > adx_min AND price < bb_lower

  SL = max(bb_width_pts, calc_sl(symbol, atr, params))
  TP = banda oposta de BB (projeção de expansão simétrica)
"""

STRATEGY_NAME = "AGI4_WSP_074136"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    # --- guards mínimos ---
    if not bars or len(bars) < 40 or atr is None or atr <= 0:
        return None
    if price is None or price <= 0:
        return None

    calc_sl = utils["calc_sl"]
    calculate_vwap = utils["calculate_vwap"]
    calculate_bollinger = utils["calculate_bollinger"]
    calculate_adx = utils["calculate_adx"]

    # --- parâmetros (todos via params.get c/ default) ---
    bb_period = int(params.get("bb_period", 20))
    bb_std = float(params.get("bb_std", 2.0))
    vwap_period = int(params.get("vwap_period", 20))
    squeeze_max = float(params.get("squeeze_max", 0.012))     # 1.2% largura relativa
    slope_min = float(params.get("slope_min", 0.0))            # ΔVWAP mínimo (preço unit.)
    adx_period = int(params.get("adx_period", 14))
    adx_min = float(params.get("adx_min", 20.0))
    min_history = int(params.get("min_history", 5))            # barras p/ medir slope

    # --- 1) Bollinger: detecta squeeze e define alvos de TP/SL ---
    bb = calculate_bollinger(bars, bb_period, bb_std)
    if not bb or len(bb) != 3:
        return None
    bb_upper, bb_mid, bb_lower = bb
    if bb_mid is None or bb_mid <= 0 or bb_upper is None or bb_lower is None:
        return None

    bb_width = bb_upper - bb_lower
    if bb_width <= 0:
        return None
    rel_width = bb_width / bb_mid
    squeeze = rel_width < squeeze_max

    # --- 2) Slope da VWAP (Δ entre barras para filtrar direção do breakout) ---
    vwap_now = calculate_vwap(bars, vwap_period)
    if vwap_now is None or vwap_now <= 0:
        return None
    if len(bars) < min_history + 1:
        return None
    vwap_prev = calculate_vwap(bars[:-min_history], vwap_period)
    if vwap_prev is None or vwap_prev <= 0:
        return None
    slope = vwap_now - vwap_prev

    # --- 3) ADX: confirma que o breakout carrega tendência, não ruído ---
    adx_pack = calculate_adx(bars, adx_period)
    if not adx_pack or len(adx_pack) != 3:
        return None
    adx_now, plus_di, minus_di = adx_pack
    if adx_now is None or adx_now <= 0:
        return None

    # --- 4) Decisão ---
    direction = None
    info_extra = {}

    if squeeze and slope > slope_min and adx_now > adx_min and price > bb_upper:
        direction = "BUY"
        info_extra["vwap_slope"] = round(slope, 4)
        info_extra["plus_di"] = round(plus_di, 1) if plus_di is not None else None
        info_extra["minus_di"] = round(minus_di, 1) if minus_di is not None else None
        info_extra["tp_pts"] = int(round(price + bb_width))            # TP: banda oposta simétrica
        sl_pts_band = int(round(price - bb_lower))                      # SL: banda inferior
    elif squeeze and slope < -slope_min and adx_now > adx_min and price < bb_lower:
        direction = "SELL"
        info_extra["vwap_slope"] = round(slope, 4)
        info_extra["plus_di"] = round(plus_di, 1) if plus_di is not None else None
        info_extra["minus_di"] = round(minus_di, 1) if minus_di is not None else None
        info_extra["tp_pts"] = int(round(bb_upper - price))            # TP: banda superior simétrica
        sl_pts_band = int(round(bb_upper - price))                      # SL: banda superior
    else:
        return None

    # --- 5) SL final: piso é calc_sl do autotrader; teto é a banda oposta ---
    sl_pts_base = calc_sl(symbol, atr, params)
    sl_pts = max(int(sl_pts_band), int(sl_pts_base))

    return {
        "direction": direction,
        "sl_pts": int(sl_pts),
        "info": {
            "rationale": "AGI4_WSP vwap_slope_bb_squeeze_breakout",
            "bb_upper": round(bb_upper, 2),
            "bb_mid": round(bb_mid, 2),
            "bb_lower": round(bb_lower, 2),
            "bb_rel_width": round(rel_width, 5),
            "vwap_now": round(vwap_now, 2),
            "vwap_prev": round(vwap_prev, 2),
            "adx": round(adx_now, 1),
            "squeeze": bool(squeeze),
            "sl_band_pts": int(sl_pts_band),
            "sl_floor_pts": int(sl_pts_base),
            "atr": round(atr, 2),
            **info_extra,
        },
    }
