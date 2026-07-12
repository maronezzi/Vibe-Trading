"""
Wave 13 (Bruno 2026-07-12) — IND_INSTITUTIONAL_SELL.

Hipótese de mercado (sem dependência em PnL passado):

  Setup institucional de baixa construído em confluência de 4 sinais:
    1) Tendência primária de baixa: EMA_fast < EMA_slow E -DI > +DI E ADX > 25;
    2) Pullback à VWAP confirmado: barra anterior fechou ACIMA da VWAP (tocou)
       e barra atual fechou ABAIXO da VWAP (rompimento pra baixo);
    3) RSI em zona neutra-leve (35-55, não oversold extremo);
    4) Volume climax na barra do sinal (>1.4x média 20);

  Janela 10h30-15h30 BRT (pico institucional).

  SELL-only — um setup direcional dedicado a operações de venda onde
  confluência (VWAP + ADX + volume + RSI) reduz a probabilidade de
  reversão surpresa. É o oposto complementar de uma versão BUY espelhada
  (ainda não escrita), mantendo a integração simples.

  Diferencial vs variantes existentes:
    - VWAP_EXTREME_REVERSION (mean-reversion pura em desvio);
    - PIVOT_POINTS (pivôs diários, sem VWAP dinâmica nem volume);
    - RSI_REVERSION (RSI isolado, sem ADX nem VWAP);
    - DIVERGENCE_RSI (divergência, sem VWAP nem volume climax);

  IMPORTANTE: este arquivo NÃO usa trades passados. Validação obrigatória via
  optimization/vt_forward_backtest.py::simulate_forward() sobre barras brutas
  MT5. Não ative em vt_config.json sem walk-forward positivo.

Parâmetros (defaults):
  ema_fast=9, ema_slow=21, adx_period=14, adx_min=25,
  rsi_period=14, rsi_pullback_low=35, rsi_pullback_high=55,
  vwap_period=30, vwap_touch_atr=0.4,
  volume_mult=1.4, volume_avg_period=20,
  hour_start=10, hour_start_minute=30, hour_end=15, hour_end_minute=30,
  sl_atr_mult=1.3
"""

from datetime import datetime, timezone, timedelta

STRATEGY_NAME = "IND_INSTITUTIONAL_SELL"


def _bar_dt_brt(bar_ts):
    try:
        ts = float(bar_ts)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(
        timezone(timedelta(hours=-3))
    )


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Pullback à VWAP em tendência de baixa institucional."""
    if not bars or len(bars) < 30 or atr <= 0:
        return None

    # 1) Janela 10:30-15:30
    dt = _bar_dt_brt(bar_ts)
    if dt is None:
        return None
    minute_of_day = dt.hour * 60 + dt.minute
    start_min = params.get("hour_start", 10) * 60 + params.get("hour_start_minute", 30)
    end_min = params.get("hour_end", 15) * 60 + params.get("hour_end_minute", 30)
    if not (start_min <= minute_of_day <= end_min):
        return None

    # 2) Trend de baixa forte
    adx_val, plus_di, minus_di = utils["calculate_adx"](bars, params.get("adx_period", 14))
    if adx_val < params.get("adx_min", 25):
        return None
    ema_fast = utils["calculate_ema"](bars, params.get("ema_fast", 9))
    ema_slow = utils["calculate_ema"](bars, params.get("ema_slow", 21))
    if ema_fast == 0 or ema_slow == 0:
        return None
    trend_bear = ema_fast < ema_slow and minus_di > plus_di
    if not trend_bear:
        return None

    # 3) Pullback à VWAP confirmado
    vwap = utils["calculate_vwap"](bars, params.get("vwap_period", 30))
    if vwap == 0:
        return None

    vwap_touch_atr = params.get("vwap_touch_atr", 0.4)
    # Barra atual fechou ABAIXO da VWAP (após pullback)
    if bars[0].get("close", 0) >= vwap:
        return None
    # Distância da VWAP > 0.4 ATR (não está muito colado)
    dist_atr = (vwap - bars[0].get("close", 0)) / atr
    if dist_atr > vwap_touch_atr:
        return None  # muito longe da VWAP — não é pullback
    # Barra anterior TOCOU a VWAP (fechou acima)
    if bars[1].get("close", 0) < vwap:
        return None

    # 4) RSI em zona pullback (35-55)
    rsi = utils["calculate_rsi"](bars, params.get("rsi_period", 14))
    if rsi < params.get("rsi_pullback_low", 35) or rsi > params.get("rsi_pullback_high", 55):
        return None

    # 5) Volume climax na barra atual (entrada institucional)
    vol_avg = params.get("volume_avg_period", 20)
    if len(bars) < vol_avg:
        return None
    recent_vol = bars[0].get("tick_volume", 0)
    avg_vol = sum(b.get("tick_volume", 0) for b in bars[1:vol_avg + 1]) / vol_avg
    if avg_vol <= 0 or recent_vol < avg_vol * params.get("volume_mult", 1.4):
        return None

    sl_pts = utils["calc_sl"](price, atr, params.get("sl_atr_mult", 1.3), "SELL")
    return {"direction": "SELL", "sl_pts": sl_pts,
            "info": {"edge": "ind_inst_sell", "vwap": round(vwap, 2),
                     "vwap_dist_atr": round(dist_atr, 2),
                     "adx": round(adx_val, 1), "rsi": round(rsi, 1),
                     "vol_ratio": round(recent_vol / avg_vol, 2)}}
