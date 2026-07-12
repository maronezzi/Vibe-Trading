"""
Wave 13 (Bruno 2026-07-12) — VOLATILITY_REGIME_TREND.

Hipótese de mercado (construída apenas sobre estrutura, sem DB):

  Mercados em TRENDING regime + VOL EXPANDING produzem as oportunidades mais
  robustas de trend-following. Em contraste, mercados em compressão de volatilidade
  com ADX baixo tendem a reverter à média. Esta estratégia captura o primeiro
  regime.

  Setup:
    1) ATR da barra atual > 1.1 × ATR média das últimas 20 barras
       (vol-expandindo: momentum institucional em curso);
    2) ADX > 22 (tendência direcional estabelecida);
    3) EMA rápida vs EMA lenta confirmando direção (+DI vs -DI);
    4) Volume climax (>1.2× média 20) confirmando participação institucional;
    5) RSI em zona direcional: >50 para compra, <50 para venda (não neutro);
    6) Janela 10h-16h BRT (depois do leilão matinal, antes do EOD);
    7) SL curto (1.2 ATR) deixando trailing absorver.

  Diferencial vs variantes existentes:
    - ADX_TREND (existente): exige tendência mas NÃO exige expansão de vol;
    - STRONG_TREND (existente): exige ADX alto + DI, sem vol expansion + volume;
    - TRAIL_HOLDERS_TREND (paralelo Wave 13): filtros parecidos mas exige
      pullback confirmado, este aqui detecta o start do regime.

  IMPORTANTE: este arquivo NÃO usa trades passados. Validação obrigatória via
  optimization/vt_forward_backtest.py::simulate_forward() sobre barras brutas
  MT5. Não ative em vt_config.json sem walk-forward positivo.

Parâmetros (defaults):
  atr_period=14, atr_avg_period=20, atr_ratio_min=1.1,
  adx_period=14, adx_min=22,
  ema_fast=9, ema_slow=21,
  volume_mult=1.2, volume_avg_period=20,
  rsi_period=14,
  hour_start=10, hour_end=16,
  sl_atr_mult=1.2
"""

from datetime import datetime, timezone, timedelta

STRATEGY_NAME = "VOLATILITY_REGIME_TREND"


def _bar_dt_brt(bar_ts):
    try:
        ts = float(bar_ts)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(
        timezone(timedelta(hours=-3))
    )


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Entry quando vol expande em regime trending direcional."""
    if not bars or len(bars) < 30 or atr <= 0:
        return None

    dt = _bar_dt_brt(bar_ts)
    if dt is None:
        return None
    minute_of_day = dt.hour * 60 + dt.minute
    if minute_of_day < params.get("hour_start", 10) * 60:
        return None
    if minute_of_day > params.get("hour_end", 16) * 60:
        return None

    # 1) ATR atual vs ATR média (vol expansion)
    atr_avg_period = params.get("atr_avg_period", 20)
    if len(bars) < atr_avg_period + 1:
        return None
    atr_values = []
    for i in range(1, atr_avg_period + 1):
        bar = bars[i]
        if isinstance(bar, dict):
            hi, lo, cl_prev = bar.get("high", 0), bar.get("low", 0), bar.get("close", 0)
            prev_close = bars[i + 1].get("close", cl_prev) if i + 1 < len(bars) else cl_prev
            tr = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
            atr_values.append(tr)
    if not atr_values:
        return None
    atr_avg = sum(atr_values) / len(atr_values)
    if atr_avg <= 0 or atr / atr_avg < params.get("atr_ratio_min", 1.1):
        return None

    # 2) ADX trending
    adx_val, plus_di, minus_di = utils["calculate_adx"](bars, params.get("adx_period", 14))
    if adx_val < params.get("adx_min", 22):
        return None

    # 3) EMA direction
    ema_fast = utils["calculate_ema"](bars, params.get("ema_fast", 9))
    ema_slow = utils["calculate_ema"](bars, params.get("ema_slow", 21))
    if ema_fast == 0 or ema_slow == 0:
        return None
    trend_bull = ema_fast > ema_slow and plus_di > minus_di
    trend_bear = ema_fast < ema_slow and minus_di > plus_di
    if not (trend_bull or trend_bear):
        return None

    # 4) Volume climax
    vol_avg_period = params.get("volume_avg_period", 20)
    if len(bars) < vol_avg_period:
        return None
    recent_vol = bars[0].get("tick_volume", 0)
    avg_vol = sum(bars[i].get("tick_volume", 0) for i in range(1, vol_avg_period + 1)) / vol_avg_period
    if avg_vol <= 0 or recent_vol < avg_vol * params.get("volume_mult", 1.2):
        return None

    # 5) RSI direcional (não-neutro)
    rsi = utils["calculate_rsi"](bars, params.get("rsi_period", 14))
    if rsi == 0:
        return None

    if trend_bull and rsi >= 50:
        sl_pts = utils["calc_sl"](price, atr, params.get("sl_atr_mult", 1.2), "BUY")
        return {
            "direction": "BUY",
            "sl_pts": sl_pts,
            "info": {
                "edge": "vol_regime_trend_buy",
                "atr_ratio": round(atr / atr_avg, 2),
                "adx": round(adx_val, 1),
                "rsi": round(rsi, 1),
            },
        }
    if trend_bear and rsi <= 50:
        sl_pts = utils["calc_sl"](price, atr, params.get("sl_atr_mult", 1.2), "SELL")
        return {
            "direction": "SELL",
            "sl_pts": sl_pts,
            "info": {
                "edge": "vol_regime_trend_sell",
                "atr_ratio": round(atr / atr_avg, 2),
                "adx": round(adx_val, 1),
                "rsi": round(rsi, 1),
            },
        }
    return None
