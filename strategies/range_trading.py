"""
Estratégia RANGE_TRADING — Trading em consolidação (range-bound).
Identifica range e opera bouncing em suporte/resistência.

Sinal de entrada:
- BUY: preço toca fundo do range (suporte) com confirmação
- SELL: preço toca topo do range (resistência) com confirmação

Parâmetros (via vt_config.json):
  lookback, range_atr_pct, touch_pct
  rsi_period, rsi_overbought, rsi_oversold
  sl_atr_mult, trail_activate, trail_distance
"""

STRATEGY_NAME = "RANGE_TRADING"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada RANGE_TRADING.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calc_sl = utils["calc_sl"]
    calculate_rsi = utils["calculate_rsi"]

    lookback = params.get("lookback", 30)
    range_atr_pct = params.get("range_atr_pct", 0.005)  # ATR/price < 0.5% = ranging
    touch_pct = params.get("touch_pct", 0.003)  # 0.3% from level
    rsi_period = params.get("rsi_period", 14)
    rsi_ob = params.get("rsi_overbought", 65)
    rsi_os = params.get("rsi_oversold", 35)

    min_bars = max(lookback, rsi_period) + 5
    if not bars or len(bars) < min_bars:
        return None

    if price == 0 or atr == 0:
        return None

    # Check if market is ranging (ATR relative to price is low)
    atr_pct = atr / price
    if atr_pct > range_atr_pct:
        return None  # Too volatile for range trading

    # Find range boundaries
    recent = bars[:lookback]
    range_high = max(b.get("high", 0) for b in recent)
    range_low = min(b.get("low", float("inf")) for b in recent)

    if range_high == 0 or range_low == float("inf"):
        return None

    range_size = range_high - range_low
    if range_size == 0:
        return None

    # Price position in range (0 = bottom, 1 = top)
    range_pos = (price - range_low) / range_size

    rsi = calculate_rsi(bars, rsi_period)
    if rsi is None:
        rsi = 50

    direction = None

    # BUY: price near range bottom
    if range_pos < touch_pct * 10 and rsi < rsi_os + 5:
        direction = "BUY"
    # SELL: price near range top
    elif range_pos > 1 - touch_pct * 10 and rsi > rsi_ob - 5:
        direction = "SELL"

    if not direction:
        return None

    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "RANGE_TRADING",
            "range_high": round(range_high, 2),
            "range_low": round(range_low, 2),
            "range_pos": round(range_pos, 3),
            "atr_pct": round(atr_pct * 100, 3),
            "rsi": round(rsi, 2),
        },
    }
