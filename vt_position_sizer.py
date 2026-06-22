"""
vt_position_sizer.py — Smart position sizing (v3)

Dynamically adjusts position size based on:
- Base size from config
- Consecutive losses (reduce after 2+)
- Market volatility (reduce in high volatility)
- Trend strength (increase in strong trend)
- Trade quality score (from vt_trade_scorer)
- Early entry flag (half size)

Limits:
- Max multiplier: 2.0x
- Min multiplier: 0.25x

This module does NOT execute trades. It returns a multiplier
that the autotrader applies to the base volume.
"""

STRATEGY_NAME = "POSITION_SIZER"


def calculate_position_size(
    base_volume,
    consecutive_losses,
    atr,
    price,
    adx_val,
    trade_score,
    is_early_entry=False,
    params=None,
):
    """
    Calculate adjusted position size.

    Args:
        base_volume: base lot size from config
        consecutive_losses: number of consecutive losses for this symbol/tf
        atr: current ATR
        price: current price
        adx_val: current ADX value (trend strength)
        trade_score: score from vt_trade_scorer (0-100, or None)
        is_early_entry: True if this is an early/anticipatory entry
        params: config params dict

    Returns:
        dict:
        {
            "volume": float (adjusted volume),
            "multiplier": float (0.25 to 2.0),
            "reason": str,
        }
    """
    if params is None:
        params = {}

    # Get config limits
    max_mult = params.get("position_sizer_max_mult", 2.0)
    min_mult = params.get("position_sizer_min_mult", 0.25)

    multiplier = 1.0
    reasons = []

    # 1. CONSECUTIVE LOSSES — reduce after 2+
    loss_reduction = params.get("position_sizer_loss_reduction", 0.5)
    loss_threshold = params.get("position_sizer_loss_threshold", 2)

    if consecutive_losses >= loss_threshold + 2:  # 4+ losses = minimum size
        multiplier *= loss_reduction * loss_reduction  # 0.25x
        reasons.append(f"CONSEC_LOSS_{consecutive_losses}")
    elif consecutive_losses >= loss_threshold + 1:  # 3 losses
        multiplier *= loss_reduction * 0.75  # 0.375x
        reasons.append(f"CONSEC_LOSS_{consecutive_losses}")
    elif consecutive_losses >= loss_threshold:  # 2 losses
        multiplier *= loss_reduction  # 0.5x
        reasons.append(f"CONSEC_LOSS_{consecutive_losses}")

    # 2. VOLATILITY — reduce in high volatility
    if atr > 0 and price > 0:
        atr_pct = atr / price * 100
        vol_threshold = params.get("position_sizer_vol_threshold", 0.15)  # 0.15%

        if atr_pct > vol_threshold * 3:  # Extreme volatility
            multiplier *= 0.5
            reasons.append(f"HIGH_VOL({atr_pct:.2f}%)")
        elif atr_pct > vol_threshold * 2:  # High volatility
            multiplier *= 0.75
            reasons.append(f"HIGH_VOL({atr_pct:.2f}%)")
        elif atr_pct < vol_threshold * 0.5:  # Very low volatility
            multiplier *= 0.75  # Reduce in dead markets too
            reasons.append(f"LOW_VOL({atr_pct:.2f}%)")

    # 3. TREND STRENGTH — increase in strong trends
    trend_threshold = params.get("position_sizer_trend_threshold", 25)

    if adx_val > 0:
        if adx_val > trend_threshold * 2:  # Very strong trend (50+)
            multiplier *= 1.5
            reasons.append(f"STRONG_TREND(ADX={adx_val:.0f})")
        elif adx_val > trend_threshold * 1.5:  # Strong trend (37+)
            multiplier *= 1.25
            reasons.append(f"TREND(ADX={adx_val:.0f})")
        elif adx_val < trend_threshold * 0.5:  # Weak trend (<12)
            multiplier *= 0.75
            reasons.append(f"WEAK_TREND(ADX={adx_val:.0f})")

    # 4. TRADE QUALITY SCORE — boost good setups
    if trade_score is not None and trade_score > 0:
        score_threshold = params.get("position_sizer_score_threshold", 70)

        if trade_score >= 90:  # Excellent setup
            multiplier *= 1.3
            reasons.append(f"EXCELLENT_SCORE({trade_score})")
        elif trade_score >= score_threshold:  # Good setup
            multiplier *= 1.15
            reasons.append(f"GOOD_SCORE({trade_score})")
        elif trade_score < 40:  # Poor setup
            multiplier *= 0.75
            reasons.append(f"LOW_SCORE({trade_score})")

    # 5. EARLY ENTRY — half size for anticipatory entries
    if is_early_entry:
        multiplier *= 0.5
        reasons.append("EARLY_ENTRY")

    # Apply limits
    multiplier = max(min(multiplier, max_mult), min_mult)

    # Calculate final volume (round to 0.01 lots)
    volume = round(base_volume * multiplier, 2)
    volume = max(volume, 0.01)  # Minimum 0.01 lots

    reason_str = ", ".join(reasons) if reasons else "BASE_SIZE"

    return {
        "volume": volume,
        "multiplier": multiplier,
        "reason": reason_str,
    }


def format_position_size(size_info):
    """Format position size info for logging."""
    if not size_info:
        return "no sizing info"

    return f"{size_info['volume']:.2f} lots ({size_info['multiplier']:.2f}x) | {size_info['reason']}"
