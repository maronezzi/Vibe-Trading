"""
Wave 13 (Bruno 2026-07-12) — VOLATILITY_MEAN_REVERSION.

Hipótese de mercado (construída apenas sobre estrutura, sem DB):

  Complementar ao VOLATILITY_REGIME_TREND: mercados em compressão de volatilidade
  + ADX baixo (RANGING regime) apresentam extremos de RSI que revertem à média
  com alta previsibilidade. Esta estratégia captura o regime oposto.

  Setup:
    1) ATR da barra atual < 0.9 × ATR média das últimas 20 barras
       (vol-contraindo: mercado lateral);
    2) ADX < 20 (sem tendência definida, mercado em range);
    3) RSI extremo: < 25 (oversold → BUY) ou > 75 (overbought → SELL);
    4) Volume BAIXO (< 0.8× média 20) — sem convicção direcional;
    5) Distância da EMA central curta (≤0.5%) — preço não está fugindo;
    6) Janela 10h-15h30 BRT (depois do leilão, antes da volatilidade EOD);
    7) SL mais apertado (0.9 ATR) — alvos curtos.

  Diferencial vs variantes existentes:
    - RSI_REVERSION (existente): usa RSI isolado, sem exigir contexto de regime;
    - WIN_REVERSION (existente): combinação RSI+BB, sem checar compressão de vol;
    - BOLLINGER (existente): bandas isoladas, sem confirmação de regime;
    - RANGE_TRADING (existente): conceito similar, sem filtros de ATR/volume;
    - Esta: exige CONTEXTO (vol/ADX/volume/EMA) ANTES de confirmar RSI.

  IMPORTANTE: este arquivo NÃO usa trades passados. Validação obrigatória via
  optimization/vt_forward_backtest.py::simulate_forward() sobre barras brutas
  MT5. Não ative em vt_config.json sem walk-forward positivo.

Parâmetros (defaults):
  atr_period=14, atr_avg_period=20, atr_ratio_max=0.9,
  adx_period=14, adx_max=20,
  ema_period=21, max_ema_distance_pct=0.5,
  rsi_period=14, rsi_overbought=75, rsi_oversold=25,
  volume_mult=0.8, volume_avg_period=20,
  hour_start=10, hour_end=15, hour_end_minute=30,
  sl_atr_mult=0.9
"""

from datetime import datetime, timezone, timedelta

STRATEGY_NAME = "VOLATILITY_MEAN_REVERSION"


def _bar_dt_brt(bar_ts):
    try:
        ts = float(bar_ts)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(
        timezone(timedelta(hours=-3))
    )


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Entry em regime ranging com RSI extremo."""
    if not bars or len(bars) < 30 or atr <= 0:
        return None

    dt = _bar_dt_brt(bar_ts)
    if dt is None:
        return None
    minute_of_day = dt.hour * 60 + dt.minute
    if minute_of_day < params.get("hour_start", 10) * 60:
        return None
    if minute_of_day > params.get("hour_end", 15) * 60 + params.get("hour_end_minute", 30):
        return None

    # 1) ATR atual < 0.9 × ATR média (contraindo)
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
    if atr_avg <= 0 or atr / atr_avg > params.get("atr_ratio_max", 0.9):
        return None

    # 2) ADX baixo (sem tendência)
    adx_val, plus_di, minus_di = utils["calculate_adx"](bars, params.get("adx_period", 14))
    if adx_val == 0 or adx_val >= params.get("adx_max", 20):
        return None

    # 3) RSI extremo
    rsi = utils["calculate_rsi"](bars, params.get("rsi_period", 14))
    if rsi == 0:
        return None
    rsi_ob = params.get("rsi_overbought", 75)
    rsi_os = params.get("rsi_oversold", 25)
    direction = None
    if rsi < rsi_os:
        direction = "BUY"
    elif rsi > rsi_ob:
        direction = "SELL"
    if direction is None:
        return None

    # 4) Volume baixo (sem breakout)
    vol_avg_period = params.get("volume_avg_period", 20)
    if len(bars) < vol_avg_period:
        return None
    recent_vol = bars[0].get("tick_volume", 0)
    avg_vol = sum(bars[i].get("tick_volume", 0) for i in range(1, vol_avg_period + 1)) / vol_avg_period
    if avg_vol <= 0 or recent_vol >= avg_vol * params.get("volume_mult", 0.8):
        return None

    # 5) Distância da EMA central curta
    ema_period = params.get("ema_period", 21)
    ema_val = utils["calculate_ema"](bars, ema_period)
    if ema_val and ema_val > 0:
        dist_pct = abs(price - ema_val) / ema_val * 100
        if dist_pct > params.get("max_ema_distance_pct", 0.5):
            return None

    sl_pts = utils["calc_sl"](price, atr, params.get("sl_atr_mult", 0.9), direction)
    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "edge": "vol_mean_reversion",
            "atr_ratio": round(atr / atr_avg, 2),
            "adx": round(adx_val, 1),
            "rsi": round(rsi, 1),
        },
    }
