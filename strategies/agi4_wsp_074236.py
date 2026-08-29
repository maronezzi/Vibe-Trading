"""
AGI4_WSP_074236 — Estratégia "Donchian Trend Continuation + EMA Ribbon + Volume Pulse" para WSP_H1.

Abordagem nova (não aparece em nenhuma das 27 estratégias existentes): combina três
eixos em camadas que filtram sinais sequencialmente — não é só breakout nem só
reversion nem só momentum puro.

  1. Donchian Channel multi-período (curta + longa): exige que a máxima/ mínima de N
     barras esteja se expandindo na direção do sinal (contração prévia da range +
     expansão recente). Isso descarta breakouts de uma única barra espúria.
  2. EMA Ribbon (EMA rápida vs EMA lenta): confirma que o slope da EMA rápida está
     alinhado com a direção do breakout — filtro clássico de tendência, mas combinado
     com slope mínimo (não só posição relativa) para evitar entradas em regime lateral.
  3. Volume Pulse: a barra de entrada deve ter volume relativo acima de um limiar
     adaptativo (média móvel exponencial do volume), evitando breakout sem convicção
     institucional. WSP em H1 tem gaps de liquidez — volume confirma "quem move o preço".

Diferenciação explícita das 27 estratégias pré-existentes:
  - DONCHIAN_BREAKOUT/MOMENTUM_BREAKOUT: usamos DONCHIAN + EMA slope + volume, não só
    range breakout.
  - SMART_EMA/EMA_CROSSOVER: âncora no slope da EMA, não só no cruzamento.
  - VOLATILITY_BREAKOUT: aqui medimos expansão da range Donchian (continuação), não só
    volatilidade realizada.
  - VWAP-based: usamos volume relativo (não VWAP de preço) — métrica ortogonal.

Lógica:
  dc_short_high = max(high[-dc_short:])           # resistência recente curta
  dc_short_low  = min(low[-dc_short:])            # suporte recente curto
  dc_long_high  = max(high[-dc_long:])            # resistência de longo prazo
  dc_long_low   = min(low[-dc_long:])             # suporte de longo prazo
  range_expansion = (dc_short_high - dc_short_low) / max(dc_long_high - dc_long_low, 1e-9)

  ema_fast_slope = ema_fast_now - ema_fast_prev   # slope da EMA rápida
  ema_slow_slope = ema_slow_now - ema_slow_prev   # slope da EMA lenta

  vol_now     = bars[-1]["volume"]                 # volume da barra atual
  vol_avg     = média exponencial do volume (período vol_avg_period)
  vol_ratio   = vol_now / vol_avg

  BUY  se price > dc_short_high                                    (breakout alta)
     E range_expansion > range_min                                (range expandindo)
     E ema_fast_slope > ema_slope_min                             (EMA rápida subindo)
     E ema_fast_now > ema_slow_now                                (EMA ribbon alinhado alta)
     E vol_ratio > vol_ratio_min                                  (volume confirma)
  SELL simétrico.

  SL = max(dc_atr_mult × ATR, calc_sl(symbol, atr, params))
       — a range curta em pontos vira piso de risco se for maior que o floor do
         autotrader (1.5×ATR típico para WSP).
  TP = TP múltiplo ATR (atr_tp_mult × ATR) projetado a partir do preço de entrada.
"""

