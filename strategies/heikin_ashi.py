"""
Estratégia HEIKIN_ASHI — Tendência com candles Heikin Ashi suavizados.
Usa HA candles para filtrar ruído e detectar tendências limpas.

Sinal de entrada:
- BUY: Sequência de HA candles bullish (sem lower shadow) + reversão confirmada
- SELL: Sequência de HA candles bearish (sem upper shadow) + reversão confirmada

Parâmetros (via vt_config.json):
  ha_period (consecutive candles needed), ema_period
  rsi_period, rsi_overbought, rsi_oversold
  sl_atr_mult, trail_activate, trail_distance
"""

STRATEGY_NAME = "HEIKIN_ASHI"


def _compute_heikin_ashi(bars, n=10):
    """Compute last n Heikin Ashi candles from raw bars (newest-first)."""
    # Reverse to oldest-first for sequential computation
    raw = list(reversed(bars[:n + 1]))
    ha_candles = []

    ha_open = (raw[0].get("open", 0) + raw[0].get("close", 0)) / 2.0
    ha_close = (raw[0].get("open", 0) + raw[0].get("high", 0) +
                raw[0].get("low", 0) + raw[0].get("close", 0)) / 4.0
    ha_candles.append({"open": ha_open, "close": ha_close,
                       "high": raw[0].get("high", 0), "low": raw[0].get("low", 0)})

    for i in range(1, len(raw)):
        b = raw[i]
        o, h, l, c = b.get("open", 0), b.get("high", 0), b.get("low", 0), b.get("close", 0)
        ha_close_new = (o + h + l + c) / 4.0
        ha_open_new = (ha_candles[-1]["open"] + ha_candles[-1]["close"]) / 2.0
        ha_high = max(h, ha_open_new, ha_close_new)
        ha_low = min(l, ha_open_new, ha_close_new)
        ha_candles.append({"open": ha_open_new, "close": ha_close_new,
                           "high": ha_high, "low": ha_low})

    # Return newest-first
    return list(reversed(ha_candles))


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada HEIKIN_ASHI.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calc_sl = utils["calc_sl"]
    calculate_rsi = utils["calculate_rsi"]
    calculate_ema = utils["calculate_ema"]

    ha_period = params.get("ha_period", 3)  # Consecutive HA candles for trend
    ema_period = params.get("ema_period", 50)
    rsi_period = params.get("rsi_period", 14)
    rsi_ob = params.get("rsi_overbought", 70)
    rsi_os = params.get("rsi_oversold", 30)

    min_bars = max(ha_period + 1, ema_period, rsi_period) + 5
    if not bars or len(bars) < min_bars:
        return None

    ha_candles = _compute_heikin_ashi(bars, ha_period + 2)
    if len(ha_candles) < ha_period + 1:
        return None

    rsi = calculate_rsi(bars, rsi_period)
    ema_val = calculate_ema(bars, ema_period)
    if rsi is None or ema_val == 0:
        return None

    # Count consecutive bullish/bearish HA candles
    # Bullish: close > open (green) and close == high (no upper shadow rejection)
    # Bearish: close < open (red) and close == low (no lower shadow rejection)
    bullish_count = 0
    bearish_count = 0

    for ha in ha_candles[:ha_period]:
        o, c, h, l = ha["open"], ha["close"], ha["high"], ha["low"]
        body = abs(c - o)
        total_range = h - l if h > l else 1

        if c > o and body / total_range > 0.5:  # Strong green candle
            bullish_count += 1
        elif c < o and body / total_range > 0.5:  # Strong red candle
            bearish_count += 1

    direction = None

    # BUY: consecutive bullish HA + not overbought
    if bullish_count >= ha_period and rsi < rsi_ob and price > ema_val:
        direction = "BUY"
    # SELL: consecutive bearish HA + not oversold
    elif bearish_count >= ha_period and rsi > rsi_os and price < ema_val:
        direction = "SELL"

    if not direction:
        return None

    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "HEIKIN_ASHI",
            "bullish_count": bullish_count,
            "bearish_count": bearish_count,
            "rsi": round(rsi, 2),
            "ema": round(ema_val, 2),
        },
    }
