"""
vt_early_entry.py — Anticipatory entry system (v3)

Enters trades BEFORE full signal confirmation when conditions are favorable.
Uses vt_signal_predictor.py for pre-signal analysis.

Key principles:
- Enter at 50% of normal size (reduced risk)
- Use tighter stops (1 ATR instead of 1.5 ATR)
- Require high confidence from predictor
- Only anticipate when volume/structure confirms
- Log all early entries separately for analysis

This module creates early entries that complement (not replace) normal signals.
"""

from vt_signal_predictor import analyze_pre_signal, format_prediction

STRATEGY_NAME = "EARLY_ENTRY"


def should_enter_early(symbol, tf, price, atr, bars, params, utils, strategy_name):
    """
    Determine if we should enter early before full signal.

    Args:
        symbol: trading symbol
        tf: timeframe
        price: current price
        atr: current ATR
        bars: price bars (newest first)
        params: config params
        utils: utility functions
        strategy_name: name of strategy that might generate signal

    Returns:
        None (no early entry) or dict:
        {
            "direction": "BUY" | "SELL",
            "sl_pts": int (tighter than normal),
            "size_mult": float (0.5 = half size),
            "reason": str,
            "confidence": float,
            "prediction": dict,
        }
    """
    if not bars or len(bars) < 30 or atr <= 0:
        return None

    # Get prediction from signal predictor
    prediction = analyze_pre_signal(symbol, tf, price, atr, bars, params, utils)
    if not prediction or not prediction["approaching_signal"]:
        return None

    # Require minimum confidence
    min_confidence = params.get("early_entry_min_confidence", 0.6)
    if prediction["confidence"] < min_confidence:
        return None

    direction = prediction["signal_direction"]
    if not direction:
        return None

    # Additional confirmation based on strategy type
    calc_sl = utils["calc_sl"]
    calculate_rsi = utils["calculate_rsi"]
    calculate_ema = utils["calculate_ema"]

    # Strategy-specific early entry conditions
    strategy_ok = False
    reason_parts = []

    # For RSI-based strategies: anticipate bounce from oversold/overbought
    if strategy_name in ["RSI_REVERSION", "BOLLINGER", "STOCHASTIC"]:
        rsi = calculate_rsi(bars, 14)
        if rsi and rsi > 0:
            if direction == "BUY" and rsi < 35:  # Approaching oversold
                strategy_ok = True
                reason_parts.append(f"RSI={rsi:.0f} approaching oversold")
            elif direction == "SELL" and rsi > 65:  # Approaching overbought
                strategy_ok = True
                reason_parts.append(f"RSI={rsi:.0f} approaching overbought")

    # For trend strategies: anticipate EMA crossover
    elif strategy_name in ["EMA_CROSSOVER", "EMA_PULLBACK", "ADX_TREND", "MACD_MOMENTUM"]:
        ema_9 = calculate_ema(bars, 9)
        ema_21 = calculate_ema(bars, 21)
        if ema_9 > 0 and ema_21 > 0:
            ema_diff_pct = (ema_9 - ema_21) / ema_21 * 100
            if direction == "BUY" and -0.1 < ema_diff_pct < 0.1:  # EMAs converging
                strategy_ok = True
                reason_parts.append(f"EMA converging ({ema_diff_pct:.2f}%)")
            elif direction == "SELL" and -0.1 < ema_diff_pct < 0.1:
                strategy_ok = True
                reason_parts.append(f"EMA converging ({ema_diff_pct:.2f}%)")

    # For breakout strategies: anticipate volatility expansion
    elif strategy_name in ["DONCHIAN_BREAKOUT", "VOLATILITY_BREAKOUT", "KELTNER_CHANNEL"]:
        if prediction["atr_compression"]:
            strategy_ok = True
            reason_parts.append(f"ATR squeeze ({prediction['atr_ratio']:.2f})")

    # For VWAP: anticipate price crossing VWAP
    elif strategy_name == "VWAP":
        vwap = utils["calculate_vwap"](bars, params.get("vwap_period", 20))
        if vwap > 0:
            vwap_dist_pct = abs(price - vwap) / vwap * 100
            if vwap_dist_pct < 0.2:  # Within 0.2% of VWAP
                strategy_ok = True
                reason_parts.append(f"Near VWAP ({vwap_dist_pct:.2f}%)")

    # Default: use generic conditions
    else:
        if prediction["near_key_level"] or prediction["volume_surge"]:
            strategy_ok = True
            if prediction["near_key_level"]:
                reason_parts.append(f"Near {prediction['key_level_type']}")
            if prediction["volume_surge"]:
                reason_parts.append(f"Volume surge ({prediction['volume_ratio']:.1f}x)")

    if not strategy_ok:
        return None

    # Calculate tighter SL for early entry
    normal_sl = calc_sl(symbol, atr, params)
    early_sl = int(atr * 1.0)  # Tighter: 1 ATR instead of 1.5

    # Build reason string
    reason = f"EARLY_{strategy_name}: {', '.join(reason_parts)}"

    return {
        "direction": direction,
        "sl_pts": early_sl,
        "size_mult": 0.5,  # Half size for early entries
        "reason": reason,
        "confidence": prediction["confidence"],
        "prediction": prediction,
    }


def format_early_entry(entry):
    """Format early entry for logging."""
    if not entry:
        return "no early entry"

    return (
        f"{entry['direction']} @ {entry['size_mult']:.0%} size | "
        f"SL={entry['sl_pts']}pts | "
        f"{entry['reason']} | "
        f"conf={entry['confidence']:.1f}"
    )
