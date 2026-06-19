"""
Estratégia MOMENTUM_BREAKOUT — Breakout com confirmação de momentum (ROC).
Entra quando preço rompe extremo de N barras com momentum positivo/negativo.

Sinal de entrada:
- BUY: preço > highest high de N barras E ROC > 0
- SELL: preço < lowest low de N barras E ROC < 0

Parâmetros (via vt_config.json):
  lookback, roc_period, roc_threshold
  rsi_period, rsi_overbought, rsi_oversold
  sl_atr_mult, trail_activate, trail_distance
"""

STRATEGY_NAME = "MOMENTUM_BREAKOUT"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada MOMENTUM_BREAKOUT.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calc_sl = utils["calc_sl"]
    calculate_rsi = utils["calculate_rsi"]

    lookback = params.get("lookback", 20)
    roc_period = params.get("roc_period", 10)
    roc_threshold = params.get("roc_threshold", 0.001)  # 0.1% min ROC
    rsi_period = params.get("rsi_period", 14)
    rsi_ob = params.get("rsi_overbought", 80)
    rsi_os = params.get("rsi_oversold", 20)

    min_bars = max(lookback, roc_period) + 5
    if not bars or len(bars) < min_bars:
        return None

    # Highest high and lowest low of lookback period (excluding current bar)
    recent = bars[1:lookback + 1]
    if len(recent) < lookback:
        return None

    highest_high = max(b.get("high", 0) for b in recent)
    lowest_low = min(b.get("low", float("inf")) for b in recent)

    if highest_high == 0 or lowest_low == float("inf"):
        return None

    # Rate of Change
    price_n = bars[roc_period].get("close", 0) if roc_period < len(bars) else 0
    if price_n == 0:
        return None

    roc = (price - price_n) / price_n

    rsi = calculate_rsi(bars, rsi_period)
    if rsi is None:
        rsi = 50

    direction = None

    # BUY: breakout above highest high with positive momentum
    if price > highest_high and roc > roc_threshold and rsi < rsi_ob:
        direction = "BUY"
    # SELL: breakout below lowest low with negative momentum
    elif price < lowest_low and roc < -roc_threshold and rsi > rsi_os:
        direction = "SELL"

    if not direction:
        return None

    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "MOMENTUM_BREAKOUT",
            "highest_high": round(highest_high, 2),
            "lowest_low": round(lowest_low, 2),
            "roc": round(roc * 100, 3),
            "rsi": round(rsi, 2),
        },
    }
