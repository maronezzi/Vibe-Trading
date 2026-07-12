"""
Wave 13 (Bruno 2026-07-12) — TRAIL_HOLDERS_TREND.

Hipótese de mercado (sem dependência em PnL passado):

  Estratégia desenhada para SINALIZAR ENTRADAS com perfil de continuação
  prolongada — i.e., trades com maior probabilidade de sair via trailing stop
  (captura de momentum) em vez de batida no stop curto.

  Filtros estruturais:
    1) ADX mínimo elevado (28) — exige tendência FORTE estabelecida (não nascendo);
    2) Spread entre +DI e -DI > 15 — direção direcional clara, sem indecisão;
    3) Pullback à EMA_fast + recross confirmado (entrada a favor da tendência);
    4) Volume spike > 1.5x média 20 — interesse institucional;
    5) Janela 10h30-15h30 — depois do leilão caótico, antes do EOD.

  Perfil esperado: SL curto (1.2 ATR) e trailing fazendo o resto do trabalho.
  Justificativa teórica: continuação de tendência em pares institucionais com
  participação real tende a absorver drawdowns curtos e seguir — hipótese
  clássica de trend-following, mas com filtros suficientes para reduzir
  exposição em chop.

  Diferencial:
    - ADX_TREND (existente): mesma base ADX, sem filtros extras;
    - STRONG_TREND (existente): ADX alto + DI, mas sem confirmação de pullback;
    - MOMENTUM_BREAKOUT (existente): breakout puro, sem pullback institucional;

  IMPORTANTE: este arquivo NÃO usa trades passados. Validação obrigatória via
  optimization/vt_forward_backtest.py::simulate_forward() sobre barras brutas
  MT5. Não ative em vt_config.json sem walk-forward positivo.

Parâmetros (defaults):
  ema_fast=9, ema_slow=21, adx_period=14, adx_min=28, di_spread_min=15,
  volume_mult=1.5, volume_avg_period=20,
  hour_start=10, hour_start_minute=30, hour_end=15, hour_end_minute=30,
  sl_atr_mult=1.2
"""

from datetime import datetime, timezone, timedelta

STRATEGY_NAME = "TRAIL_HOLDERS_TREND"


def _bar_dt_brt(bar_ts):
    try:
        ts = float(bar_ts)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(
        timezone(timedelta(hours=-3))
    )


def _atr_ratio(bars, atr_period, atr_avg_period):
    """ATR atual / ATR médio = medida de expansão de volatilidade."""
    atr_current = bars[0].get("atr", 0) if isinstance(bars[0], dict) else 0
    # Fallback: calcular via calculate_atr
    return None


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Pullback institucional em trend forte — saída por trailing stop."""
    if not bars or len(bars) < 30 or atr <= 0:
        return None

    # 1) Janela: 10:30-15:30 BRT (depois do caos matinal, antes do EOD)
    dt = _bar_dt_brt(bar_ts)
    if dt is None:
        return None
    minute_of_day = dt.hour * 60 + dt.minute
    start_min = params.get("hour_start", 10) * 60 + params.get("hour_start_minute", 30)
    end_min = params.get("hour_end", 15) * 60 + params.get("hour_end_minute", 30)
    if not (start_min <= minute_of_day <= end_min):
        return None

    adx_period = params.get("adx_period", 14)
    adx_val, plus_di, minus_di = utils["calculate_adx"](bars, adx_period)
    if adx_val < params.get("adx_min", 28):
        return None

    # 2) DI spread (direção clara)
    di_spread = abs(plus_di - minus_di)
    if di_spread < params.get("di_spread_min", 15):
        return None

    # 3) EMA + pullback confirmado
    ema_fast = utils["calculate_ema"](bars, params.get("ema_fast", 9))
    ema_slow = utils["calculate_ema"](bars, params.get("ema_slow", 21))
    if ema_fast == 0 or ema_slow == 0:
        return None

    trend_bull = ema_fast > ema_slow and plus_di > minus_di
    trend_bear = ema_fast < ema_slow and minus_di > plus_di

    if not (trend_bull or trend_bear):
        return None

    # 4) Volume spike (entrada institucional)
    vol_avg_period = params.get("volume_avg_period", 20)
    if len(bars) < vol_avg_period:
        return None
    recent_vol = bars[0].get("tick_volume", 0)
    avg_vol = sum(b.get("tick_volume", 0) for b in bars[1:vol_avg_period + 1]) / vol_avg_period
    if avg_vol <= 0 or recent_vol < avg_vol * params.get("volume_mult", 1.5):
        return None

    # 5) Pullback confirmado (preço RECUPEROU a EMA, não está longe dela)
    if trend_bull:
        # BUY: preço voltou acima da EMA_fast após pullback
        # Confirmação: barra atual fechou > EMA_fast, barra anterior chegou a tocar
        close_now = bars[0].get("close", 0)
        prev_low = bars[1].get("low", 0)
        if not (close_now > ema_fast and prev_low <= ema_fast * 1.0005):
            return None
        sl_pts = utils["calc_sl"](price, atr, params.get("sl_atr_mult", 1.2), "BUY")
        return {"direction": "BUY", "sl_pts": sl_pts,
                "info": {"edge": "trail_holders", "adx": round(adx_val, 1),
                         "di_spread": round(di_spread, 1),
                         "ema_fast": round(ema_fast, 2), "ema_slow": round(ema_slow, 2)}}

    if trend_bear:
        close_now = bars[0].get("close", 0)
        prev_high = bars[1].get("high", 0)
        if not (close_now < ema_fast and prev_high >= ema_fast * 0.9995):
            return None
        sl_pts = utils["calc_sl"](price, atr, params.get("sl_atr_mult", 1.2), "SELL")
        return {"direction": "SELL", "sl_pts": sl_pts,
                "info": {"edge": "trail_holders", "adx": round(adx_val, 1),
                         "di_spread": round(di_spread, 1),
                         "ema_fast": round(ema_fast, 2), "ema_slow": round(ema_slow, 2)}}

    return None
