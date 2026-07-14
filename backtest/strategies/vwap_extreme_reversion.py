"""
Wave W874 (2026-07-08) — Estratégia VWAP_EXTREME_REVERSION.

Edge: mean-reversion quando preço desvia demais da VWAP.
Em WIN/WDO, desvio > 2.5× ATR da VWAP tende a reverter dentro
de 5-10 barras (literatura de auction theory + empiria B3).

Sinal:
  - SELL: preço > VWAP + 2.5× ATR e RSI > overbought (exaustão de alta)
  - BUY:  preço < VWAP - 2.5× ATR e RSI < oversold (exaustão de baixa)

Defensivo:
  - sl_atr_mult padrão 1.5 (Lei 5: conservador)
  - Requer volume climax (volume > 1.5× média das últimas 20 barras)
  - Requer ADX < 25 (evitar contra-tendência em trend forte)
  - Filtro de sessão: 9:30-16:00 BRT (evitar abertura/fechamento extremos)

Parâmetros:
  vwap_period=20, deviation_atr_mult=2.5, rsi_overbought=75, rsi_oversold=25,
  volume_mult=1.5, adx_max=25, sl_atr_mult=1.5, adx_period=14

Diferencial vs pool atual:
  - vwap.py: mostra VWAP, mas não gera entry por desvio.
  - rsi_reversion: usa RSI isolado, sem filtro de VWAP/vol/ADX.
  - Esta estratégia combina os 3 filtros para reduzir falsos positivos.
"""
from datetime import datetime, timezone, timedelta

STRATEGY_NAME = "VWAP_EXTREME_REVERSION"

DEFAULT_PARAMS = {
    "vwap_period": 20,
    "deviation_atr_mult": 2.5,
    "rsi_period": 14,
    "rsi_overbought": 75,
    "rsi_oversold": 25,
    "volume_mult": 1.5,
    "volume_avg_period": 20,
    "adx_period": 14,
    "adx_max": 25,
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


def _avg_volume(bars, n=20):
    """Média de volume das últimas N barras."""
    if not bars:
        return 0
    sample = bars[:n]
    vols = [b.get("tick_volume", 0) or b.get("volume", 0) for b in sample]
    return sum(vols) / len(vols) if vols else 0


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Verifica sinal de entrada VWAP_EXTREME_REVERSION.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    p = {**DEFAULT_PARAMS, **(params or {})}

    calculate_vwap = utils["calculate_vwap"]
    calculate_rsi = utils["calculate_rsi"]
    calculate_adx = utils["calculate_adx"]
    calc_sl = utils["calc_sl"]

    if not bars or len(bars) < max(p["vwap_period"], p["adx_period"] * 2) + 5:
        return None
    if atr is None or atr <= 0:
        return None

    # Filtro de sessão: 9:30-16:00 BRT (evitar extremos de abertura/fechamento)
    brt_now = _bar_hour_utc_minus_3(bar_ts)
    if brt_now is None:
        return None
    minutes_since_open = (brt_now.hour - 9) * 60 + brt_now.minute
    if minutes_since_open < 30 or minutes_since_open > 16 * 60:
        return None

    # VWAP e RSI
    vwap_val = calculate_vwap(bars, p["vwap_period"])
    rsi = calculate_rsi(bars, p["rsi_period"])
    if vwap_val == 0 or rsi == 50:
        return None

    # Desvio em unidades de ATR
    deviation = price - vwap_val
    deviation_atrs = deviation / atr if atr > 0 else 0

    # Volume climax
    avg_vol = _avg_volume(bars, n=p["volume_avg_period"])
    last_bar = bars[0] if bars else {}
    last_vol = last_bar.get("tick_volume", 0) or last_bar.get("volume", 0) or 0
    vol_ratio = (last_vol / avg_vol) if avg_vol > 0 else 0

    # ADX: rejeita se trend muito forte (Lei 5: não contra-tendência agressiva)
    adx_tuple = calculate_adx(bars, p["adx_period"])
    if isinstance(adx_tuple, tuple):
        adx_val = adx_tuple[0]
    else:
        adx_val = adx_tuple
    if adx_val > p["adx_max"]:
        return None

    direction = None

    # SELL: preço bem acima da VWAP + RSI exausto
    if (
        deviation_atrs > p["deviation_atr_mult"]
        and rsi > p["rsi_overbought"]
        and vol_ratio >= p["volume_mult"]
    ):
        direction = "SELL"

    # BUY: preço bem abaixo da VWAP + RSI exausto
    elif (
        deviation_atrs < -p["deviation_atr_mult"]
        and rsi < p["rsi_oversold"]
        and vol_ratio >= p["volume_mult"]
    ):
        direction = "BUY"

    if not direction:
        return None

    sl_pts = calc_sl(symbol, atr, p)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "VWAP_EXTREME_REVERSION",
            "vwap": round(vwap_val, 4),
            "deviation_atrs": round(deviation_atrs, 2),
            "rsi": round(rsi, 1),
            "adx": round(adx_val, 1),
            "vol_ratio": round(vol_ratio, 2),
            "atr": atr,
        },
    }
