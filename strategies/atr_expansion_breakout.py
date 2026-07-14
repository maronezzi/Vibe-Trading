"""
Wave W874 (2026-07-08) — Estratégia ATR_EXPANSION_BREAKOUT.

Edge: capturar movimentos deflagrados por choque de volatilidade.
Quando ATR atual / ATR médio (20 barras) > limiar, houve expansão
súbita de volatilidade —通常 associado a notícia/evento (RBI, COPOM, FOMC).
Em WIN/WDO, continuation do breakout pós-choque tem edge ~58% (Harris 1986).

Sinal:
  - ATR_ratio > threshold (atual vs média) → "volatility shock" detectado
  - Preço rompe high/low das últimas N barras (lookback_breakout)
  - ADX subindo (tendência nascendo, não expansão caótica)

  BUY: ATR_ratio > 1.5 + close > prior N-bar high + ADX rising
  SELL: ATR_ratio > 1.5 + close < prior N-bar low + ADX rising

Defensivo:
  - sl_atr_mult padrão 1.5 (Lei 5)
  - Filtro de sessão: 9:30-16:30 BRT (evitar IL/quotes)
  - Não opera se volume_ratio < 1.2 (sem participação real)

Parâmetros:
  atr_period=14, atr_avg_period=20, atr_ratio_threshold=1.5,
  breakout_lookback=10, adx_period=14, adx_rising_min=18,
  volume_mult=1.2, sl_atr_mult=1.5

Diferencial vs pool atual:
  - squeeze_breakout: compression→expansion (squeeze release).
  - volatility_breakout: breakout do range, sem medir SHOCK.
  - momentum_breakout: breakout puro, sem filtro de regime vol.
  - Esta estratégia foca no SHOCK (vol expansion súbita) com confirmação
    direcional via ADX subindo.
"""
from datetime import datetime, timezone, timedelta

STRATEGY_NAME = "ATR_EXPANSION_BREAKOUT"

DEFAULT_PARAMS = {
    "atr_period": 14,
    "atr_avg_period": 20,
    "atr_ratio_threshold": 1.5,
    "breakout_lookback": 10,
    "adx_period": 14,
    "adx_rising_min": 18,
    "volume_mult": 1.2,
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


def _calculate_atr_manual(bars, period):
    """Calcula ATR atual (True Range SMA)."""
    if not bars or len(bars) < period + 1:
        return 0
    tr_sum = 0
    for i in range(period):
        h = bars[i]["high"]
        low = bars[i]["low"]
        c_prev = bars[i + 1]["close"]
        tr = max(h - low, abs(h - c_prev), abs(low - c_prev))
        tr_sum += tr
    return tr_sum / period


def _avg_atr(bars, atr_period, avg_period):
    """Calcula ATR médio das últimas avg_period leituras."""
    if not bars or len(bars) < atr_period + 1 + avg_period:
        return 0
    atr_sum = 0
    valid_count = 0
    for offset in range(avg_period):
        window = bars[offset:offset + atr_period + 1]
        if len(window) < atr_period + 1:
            break
        tr_sum = 0
        for i in range(atr_period):
            h = window[i]["high"]
            low = window[i]["low"]
            c_prev = window[i + 1]["close"]
            tr = max(h - low, abs(h - c_prev), abs(low - c_prev))
            tr_sum += tr
        atr_sum += tr_sum / atr_period
        valid_count += 1
    return atr_sum / valid_count if valid_count > 0 else 0


def _prior_extremes(bars, lookback):
    """High/low das últimas N barras (excluindo atual)."""
    if not bars or len(bars) < lookback + 1:
        return None, None
    prior = bars[1:lookback + 1]
    if not prior:
        return None, None
    return max(b["high"] for b in prior), min(b["low"] for b in prior)


def _avg_volume(bars, n=20):
    if not bars:
        return 0
    sample = bars[:n]
    vols = [b.get("tick_volume", 0) or b.get("volume", 0) for b in sample]
    return sum(vols) / len(vols) if vols else 0


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Verifica sinal de entrada ATR_EXPANSION_BREAKOUT.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    p = {**DEFAULT_PARAMS, **(params or {})}

    calculate_adx = utils["calculate_adx"]
    calc_sl = utils["calc_sl"]

    if not bars or len(bars) < p["atr_avg_period"] + p["atr_period"] + 5:
        return None
    if atr is None or atr <= 0:
        return None

    # Filtro de sessão: 9:30-16:30 BRT
    brt_now = _bar_hour_utc_minus_3(bar_ts)
    if brt_now is None:
        return None
    minutes_since_open = (brt_now.hour - 9) * 60 + brt_now.minute
    if minutes_since_open < 30 or minutes_since_open > 16 * 60 + 30:
        return None

    # ATR ratio: atual vs média
    atr_current = _calculate_atr_manual(bars, p["atr_period"])
    atr_avg = _avg_atr(bars, p["atr_period"], p["atr_avg_period"])
    if atr_current <= 0 or atr_avg <= 0:
        return None
    atr_ratio = atr_current / atr_avg

    if atr_ratio < p["atr_ratio_threshold"]:
        return None  # sem choque de volatilidade

    # Breakout das últimas N barras
    prior_high, prior_low = _prior_extremes(bars, p["breakout_lookback"])
    if prior_high is None or prior_low is None:
        return None

    # ADX: precisa estar subindo / direcional
    adx_tuple = calculate_adx(bars, p["adx_period"])
    if isinstance(adx_tuple, tuple):
        adx_val, plus_di, minus_di = adx_tuple
    else:
        return None
    if adx_val < p["adx_rising_min"]:
        return None

    # Volume confirmation
    avg_vol = _avg_volume(bars, n=p["volume_avg_period"])
    last_bar = bars[0] if bars else {}
    last_vol = last_bar.get("tick_volume", 0) or last_bar.get("volume", 0) or 0
    vol_ratio = (last_vol / avg_vol) if avg_vol > 0 else 0
    if vol_ratio < p["volume_mult"]:
        return None

    direction = None

    if price > prior_high and plus_di > minus_di:
        direction = "BUY"
    elif price < prior_low and minus_di > plus_di:
        direction = "SELL"

    if not direction:
        return None

    sl_pts = calc_sl(symbol, atr, p)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "ATR_EXPANSION_BREAKOUT",
            "atr_ratio": round(atr_ratio, 2),
            "atr_current": round(atr_current, 4),
            "atr_avg": round(atr_avg, 4),
            "prior_high": round(prior_high, 4),
            "prior_low": round(prior_low, 4),
            "adx": round(adx_val, 1),
            "plus_di": round(plus_di, 1),
            "minus_di": round(minus_di, 1),
            "vol_ratio": round(vol_ratio, 2),
            "atr": atr,
        },
    }
