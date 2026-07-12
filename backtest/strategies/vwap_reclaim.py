"""
Wave 13 (Bruno 2026-07-12) — VWAP_RECLAIM.

Hipótese de mercado (sem dependência em PnL passado):

  Operar a CONTINUAÇÃO (não reversão) quando o preço RECUPERA a VWAP após um
  desvio institucional. O VWAP funciona como ímã: choques rompem o VWAP, e
  quando o preço retorna, há continuação do momentum original.

  Lógica:
    1) Detecta desvio significativo nas últimas N barras (>1.5 ATR acima ou abaixo);
    2) Confirma RECLAIM: preço atual fechou DE VOLTA dentro de ±0.5 ATR do VWAP;
    3) Exige volume climax (>1.3x média 20) no reclaim;
    4) ADX > 18 (regime direcional, não chop);
    5) Direção alinhada com o sinal do reclaim (DI confirma tendência).

  Janela 10h-15h30 BRT.

  Diferencial:
    - VWAP_EXTREME_REVERSION (existente): opera CONTRA o desvio (reversal);
    - VWAP_VALUE_AREA (existente): opera mean-reversion nas bandas ±1σ;
    - Esta: opera COM a direção do reclaim — continuation institucional.

  IMPORTANTE: este arquivo NÃO usa trades passados. Validação obrigatória via
  optimization/vt_forward_backtest.py::simulate_forward() sobre barras brutas
  MT5. Não ative em vt_config.json sem walk-forward positivo.

Parâmetros (defaults):
  vwap_period=30, deviation_atr_mult=1.5, reclaim_atr_mult=0.5,
  lookback=20, adx_period=14, adx_min=18,
  volume_mult=1.3, volume_avg_period=20,
  hour_start=10, hour_end=15, hour_end_minute=30,
  sl_atr_mult=1.4
"""

from datetime import datetime, timezone, timedelta

STRATEGY_NAME = "VWAP_RECLAIM"


def _bar_dt_brt(bar_ts):
    try:
        ts = float(bar_ts)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(
        timezone(timedelta(hours=-3))
    )


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Reclaim de VWAP após desvio — continuation signal."""
    if not bars or len(bars) < 30 or atr <= 0:
        return None

    # 1) Janela: 10:00-15:30 BRT (depois do caos matinal)
    dt = _bar_dt_brt(bar_ts)
    if dt is None:
        return None
    minute_of_day = dt.hour * 60 + dt.minute
    if minute_of_day < params.get("hour_start", 10) * 60:
        return None
    if minute_of_day > params.get("hour_end", 15) * 60 + params.get("hour_end_minute", 30):
        return None

    # 2) VWAP atual
    vwap_period = params.get("vwap_period", 30)
    vwap = utils["calculate_vwap"](bars, vwap_period)
    if vwap == 0:
        return None

    # 3) Lookback: houve desvio significativo nas últimas N barras
    lookback = params.get("lookback", 20)
    if len(bars) < lookback + 1:
        return None

    deviation_mult = params.get("deviation_atr_mult", 1.5)
    reclaim_mult = params.get("reclaim_atr_mult", 0.5)

    # Calcular distância máxima de VWAP nas últimas lookback barras
    max_dist_above = 0.0
    max_dist_below = 0.0
    for i in range(1, lookback + 1):
        if i >= len(bars):
            break
        close_i = bars[i].get("close", 0)
        dist = (close_i - vwap) / atr if atr > 0 else 0
        if dist > max_dist_above:
            max_dist_above = dist
        if -dist > max_dist_below:
            max_dist_below = -dist

    # 4) Distância atual da VWAP (em ATR)
    current_close = bars[0].get("close", 0)
    current_dist = (current_close - vwap) / atr if atr > 0 else 0

    # 5) Houve desvio significativo E houve reclaim?
    had_above_deviation = max_dist_above > deviation_mult
    had_below_deviation = max_dist_below > deviation_mult

    # Reclaim: preço voltou dentro de ±reclaim_mult da VWAP
    reclaimed_above_to_below = had_above_deviation and abs(current_dist) <= reclaim_mult
    reclaimed_below_to_above = had_below_deviation and abs(current_dist) <= reclaim_mult

    if not (reclaimed_above_to_below or reclaimed_below_to_above):
        return None

    # 6) ADX regime direcional
    adx_val, plus_di, minus_di = utils["calculate_adx"](bars, params.get("adx_period", 14))
    if adx_val < params.get("adx_min", 18):
        return None

    # 7) Volume spike (confirma reclaim)
    vol_avg = params.get("volume_avg_period", 20)
    if len(bars) < vol_avg:
        return None
    recent_vol = bars[0].get("tick_volume", 0)
    avg_vol = sum(b.get("tick_volume", 0) for b in bars[1:vol_avg + 1]) / vol_avg
    if avg_vol <= 0 or recent_vol < avg_vol * params.get("volume_mult", 1.3):
        return None

    # 8) Direção:
    # Se preço veio de CIMA da VWAP (max_dist_above > 0) e agora está na VWAP
    #   → BUY (continuation da tendência de baixa que varreu a VWAP)
    # Na verdade, sem info da tendência prévia, optamos pelo sinal mais conservador:
    #   → direção da tendência mais recente (compare with current VWAP)
    # Heurística: reclaimed_above_to_below significa que preço ESTAVA acima e caiu
    #             → SELL continuation da queda (VWAP virou resistência)
    #             reclaimed_below_to_above → BUY continuation da alta
    if had_above_deviation and reclaimed_above_to_below and minus_di > plus_di:
        sl_pts = utils["calc_sl"](price, atr, params.get("sl_atr_mult", 1.4), "SELL")
        return {"direction": "SELL", "sl_pts": sl_pts,
                "info": {"edge": "vwap_reclaim_bear", "vwap": round(vwap, 2),
                         "max_dev_atr": round(max_dist_above, 2),
                         "current_dev_atr": round(current_dist, 2),
                         "adx": round(adx_val, 1)}}

    if had_below_deviation and reclaimed_below_to_above and plus_di > minus_di:
        sl_pts = utils["calc_sl"](price, atr, params.get("sl_atr_mult", 1.4), "BUY")
        return {"direction": "BUY", "sl_pts": sl_pts,
                "info": {"edge": "vwap_reclaim_bull", "vwap": round(vwap, 2),
                         "max_dev_atr": round(max_dist_below, 2),
                         "current_dev_atr": round(current_dist, 2),
                         "adx": round(adx_val, 1)}}

    return None
