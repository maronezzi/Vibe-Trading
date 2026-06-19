"""
Estratégia CANDLE_PATTERNS — Padrões de candles (Pin Bar, Hammer, Engulfing).
Detecta padrões de reversão com filtro de tendência.

Sinal de entrada:
- BUY: Hammer/Pin Bar bullish ou Engulfing bullish em uptrend
- SELL: Pin Bar bearish ou Engulfing bearish em downtrend

Parâmetros (via vt_config.json):
  ema_period, body_ratio, wick_ratio
  rsi_period, rsi_overbought, rsi_oversold
  sl_atr_mult, trail_activate, trail_distance
"""

STRATEGY_NAME = "CANDLE_PATTERNS"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada CANDLE_PATTERNS.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calc_sl = utils["calc_sl"]
    calculate_ema = utils["calculate_ema"]
    calculate_rsi = utils["calculate_rsi"]

    ema_period = params.get("ema_period", 50)
    body_ratio = params.get("body_ratio", 0.3)  # Body < 30% of total range
    wick_ratio = params.get("wick_ratio", 2.0)  # Wick > 2x body
    rsi_period = params.get("rsi_period", 14)
    rsi_ob = params.get("rsi_overbought", 70)
    rsi_os = params.get("rsi_oversold", 30)

    min_bars = max(ema_period, rsi_period) + 5
    if not bars or len(bars) < min_bars:
        return None

    ema_val = calculate_ema(bars, ema_period)
    if ema_val == 0:
        return None

    rsi = calculate_rsi(bars, rsi_period)
    if rsi is None:
        rsi = 50

    # Current and previous bar
    curr = bars[0]
    prev = bars[1]

    c_open = curr.get("open", 0)
    c_high = curr.get("high", 0)
    c_low = curr.get("low", 0)
    c_close = curr.get("close", 0)

    p_open = prev.get("open", 0)
    p_close = prev.get("close", 0)

    if c_high <= c_low or c_open == 0 or c_close == 0:
        return None
    if p_open == 0 or p_close == 0:
        return None

    c_range = c_high - c_low
    c_body = abs(c_close - c_open)
    c_upper_wick = c_high - max(c_open, c_close)
    c_lower_wick = min(c_open, c_close) - c_low

    p_body = abs(p_close - p_open)

    direction = None
    pattern = None

    # Hammer / Pin Bar bullish: long lower wick, small body at top
    if (c_body / c_range < body_ratio and
            c_lower_wick > c_body * wick_ratio and
            c_close > c_open):  # Green candle
        if price > ema_val and rsi < rsi_ob:
            direction = "BUY"
            pattern = "HAMMER"

    # Pin Bar bearish: long upper wick, small body at bottom
    if not direction and (c_body / c_range < body_ratio and
                          c_upper_wick > c_body * wick_ratio and
                          c_close < c_open):  # Red candle
        if price < ema_val and rsi > rsi_os:
            direction = "SELL"
            pattern = "PIN_BAR_BEARISH"

    # Bullish Engulfing: current green body engulfs previous red body
    if not direction and c_close > c_open and p_close < p_open:
        if c_body > p_body and c_close > p_open and c_open < p_close:
            if price > ema_val * 0.998 and rsi < rsi_ob:
                direction = "BUY"
                pattern = "ENGULFING_BULL"

    # Bearish Engulfing: current red body engulfs previous green body
    if not direction and c_close < c_open and p_close > p_open:
        if c_body > p_body and c_close < p_open and c_open > p_close:
            if price < ema_val * 1.002 and rsi > rsi_os:
                direction = "SELL"
                pattern = "ENGULFING_BEAR"

    if not direction:
        return None

    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "CANDLE_PATTERNS",
            "pattern": pattern,
            "rsi": round(rsi, 2),
            "ema": round(ema_val, 2),
        },
    }
