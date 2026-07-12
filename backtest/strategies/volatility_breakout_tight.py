"""
Wave 13 (Bruno 2026-07-12) — VOLATILITY_BREAKOUT_TIGHT.

Hipótese de mercado (sem dependência em PnL passado):

  Versão mais rigorosa de breakout direcional com confirmação tripla:
    1) ADX > 22 (tendência presente, não nascendo com força institucional);
    2) Range de N barras rompido PELO FECHAMENTO (não só topos/fundos intra-bar);
    3) Volume climax (>1.8x média 20) — participação real;
    4) RSI em zona direcional (>52.5 para compra, <37.5 para venda) — confirma
       que o rompimento tem corpo, não é só uma sombra;

  Tudo alinhado com breakout institucional de qualidade: rompe range, fecha
  fora, com volume, e com momentum confirmado por RSI.

  Janela 10h-15h30 BRT (depois do leilão matinal).

  Diferencial:
    - VOLATILITY_BREAKOUT (existente): breakout puro sem ADX nem volume;
    - ATR_EXPANSION_BREAKOUT (existente): expansão de ATR mas sem volume climax;
    - DONCHIAN_BREAKOUT (existente): apenas high/low N-bar, sem confirmação;
    - MOMENTUM_BREAKOUT (existente): ADX + breakout, sem volume climax;
    - SQUEEZE_BREAKOUT (existente): exige BB<KC, mais restritivo e complexo;
    - Esta: meio-termo — rigoroso sem ser tão restritivo.

  IMPORTANTE: este arquivo NÃO usa trades passados. Validação obrigatória via
  optimization/vt_forward_backtest.py::simulate_forward() sobre barras brutas
  MT5. Não ative em vt_config.json sem walk-forward positivo.

Parâmetros (defaults):
  breakout_lookback=12, adx_period=14, adx_min=22,
  volume_mult=1.8, volume_avg_period=20,
  rsi_period=14, rsi_overbought=70, rsi_oversold=30,
  hour_start=10, hour_end=15, hour_end_minute=30,
  sl_atr_mult=1.4
"""

from datetime import datetime, timezone, timedelta

STRATEGY_NAME = "VOLATILITY_BREAKOUT_TIGHT"


def _bar_dt_brt(bar_ts):
    try:
        ts = float(bar_ts)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(
        timezone(timedelta(hours=-3))
    )


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Breakout de range com confirmação triple (ADX+volume+RSI)."""
    if not bars or len(bars) < 30 or atr <= 0:
        return None

    # 1) Janela 10:00-15:30
    dt = _bar_dt_brt(bar_ts)
    if dt is None:
        return None
    minute_of_day = dt.hour * 60 + dt.minute
    if minute_of_day < params.get("hour_start", 10) * 60:
        return None
    if minute_of_day > params.get("hour_end", 15) * 60 + params.get("hour_end_minute", 30):
        return None

    # 2) ADX regime
    adx_val, plus_di, minus_di = utils["calculate_adx"](bars, params.get("adx_period", 14))
    if adx_val < params.get("adx_min", 22):
        return None

    # 3) Range das últimas N barras
    lookback = params.get("breakout_lookback", 12)
    if len(bars) < lookback + 1:
        return None

    range_high = max(bars[i].get("high", 0) for i in range(1, lookback + 1))
    range_low = min(bars[i].get("low", 0) for i in range(1, lookback + 1))

    close_now = bars[0].get("close", 0)
    high_now = bars[0].get("high", 0)
    low_now = bars[0].get("low", 0)

    # 4) Volume climax
    vol_avg = params.get("volume_avg_period", 20)
    if len(bars) < vol_avg:
        return None
    recent_vol = bars[0].get("tick_volume", 0)
    avg_vol = sum(b.get("tick_volume", 0) for b in bars[1:vol_avg + 1]) / vol_avg
    if avg_vol <= 0 or recent_vol < avg_vol * params.get("volume_mult", 1.8):
        return None

    # 5) RSI não-neutro (confirma direção)
    rsi = utils["calculate_rsi"](bars, params.get("rsi_period", 14))
    if rsi == 0:
        return None

    # 6) Breakout confirmado
    breakout_up = (close_now > range_high and
                   plus_di > minus_di and
                   rsi > params.get("rsi_overbought", 70) * 0.75)   # > 52.5

    breakout_down = (close_now < range_low and
                     minus_di > plus_di and
                     rsi < params.get("rsi_oversold", 30) * 1.25)   # < 37.5

    if breakout_up:
        sl_pts = utils["calc_sl"](price, atr, params.get("sl_atr_mult", 1.4), "BUY")
        return {"direction": "BUY", "sl_pts": sl_pts,
                "info": {"edge": "vol_breakout_tight", "range_high": round(range_high, 2),
                         "range_low": round(range_low, 2), "adx": round(adx_val, 1),
                         "rsi": round(rsi, 1), "vol_ratio": round(recent_vol / avg_vol, 2)}}

    if breakout_down:
        sl_pts = utils["calc_sl"](price, atr, params.get("sl_atr_mult", 1.4), "SELL")
        return {"direction": "SELL", "sl_pts": sl_pts,
                "info": {"edge": "vol_breakout_tight", "range_high": round(range_high, 2),
                         "range_low": round(range_low, 2), "adx": round(adx_val, 1),
                         "rsi": round(rsi, 1), "vol_ratio": round(recent_vol / avg_vol, 2)}}

    return None
