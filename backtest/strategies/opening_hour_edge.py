"""
Wave 13 (Bruno 2026-07-12) — OPENING_HOUR_EDGE.

Hipótese de mercado (sem dependência em PnL passado):

  Restringe sinais à primeira hora de pregão (9h-10h BRT), onde os gaps
  overnight normalmente se consolidam em tendências iniciais antes do ruído
  institucional do meio-dia.

  Setup:
    1) Janela restrita: 9h00 - 10h00 BRT;
    2) Tendência de baixa/alta já estabelecida no início do dia
       (EMA_fast vs EMA_slow + DI direcional + ADX > 20);
    3) Pullback à EMA_fast confirmado (preço tocou a média na barra anterior
       e fechou do lado correto na barra atual);
    4) Volume > 1.0x média 20 — participação real;
    5) RSI 40-70 — evita extremos de overbought/oversold típicos do leilão;
    6) SL apertado (1.2 ATR), esperando o trailing absorver.

  Diferencial:
    - OPENING_RANGE_BREAKOUT (existente): restringe-se a breakouts high/low;
    - ATR_EXPANSION_BREAKOUT (existente): detecta choque mas opera o dia todo;
    - Esta: pullback intra-faixa na primeira hora, captura continuation.

  IMPORTANTE: este arquivo NÃO usa trades passados. Validação obrigatória via
  optimization/vt_forward_backtest.py::simulate_forward() sobre barras brutas
  MT5. Não ative em vt_config.json sem walk-forward positivo.

Parâmetros (defaults):
  window_start_hour=9, window_start_minute=0,
  window_end_hour=10, window_end_minute=0,
  ema_fast=9, ema_slow=21, adx_period=14, adx_min=20,
  rsi_period=14, rsi_low=40, rsi_high=70,
  volume_mult=1.0, volume_avg_period=20,
  sl_atr_mult=1.2
"""

from datetime import datetime, timezone, timedelta

STRATEGY_NAME = "OPENING_HOUR_EDGE"


def _bar_dt_brt(bar_ts):
    """Converte bar_ts unix para datetime BRT (UTC-3)."""
    try:
        ts = float(bar_ts)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(
        timezone(timedelta(hours=-3))
    )


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Abertura restrita à primeira hora com filtro ADX+pullback+volume."""
    if not bars or len(bars) < 30 or atr <= 0:
        return None

    # 1) Janela: 9:00-10:00 BRT
    dt = _bar_dt_brt(bar_ts)
    if dt is None:
        return None
    h, m = dt.hour, dt.minute
    minute_of_day = h * 60 + m
    start_min = params.get("window_start_hour", 9) * 60 + params.get("window_start_minute", 0)
    end_min = params.get("window_end_hour", 10) * 60 + params.get("window_end_minute", 0)
    if not (start_min <= minute_of_day <= end_min):
        return None

    # 2) ADX regime check
    adx_val, plus_di, minus_di = utils["calculate_adx"](bars, params.get("adx_period", 14))
    if adx_val < params.get("adx_min", 20):
        return None

    # 3) EMAs + preço
    ema_fast = utils["calculate_ema"](bars, params.get("ema_fast", 9))
    ema_slow = utils["calculate_ema"](bars, params.get("ema_slow", 21))
    if ema_fast == 0 or ema_slow == 0:
        return None

    # 4) Volume confirm
    if len(bars) < params.get("volume_avg_period", 20):
        return None
    recent_vol = bars[0].get("tick_volume", 0)
    avg_vol = sum(b.get("tick_volume", 0) for b in bars[1:params.get("volume_avg_period", 20) + 1]) / params.get("volume_avg_period", 20)
    if avg_vol <= 0 or recent_vol < avg_vol * params.get("volume_mult", 1.0):
        return None

    # 5) RSI 40-70 (não extremo — primeira hora não dá extremos significativos)
    rsi = utils["calculate_rsi"](bars, params.get("rsi_period", 14))
    if rsi < params.get("rsi_low", 40) or rsi > params.get("rsi_high", 70):
        return None

    # 6) Sinal
    # BUY: pullback à EMA_fast (preço toca de cima) + tendência de alta
    buy_pullback = (bars[1].get("close", 0) > ema_fast and
                    bars[0].get("close", 0) > ema_fast and
                    ema_fast > ema_slow and plus_di > minus_di and
                    bars[1].get("low", 0) <= ema_fast * 1.001)   # tocou
    sell_pullback = (bars[1].get("close", 0) < ema_fast and
                     bars[0].get("close", 0) < ema_fast and
                     ema_fast < ema_slow and minus_di > plus_di and
                     bars[1].get("high", 0) >= ema_fast * 0.999)  # tocou

    if buy_pullback:
        sl_pts = utils["calc_sl"](price, atr, params.get("sl_atr_mult", 1.2), "BUY")
        return {"direction": "BUY", "sl_pts": sl_pts,
                "info": {"edge": "opening_hour", "adx": round(adx_val, 1), "rsi": round(rsi, 1),
                         "ema_fast": round(ema_fast, 2), "ema_slow": round(ema_slow, 2)}}

    if sell_pullback:
        sl_pts = utils["calc_sl"](price, atr, params.get("sl_atr_mult", 1.2), "SELL")
        return {"direction": "SELL", "sl_pts": sl_pts,
                "info": {"edge": "opening_hour", "adx": round(adx_val, 1), "rsi": round(rsi, 1),
                         "ema_fast": round(ema_fast, 2), "ema_slow": round(ema_slow, 2)}}

    return None
