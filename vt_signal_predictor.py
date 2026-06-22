"""
vt_signal_predictor.py — Predictive entry system (v3)

Monitors price action BEFORE signals fire to anticipate entries.
Detects:
- Key support/resistance levels
- Momentum shifts
- Volume patterns
- ATR compression (volatility squeeze)
- Price approaching strategy trigger zones

This module does NOT generate signals. It provides early context
to vt_early_entry.py for anticipatory entries.
"""

STRATEGY_NAME = "SIGNAL_PREDICTOR"


def analyze_pre_signal(symbol, tf, price, atr, bars, params, utils):
    """
    Analyze price action before signals fire.

    Returns:
        dict with predictive analysis, or None if no useful context.
        {
            "momentum_shift": float (-1 to 1, 0 = neutral),
            "near_key_level": bool,
            "key_level_type": "support" | "resistance" | None,
            "key_level_price": float,
            "atr_compression": bool,
            "atr_ratio": float,
            "volume_surge": bool,
            "volume_ratio": float,
            "approaching_signal": bool,
            "signal_direction": "BUY" | "SELL" | None,
            "confidence": float (0 to 1),
        }
    """
    if not bars or len(bars) < 30:
        return None

    calculate_ema = utils["calculate_ema"]
    calculate_rsi = utils["calculate_rsi"]

    result = {
        "momentum_shift": 0.0,
        "near_key_level": False,
        "key_level_type": None,
        "key_level_price": 0.0,
        "atr_compression": False,
        "atr_ratio": 1.0,
        "volume_surge": False,
        "volume_ratio": 1.0,
        "approaching_signal": False,
        "signal_direction": None,
        "confidence": 0.0,
    }

    # 1. MOMENTUM SHIFT DETECTION
    # Compare recent momentum vs earlier momentum
    closes = [float(b["close"]) for b in bars[:20]]
    if len(closes) >= 20:
        # Recent momentum (last 5 bars)
        recent_mom = (closes[0] - closes[4]) / closes[4] if closes[4] != 0 else 0
        # Earlier momentum (bars 10-15)
        earlier_mom = (closes[9] - closes[14]) / closes[14] if closes[14] != 0 else 0

        # Momentum shift: positive = bullish acceleration, negative = bearish
        mom_shift = recent_mom - earlier_mom
        result["momentum_shift"] = min(max(mom_shift * 100, -1.0), 1.0)  # normalize

    # 2. KEY LEVEL DETECTION
    # Find recent swing highs/lows as support/resistance
    highs = [float(b["high"]) for b in bars[:50]]
    lows = [float(b["low"]) for b in bars[:50]]

    if len(highs) >= 20 and len(lows) >= 20:
        # Simple pivot detection
        swing_highs = []
        swing_lows = []
        for i in range(2, min(20, len(highs) - 2)):
            if (
                highs[i] > highs[i - 1]
                and highs[i] > highs[i - 2]
                and highs[i] > highs[i + 1]
                and highs[i] > highs[i + 2]
            ):
                swing_highs.append(highs[i])
            if lows[i] < lows[i - 1] and lows[i] < lows[i - 2] and lows[i] < lows[i + 1] and lows[i] < lows[i + 2]:
                swing_lows.append(lows[i])

        # Check if price is near a key level
        atr_val = atr if atr > 0 else 100
        tolerance = atr_val * 0.3  # within 0.3 ATR of key level

        for sh in swing_highs:
            if abs(price - sh) < tolerance:
                result["near_key_level"] = True
                result["key_level_type"] = "resistance"
                result["key_level_price"] = sh
                break

        if not result["near_key_level"]:
            for sl_val in swing_lows:
                if abs(price - sl_val) < tolerance:
                    result["near_key_level"] = True
                    result["key_level_type"] = "support"
                    result["key_level_price"] = sl_val
                    break

    # 3. ATR COMPRESSION (volatility squeeze)
    # Compare current ATR to recent average
    if len(bars) >= 30:
        atr_values = []
        for i in range(25):
            segment = bars[i : i + 10]
            seg_highs = [float(b["high"]) for b in segment]
            seg_lows = [float(b["low"]) for b in segment]
            if seg_highs and seg_lows:
                seg_atr = max(seg_highs) - min(seg_lows)
                atr_values.append(seg_atr)

        if atr_values and atr > 0:
            avg_atr = sum(atr_values) / len(atr_values)
            if avg_atr > 0:
                atr_ratio = atr / avg_atr
                result["atr_ratio"] = atr_ratio
                result["atr_compression"] = atr_ratio < 0.7  # ATR compressed to 70% of average

    # 4. VOLUME SURGE DETECTION
    if bars and len(bars) >= 20:
        volumes = [float(b.get("volume", b.get("tick_volume", 1)) or 1) for b in bars[:20]]
        if len(volumes) >= 20:
            recent_vol = sum(volumes[:5]) / 5
            avg_vol = sum(volumes) / 20
            if avg_vol > 0:
                vol_ratio = recent_vol / avg_vol
                result["volume_ratio"] = vol_ratio
                result["volume_surge"] = vol_ratio > 1.5  # 50% above average

    # 5. APPROACHING SIGNAL DETECTION
    # Check if price is moving toward a trigger zone
    ema_9 = calculate_ema(bars, 9)
    ema_21 = calculate_ema(bars, 21)
    rsi = calculate_rsi(bars, 14)

    if ema_9 > 0 and ema_21 > 0 and rsi and rsi > 0:
        # Approaching BUY signal: price dropping toward oversold + near support
        if rsi < 40 and result["key_level_type"] == "support" and result["near_key_level"]:
            result["approaching_signal"] = True
            result["signal_direction"] = "BUY"

        # Approaching SELL signal: price rising toward overbought + near resistance
        elif rsi > 60 and result["key_level_type"] == "resistance" and result["near_key_level"]:
            result["approaching_signal"] = True
            result["signal_direction"] = "SELL"

        # Approaching trend signal: EMA convergence
        elif abs(ema_9 - ema_21) / ema_21 < 0.001:  # EMAs very close
            if result["momentum_shift"] > 0.3:
                result["approaching_signal"] = True
                result["signal_direction"] = "BUY"
            elif result["momentum_shift"] < -0.3:
                result["approaching_signal"] = True
                result["signal_direction"] = "SELL"

    # 6. CONFIDENCE SCORE
    confidence = 0.0
    if result["approaching_signal"]:
        confidence += 0.4
    if result["near_key_level"]:
        confidence += 0.2
    if result["atr_compression"]:
        confidence += 0.2  # Squeeze often precedes moves
    if result["volume_surge"]:
        confidence += 0.2
    result["confidence"] = min(confidence, 1.0)

    return result


def format_prediction(pred):
    """Format prediction for logging."""
    if not pred:
        return "no prediction"

    parts = []
    if pred["approaching_signal"]:
        parts.append(f"APPROACHING_{pred['signal_direction']}")
    if pred["near_key_level"]:
        parts.append(f"NEAR_{pred['key_level_type'].upper()}@{pred['key_level_price']:.0f}")
    if pred["atr_compression"]:
        parts.append(f"SQUEEZE({pred['atr_ratio']:.2f})")
    if pred["volume_surge"]:
        parts.append(f"VOL_SURGE({pred['volume_ratio']:.1f}x)")

    if not parts:
        return f"neutral (conf={pred['confidence']:.1f})"

    return f"{', '.join(parts)} (conf={pred['confidence']:.1f})"
