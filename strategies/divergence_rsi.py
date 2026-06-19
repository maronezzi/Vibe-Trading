"""
Estratégia DIVERGENCE_RSI — Divergência RSI para reversão.
Detecta quando preço faz novo extremo mas RSI não confirma.

Sinal de entrada:
- BUY: Preço faz low mais baixo, RSI faz low mais alto (divergência bullish)
- SELL: Preço faz high mais alto, RSI faz high mais baixo (divergência bearish)

Parâmetros (via vt_config.json):
  lookback, rsi_period, min_divergence
  sl_atr_mult, trail_activate, trail_distance
"""

STRATEGY_NAME = "DIVERGENCE_RSI"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada DIVERGENCE_RSI.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calc_sl = utils["calc_sl"]
    calculate_rsi = utils["calculate_rsi"]
    calculate_ema = utils["calculate_ema"]

    lookback = params.get("lookback", 20)
    rsi_period = params.get("rsi_period", 14)
    ema_period = params.get("ema_period", 50)
    min_div_pct = params.get("min_divergence", 0.005)  # 0.5% min price move

    min_bars = max(lookback, rsi_period, ema_period) + 5
    if not bars or len(bars) < min_bars:
        return None

    rsi = calculate_rsi(bars, rsi_period)
    if rsi is None or rsi == 0:
        return None

    # Find swing points in two windows: current half and previous half
    half = lookback // 2
    current_bars = bars[:half]
    previous_bars = bars[half:lookback]

    if len(current_bars) < 3 or len(previous_bars) < 3:
        return None

    # Price extremes
    curr_low = min(b.get("low", float("inf")) for b in current_bars)
    prev_low = min(b.get("low", float("inf")) for b in previous_bars)
    curr_high = max(b.get("high", 0) for b in current_bars)
    prev_high = max(b.get("high", 0) for b in previous_bars)

    # RSI at those extremes (approximate with RSI from bars at those points)
    # We use the bars close to the extreme points
    curr_rsi = calculate_rsi(bars[:half], rsi_period)
    prev_rsi = calculate_rsi(bars[half:lookback], rsi_period)

    if curr_rsi is None or prev_rsi is None:
        return None

    ema_val = calculate_ema(bars, ema_period)
    if ema_val == 0:
        return None

    direction = None

    # Bullish divergence: price lower low, RSI higher low
    if curr_low < prev_low and curr_rsi > prev_rsi:
        price_move = abs(prev_low - curr_low) / prev_low
        if price_move > min_div_pct and price < ema_val:
            direction = "BUY"

    # Bearish divergence: price higher high, RSI lower high
    if not direction and curr_high > prev_high and curr_rsi < prev_rsi:
        price_move = abs(curr_high - prev_high) / prev_high
        if price_move > min_div_pct and price > ema_val:
            direction = "SELL"

    if not direction:
        return None

    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "DIVERGENCE_RSI",
            "rsi": round(rsi, 2),
            "curr_rsi": round(curr_rsi, 2),
            "prev_rsi": round(prev_rsi, 2),
            "ema": round(ema_val, 2),
        },
    }