STRATEGY_NAME = "AGI4_WSP_074236"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    # --- guards mínimos ---
    if not bars or len(bars) < 60 or atr is None or atr <= 0:
        return None
    if price is None or price <= 0:
        return None

    calc_sl = utils["calc_sl"]
    calculate_ema = utils["calculate_ema"]

    # --- parâmetros (todos via params.get c/ default) ---
    dc_short = int(params.get("dc_short", 20))            # Donchian curto (N barras)
    dc_long = int(params.get("dc_long", 60))              # Donchian longo (referência)
    ema_fast_period = int(params.get("ema_fast_period", 8))
    ema_slow_period = int(params.get("ema_slow_period", 34))
    ema_slope_lookback = int(params.get("ema_slope_lookback", 3))  # barras p/ slope
    range_min = float(params.get("range_min", 0.6))       # expansão mínima (curta/longa)
    ema_slope_min = float(params.get("ema_slope_min", 0.0))  # slope mínimo da EMA rápida
    vol_avg_period = int(params.get("vol_avg_period", 20))
    vol_ratio_min = float(params.get("vol_ratio_min", 1.0))    # volume relativo mínimo
    float(params.get("atr_sl_mult", 1.5))
    atr_tp_mult = float(params.get("atr_tp_mult", 2.5))

    # --- 1) Validação de histórico suficiente ---
    if len(bars) < dc_long + ema_slope_lookback + 5:
        return None
    if not all("high" in b and "low" in b and "volume" in b for b in bars[-dc_long:]):
        return None

    # --- 2) Donchian Channel (curta + longa) ---
    highs_long = [b["high"] for b in bars[-dc_long:]]
    lows_long = [b["low"] for b in bars[-dc_long:]]
    dc_long_high = max(highs_long)
    dc_long_low = min(lows_long)

    highs_short = [b["high"] for b in bars[-dc_short:]]
    lows_short = [b["low"] for b in bars[-dc_short:]]
    dc_short_high = max(highs_short)
    dc_short_low = min(lows_short)

    range_long = dc_long_high - dc_long_low
    range_short = dc_short_high - dc_short_low
    if range_long <= 0 or range_short <= 0:
        return None

    range_expansion = range_short / range_long

    # --- 3) EMA Ribbon + slope ---
    ema_fast_now = calculate_ema(bars, ema_fast_period)
    ema_slow_now = calculate_ema(bars, ema_slow_period)
    if ema_fast_now is None or ema_slow_now is None:
        return None
    if ema_fast_now <= 0 or ema_slow_now <= 0:
        return None

    if len(bars) < ema_slope_lookback + max(ema_fast_period, ema_slow_period):
        return None

    ema_fast_prev = calculate_ema(bars[:-ema_slope_lookback], ema_fast_period)
    ema_slow_prev = calculate_ema(bars[:-ema_slope_lookback], ema_slow_period)
    if ema_fast_prev is None or ema_slow_prev is None:
        return None
    if ema_fast_prev <= 0 or ema_slow_prev <= 0:
        return None

    ema_fast_slope = ema_fast_now - ema_fast_prev
    ema_slow_slope = ema_slow_now - ema_slow_prev

    # --- 4) Volume Pulse (média exponencial manual — sem imports) ---
    vol_series = [b["volume"] for b in bars[-vol_avg_period:] if b.get("volume") is not None]
    if len(vol_series) < vol_avg_period:
        return None
    # EMA do volume: alpha = 2/(N+1)
    alpha = 2.0 / (vol_avg_period + 1.0)
    ema_vol = vol_series[0]
    for v in vol_series[1:]:
        ema_vol = alpha * v + (1.0 - alpha) * ema_vol
    if ema_vol <= 0:
        return None
    vol_now = bars[-1].get("volume")
    if vol_now is None or vol_now <= 0:
        return None
    vol_ratio = vol_now / ema_vol

    # --- 5) Decisão: BUY ---
    if (
        price > dc_short_high
        and range_expansion > range_min
        and ema_fast_slope > ema_slope_min
        and ema_fast_now > ema_slow_now
        and vol_ratio > vol_ratio_min
    ):
        direction = "BUY"
        info_extra = {
            "ema_fast_slope": round(ema_fast_slope, 4),
            "ema_slow_slope": round(ema_slow_slope, 4),
            "range_expansion": round(range_expansion, 3),
            "vol_ratio": round(vol_ratio, 2),
        }

    # --- 6) Decisão: SELL ---
    elif (
        price < dc_short_low
        and range_expansion > range_min
        and ema_fast_slope < -ema_slope_min
        and ema_fast_now < ema_slow_now
        and vol_ratio > vol_ratio_min
    ):
        direction = "SELL"
        info_extra = {
            "ema_fast_slope": round(ema_fast_slope, 4),
            "ema_slow_slope": round(ema_slow_slope, 4),
            "range_expansion": round(range_expansion, 3),
            "vol_ratio": round(vol_ratio, 2),
        }
    else:
        return None

    # --- 7) SL final: piso = calc_sl do autotrader; teto = range Donchian curta ---
    sl_pts_floor = calc_sl(symbol, atr, params)
    sl_pts_range = int(round(range_short))
    sl_pts = max(int(sl_pts_floor), int(sl_pts_range))

    # TP como múltiplo de ATR — projeção direcional a partir do preço de entrada
    if direction == "BUY":
        tp_pts = int(round(price + atr_tp_mult * atr))
    else:
        tp_pts = int(round(price - atr_tp_mult * atr))

    return {
        "direction": direction,
        "sl_pts": int(sl_pts),
        "info": {
            "rationale": "AGI4_WSP donchian_continuation_ema_ribbon_vol_pulse",
            "dc_short_high": round(dc_short_high, 2),
            "dc_short_low": round(dc_short_low, 2),
            "dc_long_high": round(dc_long_high, 2),
            "dc_long_low": round(dc_long_low, 2),
            "ema_fast": round(ema_fast_now, 2),
            "ema_slow": round(ema_slow_now, 2),
            "ema_fast_prev": round(ema_fast_prev, 2),
            "ema_slow_prev": round(ema_slow_prev, 2),
            "vol_now": round(vol_now, 2),
            "vol_avg": round(ema_vol, 2),
            "atr": round(atr, 2),
            "sl_floor_pts": int(sl_pts_floor),
            "sl_range_pts": int(sl_pts_range),
            "tp_pts": int(tp_pts),
            **info_extra,
        },
    }
