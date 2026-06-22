#!/usr/bin/env python3
"""
vt_trade_scorer.py — Trade quality scorer.

Scores trades 0-100 based on:
  - Trend alignment (higher TF trend agrees)
  - Volume confirmation (above average)
  - Volatility regime (not too tight, not too wild)
  - Time of day (avoid first/last 15 min, avoid lunch hour)
  - Recent performance (reduce size after consecutive losses)

Only take trades scoring > 60.
"""

from datetime import datetime


def score_trade(
    symbol: str,
    tf: str,
    direction: str,
    price: float,
    atr: float,
    bars: list,
    params: dict,
    utils: dict,
    consecutive_losses: int = 0,
    daily_pnl: float = 0,
    min_score: int = 60,
) -> dict:
    """
    Score a potential trade 0-100.

    Args:
        min_score: Minimum score threshold for approval (default 60). 0 = always approve.

    Returns:
        {
            "score": int (0-100),
            "approved": bool (score > min_score),
            "components": {name: score, ...},
            "reason": str,
        }
    """
    try:
        components = {}

        # === 1. TREND ALIGNMENT (0-25 points) ===
        components["trend"] = _score_trend(direction, bars, params, utils)

        # === 2. VOLUME CONFIRMATION (0-20 points) ===
        components["volume"] = _score_volume(bars)

        # === 3. VOLATILITY REGIME (0-20 points) ===
        components["volatility"] = _score_volatility(atr, price, params)

        # === 4. TIME OF DAY (0-20 points) ===
        components["time"] = _score_time_of_day()

        # === 5. RECENT PERFORMANCE (0-15 points) ===
        components["performance"] = _score_performance(consecutive_losses, daily_pnl, params)

        total = sum(components.values())
        total = min(max(total, 0), 100)
        approved = total > min_score

        reason_parts = [f"{k}={v}" for k, v in components.items() if v < 15]
        reason = f"score={total}/100"
        if not approved:
            reason += f" (low: {', '.join(reason_parts)})" if reason_parts else ""

        return {
            "score": total,
            "approved": approved,
            "components": components,
            "reason": reason,
        }

    except Exception as e:
        # Graceful degradation: approve on error
        return {
            "score": 70,
            "approved": True,
            "components": {},
            "reason": f"error_fallback: {e}",
        }


def _score_trend(direction: str, bars: list, params: dict, utils: dict) -> int:
    """Score trend alignment (0-25)."""
    try:
        calculate_ema = utils.get("calculate_ema")
        if not calculate_ema or not bars or len(bars) < 30:
            return 15  # Neutral

        ema_fast = calculate_ema(bars, params.get("ema_fast", 9))
        ema_slow = calculate_ema(bars, params.get("ema_slow", 21))

        if ema_fast == 0 or ema_slow == 0:
            return 15

        # Check alignment
        if direction == "BUY":
            if ema_fast > ema_slow:
                # Check strength of trend
                spread = (ema_fast - ema_slow) / ema_slow * 100
                if spread > 0.5:
                    return 25  # Strong uptrend
                return 20  # Weak uptrend
            return 5  # Against trend
        else:
            if ema_fast < ema_slow:
                spread = (ema_slow - ema_fast) / ema_slow * 100
                if spread > 0.5:
                    return 25  # Strong downtrend
                return 20  # Weak downtrend
            return 5  # Against trend
    except Exception:
        return 15


def _score_volume(bars: list) -> int:
    """Score volume confirmation (0-20)."""
    try:
        if not bars or len(bars) < 20:
            return 12  # Neutral

        recent_vol = sum(b.get("tick_volume", b.get("volume", 1)) for b in bars[:3]) / 3
        avg_vol = sum(b.get("tick_volume", b.get("volume", 1)) for b in bars[:20]) / 20

        if avg_vol <= 0:
            return 12

        ratio = recent_vol / avg_vol

        if ratio >= 1.5:
            return 20  # Strong volume
        elif ratio >= 1.0:
            return 16  # Above average
        elif ratio >= 0.7:
            return 10  # Below average
        else:
            return 4  # Very low volume
    except Exception:
        return 12


def _score_volatility(atr: float, price: float, params: dict) -> int:
    """Score volatility regime (0-20). Not too tight, not too wild."""
    try:
        if price <= 0 or atr <= 0:
            return 12

        atr_pct = atr / price

        # Sweet spot: 0.1% - 1.0% of price
        if 0.001 <= atr_pct <= 0.01:
            return 20  # Ideal volatility
        elif 0.0005 <= atr_pct < 0.001:
            return 14  # Slightly tight
        elif 0.01 < atr_pct <= 0.02:
            return 14  # Slightly wild
        elif atr_pct < 0.0005:
            return 6  # Too tight (no movement)
        else:
            return 6  # Too wild (risky)
    except Exception:
        return 12


def _score_time_of_day() -> int:
    """Score time of day (0-20). Avoid first/last 15 min, lunch hour."""
    try:
        now = datetime.now()
        h, m = now.hour, now.minute
        time_min = h * 60 + m

        # Market hours: 9:00 - 17:00 BRT
        # Avoid: 9:00-9:15 (opening volatility)
        # Avoid: 12:00-13:30 (low liquidity lunch)
        # Avoid: 16:45-17:00 (closing volatility)
        if time_min < 9 * 60 + 15:
            return 4  # Opening chaos
        elif time_min >= 16 * 60 + 45:
            return 4  # Closing chaos
        elif 12 * 60 <= time_min < 13 * 60 + 30:
            return 8  # Lunch hour
        elif 9 * 60 + 15 <= time_min < 10 * 60:
            return 14  # Early morning (still volatile)
        elif 15 * 60 + 30 <= time_min < 16 * 60 + 45:
            return 14  # Late afternoon (volatile)
        else:
            return 20  # Prime trading hours
    except Exception:
        return 15


def _score_performance(consecutive_losses: int, daily_pnl: float, params: dict) -> int:
    """Score recent performance (0-15). Reduce after consecutive losses."""
    try:
        score = 15

        # Penalty for consecutive losses
        if consecutive_losses >= 3:
            score -= 10
        elif consecutive_losses >= 2:
            score -= 6
        elif consecutive_losses >= 1:
            score -= 3

        # Penalty for large daily loss
        max_loss = params.get("max_daily_loss", -1000)
        if daily_pnl < max_loss * 0.5:
            score -= 5
        elif daily_pnl < max_loss * 0.75:
            score -= 3

        return max(score, 0)
    except Exception:
        return 10


def format_score(score_data: dict) -> str:
    """Format score for logging."""
    score = score_data["score"]
    approved = "APPROVED" if score_data["approved"] else "REJECTED"
    components = score_data.get("components", {})
    parts = [f"{k}={v}" for k, v in components.items()]
    return f"[TRADE_SCORE] {approved} {score}/100 | {', '.join(parts)}"
