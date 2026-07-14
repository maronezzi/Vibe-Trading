"""
Wave W874 (2026-07-08) — Estratégia SESSION_MOMENTUM_CLOSE.

Edge: explorar viés de continuação no fechamento do pregão B3.
Em WIN/WDO, os últimos 60 minutos (16:00-17:00 BRT) têm participação
institucional pesada (hedge funds ajustando posições, futuros globais
fechando). Momentum iniciado nesse intervalo tende a continuar no dia
seguinte (gap) e dentro da janela final.

Sinal:
  - Janela: 16:00-16:55 BRT (depois disso, EOD fecha)
  - BUY: EMA fast > EMA slow + ADX > 20 + plus_di > minus_di
        + volume > 1.3× média + close > open (barra bullish)
  - SELL: mirror

Defensivo:
  - sl_atr_mult padrão 1.5 (Lei 5: conservador)
  - Janela restrita: apenas últimos 60 min (evitar IL matinal)
  - Requer ADX mínimo (regime direcional)
  - Volume spike (participação real, não ruído)

Parâmetros:
  window_start_hour=16, window_start_minute=0,
  window_end_hour=16, window_end_minute=55,
  ema_fast=8, ema_slow=21, adx_period=14, adx_min=20,
  volume_mult=1.3, volume_avg_period=20, sl_atr_mult=1.5

Diferencial vs pool atual:
  - opening_range_breakout: opera só nos primeiros 30 min.
  - Nenhuma outra estratégia tem viés de tempo de fechamento.
  - Esta captura o institucional close window (literatura Harris 1986,
    "S&P 500 Daytrade": MOC orders criam momentum direcional).
"""
from datetime import datetime, timezone, timedelta

STRATEGY_NAME = "SESSION_MOMENTUM_CLOSE"

DEFAULT_PARAMS = {
    "window_start_hour": 16,
    "window_start_minute": 0,
    "window_end_hour": 16,
    "window_end_minute": 55,
    "ema_fast": 8,
    "ema_slow": 21,
    "adx_period": 14,
    "adx_min": 20,
    "volume_mult": 1.3,
    "volume_avg_period": 20,
    "sl_atr_mult": 1.5,
}


def _bar_hour_utc_minus_3(bar_ts):
    """Converte bar_ts (unix) para hora BRT (UTC-3)."""
    try:
        ts = float(bar_ts)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(
        timezone(timedelta(hours=-3))
    )


def _in_close_window(brt_now, p):
    """True se brt_now está dentro da janela de fechamento."""
    start_min = p["window_start_hour"] * 60 + p["window_start_minute"]
    end_min = p["window_end_hour"] * 60 + p["window_end_minute"]
    now_min = brt_now.hour * 60 + brt_now.minute
    return start_min <= now_min <= end_min


def _avg_volume(bars, n=20):
    if not bars:
        return 0
    sample = bars[:n]
    vols = [b.get("tick_volume", 0) or b.get("volume", 0) for b in sample]
    return sum(vols) / len(vols) if vols else 0


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Verifica sinal de entrada SESSION_MOMENTUM_CLOSE.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    p = {**DEFAULT_PARAMS, **(params or {})}

    calculate_ema = utils["calculate_ema"]
    calculate_adx = utils["calculate_adx"]
    calc_sl = utils["calc_sl"]

    if not bars or len(bars) < p["ema_slow"] + 5:
        return None
    if atr is None or atr <= 0:
        return None

    # Filtro de janela: últimos 60 min do pregão
    brt_now = _bar_hour_utc_minus_3(bar_ts)
    if brt_now is None:
        return None
    if not _in_close_window(brt_now, p):
        return None

    # EMA alignment
    ema_fast = calculate_ema(bars, p["ema_fast"])
    ema_slow = calculate_ema(bars, p["ema_slow"])
    if ema_fast == 0 or ema_slow == 0:
        return None

    # ADX: precisa de direção
    adx_tuple = calculate_adx(bars, p["adx_period"])
    if isinstance(adx_tuple, tuple):
        adx_val, plus_di, minus_di = adx_tuple
    else:
        return None
    if adx_val < p["adx_min"]:
        return None

    # Volume spike
    avg_vol = _avg_volume(bars, n=p["volume_avg_period"])
    last_bar = bars[0] if bars else {}
    last_vol = last_bar.get("tick_volume", 0) or last_bar.get("volume", 0) or 0
    vol_ratio = (last_vol / avg_vol) if avg_vol > 0 else 0
    if vol_ratio < p["volume_mult"]:
        return None

    # Candle direction
    candle_open = last_bar.get("open", price)
    candle_close = last_bar.get("close", price)
    is_bullish = candle_close > candle_open
    is_bearish = candle_close < candle_open

    direction = None

    # BUY: EMA alignment + DI + candle bullish + vol
    if (
        ema_fast > ema_slow
        and plus_di > minus_di
        and is_bullish
    ):
        direction = "BUY"

    # SELL: mirror
    elif (
        ema_fast < ema_slow
        and minus_di > plus_di
        and is_bearish
    ):
        direction = "SELL"

    if not direction:
        return None

    sl_pts = calc_sl(symbol, atr, p)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "SESSION_MOMENTUM_CLOSE",
            "ema_fast": round(ema_fast, 4),
            "ema_slow": round(ema_slow, 4),
            "adx": round(adx_val, 1),
            "plus_di": round(plus_di, 1),
            "minus_di": round(minus_di, 1),
            "vol_ratio": round(vol_ratio, 2),
            "candle_open": candle_open,
            "candle_close": candle_close,
            "brt_time": brt_now.strftime("%H:%M"),
            "atr": atr,
        },
    }
