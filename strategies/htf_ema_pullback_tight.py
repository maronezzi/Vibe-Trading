"""
Wave 13 (Bruno 2026-07-12) — HTF_EMA_PULLBACK_TIGHT.

Hipótese de mercado (sem dependência em PnL passado):

  Combina dois filtros estruturais:
    1) Tendência de timeframe maior (EMA_fast > EMA_slow + DI direcional);
    2) Pullback intra-TF confirmado: RSI estava abaixo do nível de pullback na
       barra anterior E fechou acima da EMA rápida na barra atual
       (recuperação institucional típica do LEI 1 dos traders locais).

  Filtros de qualidade adicionais:
    - ADX mínimo elevado (24 por padrão) para exigir força de tendência, não chop;
    - Volume climax (>1.2x média 20) para confirmar participação real;
    - Janela 10h-15h30 BRT para evitar o leilão matinal e o fechamento.

  Diferencial vs variantes existentes:
    - HTF_BIAS_LTF_ENTRY (existente): não exige pullback confirmado nem volume climax;
    - EMA_PULLBACK (existente): pullback simples, sem confirmar recuperação;

  IMPORTANTE: este arquivo NÃO usa dados de trades passados para justificar a
  hipótese. Validação DEVE ocorrer via optimization/vt_forward_backtest.py
  ::simulate_forward() sobre barras brutas MT5 (fetch_bars_for_backtest). Não
  ative em vt_config.json sem walk-forward positivo.

Parâmetros (defaults):
  ema_fast=9, ema_slow=21, adx_period=14, adx_min=24,
  rsi_period=14, rsi_pullback_level=42,
  volume_mult=1.2, volume_avg_period=20,
  hour_start_min=600, hour_end_min=930, sl_atr_mult=1.3
"""

from datetime import datetime, timezone, timedelta

STRATEGY_NAME = "HTF_EMA_PULLBACK_TIGHT"


def _bar_dt_brt(bar_ts):
    try:
        ts = float(bar_ts)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(
        timezone(timedelta(hours=-3))
    )


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """HTF bias + LTF pullback tight + volume confirmation."""
    if not bars or len(bars) < 30 or atr <= 0:
        return None

    # 1) Janela: 10:00-15:30 BRT
    dt = _bar_dt_brt(bar_ts)
    if dt is None:
        return None
    minute_of_day = dt.hour * 60 + dt.minute
    if minute_of_day < params.get("hour_start_min", 600):
        return None
    if minute_of_day > params.get("hour_end_min", 930):
        return None

    # 2) EMAs + ADX regime (LTF)
    ema_fast = utils["calculate_ema"](bars, params.get("ema_fast", 9))
    ema_slow = utils["calculate_ema"](bars, params.get("ema_slow", 21))
    if ema_fast == 0 or ema_slow == 0:
        return None

    adx_val, plus_di, minus_di = utils["calculate_adx"](bars, params.get("adx_period", 14))
    if adx_val < params.get("adx_min", 24):
        return None

    # 3) Volume confirm
    vol_avg = params.get("volume_avg_period", 20)
    if len(bars) < vol_avg:
        return None
    recent_vol = bars[0].get("tick_volume", 0)
    avg_vol = sum(b.get("tick_volume", 0) for b in bars[1:vol_avg + 1]) / vol_avg
    if avg_vol <= 0 or recent_vol < avg_vol * params.get("volume_mult", 1.2):
        return None

    # 4) RSI pullback tight
    rsi_pullback = params.get("rsi_pullback_level", 42)
    rsi = utils["calculate_rsi"](bars, params.get("rsi_period", 14))
    if rsi == 0:
        return None

    # 5) Sinal
    # BUY: trend up (ema_fast > ema_slow, +DI > -DI) + RSI estava oversold e subiu
    #      + preço fechou > EMA_fast (confirma pullback acabou)
    trend_bull = ema_fast > ema_slow and plus_di > minus_di
    trend_bear = ema_fast < ema_slow and minus_di > plus_di

    if trend_bull and rsi >= rsi_pullback and rsi < 70:
        # Verifica que houve pullback (RSI estava < rsi_pullback na barra anterior)
        rsi_prev = utils["calculate_rsi"](bars[1:], params.get("rsi_period", 14))
        if rsi_prev == 0 or rsi_prev >= rsi_pullback:
            return None  # não houve pullback real
        # Confirma fechamento
        if bars[0].get("close", 0) <= ema_fast:
            return None
        sl_pts = utils["calc_sl"](price, atr, params.get("sl_atr_mult", 1.3), "BUY")
        return {"direction": "BUY", "sl_pts": sl_pts,
                "info": {"edge": "htf_pullback_tight", "rsi_now": round(rsi, 1),
                         "rsi_prev": round(rsi_prev, 1), "adx": round(adx_val, 1)}}

    if trend_bear and rsi <= (100 - rsi_pullback) and rsi > 30:
        rsi_prev = utils["calculate_rsi"](bars[1:], params.get("rsi_period", 14))
        if rsi_prev == 0 or rsi_prev <= (100 - rsi_pullback):
            return None
        if bars[0].get("close", 0) >= ema_fast:
            return None
        sl_pts = utils["calc_sl"](price, atr, params.get("sl_atr_mult", 1.3), "SELL")
        return {"direction": "SELL", "sl_pts": sl_pts,
                "info": {"edge": "htf_pullback_tight", "rsi_now": round(rsi, 1),
                         "rsi_prev": round(rsi_prev, 1), "adx": round(adx_val, 1)}}

    return None
