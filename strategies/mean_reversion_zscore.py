"""
Estratégia MEAN_REVERSION_ZSCORE — Reversão à média via Z-Score.
Entra quando preço desvia N desvios-padrão da média.

Sinal de entrada:
- BUY: z-score < -z_threshold (preço muito abaixo da média)
- SELL: z-score > z_threshold (preço muito acima da média)

Parâmetros (via vt_config.json):
  lookback, z_threshold, rsi_period, rsi_overbought, rsi_oversold
  sl_atr_mult, trail_activate, trail_distance
"""

STRATEGY_NAME = "MEAN_REVERSION_ZSCORE"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada MEAN_REVERSION_ZSCORE.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calc_sl = utils["calc_sl"]
    calculate_rsi = utils["calculate_rsi"]

    lookback = params.get("lookback", 50)
    z_threshold = params.get("z_threshold", 2.0)
    rsi_period = params.get("rsi_period", 14)
    rsi_ob = params.get("rsi_overbought", 70)
    rsi_os = params.get("rsi_oversold", 30)

    min_bars = max(lookback, rsi_period) + 5
    if not bars or len(bars) < min_bars:
        return None

    # Calculate mean and std from close prices
    closes = [b.get("close", 0) for b in bars[:lookback]]
    closes = [c for c in closes if c > 0]

    if len(closes) < 10:
        return None

    mean = sum(closes) / len(closes)
    if mean == 0:
        return None

    variance = sum((c - mean) ** 2 for c in closes) / len(closes)
    std = variance ** 0.5

    if std == 0:
        return None

    z_score = (price - mean) / std

    rsi = calculate_rsi(bars, rsi_period)
    if rsi is None:
        rsi = 50

    direction = None

    # BUY: price far below mean (oversold)
    if z_score < -z_threshold and rsi < rsi_os + 10:
        direction = "BUY"
    # SELL: price far above mean (overbought)
    elif z_score > z_threshold and rsi > rsi_ob - 10:
        direction = "SELL"

    if not direction:
        return None

    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "MEAN_REVERSION_ZSCORE",
            "z_score": round(z_score, 3),
            "mean": round(mean, 2),
            "std": round(std, 2),
            "rsi": round(rsi, 2),
        },
    }
