"""
Estratégia VOLATILITY_BREAKOUT — Breakout por expansão de volatilidade.
Entra quando ATR se expande significativamente + preço rompe range.

Sinal de entrada:
- BUY: ATR expandiu (current > avg * mult) E preço rompeu high recente
- SELL: ATR expandiu E preço rompeu low recente

Parâmetros (via vt_config.json):
  atr_period, atr_mult, lookback
  rsi_period, rsi_overbought, rsi_oversold
  sl_atr_mult, trail_activate, trail_distance
"""

STRATEGY_NAME = "VOLATILITY_BREAKOUT"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada VOLATILITY_BREAKOUT.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calc_sl = utils["calc_sl"]
    calculate_rsi = utils["calculate_rsi"]

    atr_period = params.get("atr_period", 14)
    atr_mult = params.get("atr_mult", 1.5)  # ATR must be 1.5x average
    lookback = params.get("lookback", 20)
    rsi_period = params.get("rsi_period", 14)
    rsi_ob = params.get("rsi_overbought", 80)
    rsi_os = params.get("rsi_oversold", 20)

    min_bars = max(lookback, atr_period * 2) + 5
    if not bars or len(bars) < min_bars:
        return None

    if atr == 0:
        return None

    # Calculate historical ATR average
    atr_values = []
    for i in range(min(atr_period * 2, len(bars) - 1)):
        b = bars[i]
        h = b.get("high", 0)
        l = b.get("low", 0)
        prev_c = bars[i + 1].get("close", 0) if i + 1 < len(bars) else h
        if h > 0 and l > 0 and prev_c > 0:
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            atr_values.append(tr)

    if len(atr_values) < atr_period:
        return None

    avg_atr = sum(atr_values[:atr_period]) / atr_period
    if avg_atr == 0:
        return None

    # Check ATR expansion
    if atr < avg_atr * atr_mult:
        return None  # Not enough volatility expansion

    # Recent high/low
    recent = bars[1:lookback + 1]  # Exclude current bar
    recent_high = max(b.get("high", 0) for b in recent)
    recent_low = min(b.get("low", float("inf")) for b in recent)

    if recent_high == 0 or recent_low == float("inf"):
        return None

    rsi = calculate_rsi(bars, rsi_period)
    if rsi is None:
        rsi = 50

    direction = None

    # BUY: price broke above recent high with ATR expansion
    if price > recent_high and rsi < rsi_ob:
        direction = "BUY"
    # SELL: price broke below recent low with ATR expansion
    elif price < recent_low and rsi > rsi_os:
        direction = "SELL"

    if not direction:
        return None

    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "VOLATILITY_BREAKOUT",
            "atr": round(atr, 2),
            "avg_atr": round(avg_atr, 2),
            "atr_ratio": round(atr / avg_atr, 2),
            "recent_high": round(recent_high, 2),
            "recent_low": round(recent_low, 2),
            "rsi": round(rsi, 2),
        },
    }
