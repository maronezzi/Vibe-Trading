#!/usr/bin/env python3
"""
vt_regime_detector.py — Market regime detection.

Classifies market as TRENDING, RANGING, or VOLATILE.
Each regime activates different strategies:
  - TRENDING: ADX_TREND, SUPERTREND, EMA_CROSSOVER
  - RANGING: RSI_REVERSION, BOLLINGER, RANGE_TRADING
  - VOLATILE: DONCHIAN_BREAKOUT, VOLATILITY_BREAKOUT

Uses ADX, ATR, Bollinger Band width, and price action.
Integrates into strategy selection in vt_autotrader.py.
"""

# Strategy affinity by regime
REGIME_STRATEGIES = {
    "TRENDING": [
        "ADX_TREND",
        "SUPERTREND",
        "EMA_CROSSOVER",
        "EMA_PULLBACK",
        "TRIPLE_EMA",
        "STRONG_TREND",
        "MOMENTUM_BREAKOUT",
    ],
    "RANGING": [
        "RSI_REVERSION",
        "BOLLINGER",
        "RANGE_TRADING",
        "PIVOT_POINTS",
        "WIN_REVERSION",
        "MEAN_REVERSION_ZSCORE",
    ],
    "VOLATILE": [
        "DONCHIAN_BREAKOUT",
        "VOLATILITY_BREAKOUT",
        "KELTNER_CHANNEL",
        "HEIKIN_ASHI",
        "CANDLE_PATTERNS",
    ],
}

# Strategies that work in any regime
UNIVERSAL_STRATEGIES = [
    "VWAP",
    "FIBONACCI_RETRACEMENT",
    "ICHIMOKU",
    "STOCHASTIC",
    "DIVERGENCE_RSI",
    "SMART_EMA",
    "MACD_MOMENTUM",
]


def detect_regime(bars: list, params: dict, utils: dict) -> dict:
    """
    Detect current market regime.

    Returns:
        {
            "regime": "TRENDING" | "RANGING" | "VOLATILE",
            "adx": float,
            "atr_pct": float,
            "bb_width_pct": float,
            "confidence": float (0-1),
            "suitable_strategies": list,
        }
    """
    try:
        if not bars or len(bars) < 30:
            return _default_regime()

        # === ADX for trend strength ===
        calculate_adx = utils.get("calculate_adx")
        adx_val = 0
        plus_di = 0
        minus_di = 0
        if calculate_adx:
            adx_val, plus_di, minus_di = calculate_adx(bars, params.get("adx_period", 14))

        # === ATR for volatility ===
        calculate_atr = utils.get("calculate_atr")
        atr_val = 0
        if calculate_atr:
            atr_val = calculate_atr(bars, params.get("atr_period", 14))

        price = bars[0]["close"]
        atr_pct = (atr_val / price) if price > 0 else 0

        # === Bollinger Band width for range/volatility ===
        calculate_bollinger = utils.get("calculate_bollinger")
        bb_width_pct = 0
        if calculate_bollinger:
            bb_result = calculate_bollinger(bars, params.get("bb_period", 20), params.get("bb_std", 2.0))
            if bb_result and len(bb_result) == 3:
                upper, mid, lower = bb_result
                if mid > 0:
                    bb_width_pct = (upper - lower) / mid

        # === Price action: recent range vs historical ===
        range_score = _compute_range_score(bars)

        # === Classify regime ===
        regime, confidence = _classify(adx_val, atr_pct, bb_width_pct, range_score, params)

        suitable = REGIME_STRATEGIES.get(regime, [])
        suitable = suitable + UNIVERSAL_STRATEGIES

        return {
            "regime": regime,
            "adx": adx_val,
            "atr_pct": atr_pct,
            "bb_width_pct": bb_width_pct,
            "range_score": range_score,
            "confidence": confidence,
            "suitable_strategies": suitable,
        }

    except Exception:
        return _default_regime()


def _classify(adx: float, atr_pct: float, bb_width_pct: float, range_score: float, params: dict) -> tuple:
    """Classify regime based on indicators. Returns (regime, confidence)."""

    adx_threshold = params.get("adx_threshold", 25)

    # TRENDING: ADX high, price moving in one direction
    if adx >= adx_threshold and adx >= 20:
        confidence = min(adx / 50.0, 1.0)
        if range_score < 0.4:  # Strong directional movement
            return "TRENDING", confidence

    # VOLATILE: Wide BB, high ATR, big moves
    if bb_width_pct > 0.04 or atr_pct > 0.015:
        confidence = min(atr_pct / 0.02, 1.0) if atr_pct > 0 else 0.5
        return "VOLATILE", confidence

    # RANGING: Low ADX, tight BB, sideways
    if adx < 20 or range_score > 0.6:
        confidence = 1.0 - (adx / 30.0) if adx > 0 else 0.5
        return "RANGING", max(confidence, 0.3)

    # Default: trending with low confidence
    return "TRENDING", 0.3


def _compute_range_score(bars: list) -> float:
    """
    Compute range score (0 = trending, 1 = ranging).
    Compares recent price range to overall movement.
    """
    try:
        if len(bars) < 20:
            return 0.5

        # Recent 10 bars range
        recent_highs = [b["high"] for b in bars[:10]]
        recent_lows = [b["low"] for b in bars[:10]]
        recent_range = max(recent_highs) - min(recent_lows)

        # Overall 20 bars movement
        all_highs = [b["high"] for b in bars[:20]]
        all_lows = [b["low"] for b in bars[:20]]
        overall_range = max(all_highs) - min(all_lows)

        if overall_range <= 0:
            return 0.5

        # If recent range covers most of overall range → ranging
        # If recent range is small vs overall → trending
        score = recent_range / overall_range
        return min(max(score, 0), 1.0)
    except Exception:
        return 0.5


def _default_regime() -> dict:
    return {
        "regime": "TRENDING",
        "adx": 0,
        "atr_pct": 0,
        "bb_width_pct": 0,
        "range_score": 0.5,
        "confidence": 0.0,
        "suitable_strategies": UNIVERSAL_STRATEGIES,
    }


def is_strategy_suitable(strategy: str, regime: str, mode: str = "strict", confidence: float = 0.0) -> bool:
    """Check if a strategy is suitable for the current regime.

    Args:
        strategy: Strategy name to check
        regime: Current market regime (TRENDING, RANGING, VOLATILE)
        mode: Filter mode - "strict", "soft", or "off"
              - strict: Only allow strategies in the regime list (original behavior)
              - soft: Only reject when confidence > 0.6 AND strategy not in regime list
              - off: Always allow all strategies (pass-through)
        confidence: Regime classification confidence (0.0 to 1.0)

    Returns:
        True if strategy is allowed, False if filtered out
    """
    # Universal strategies always pass
    if strategy in UNIVERSAL_STRATEGIES:
        return True

    # Off mode: pass everything
    if mode == "off":
        return True

    # Check if strategy is in the regime list
    in_regime_list = strategy in REGIME_STRATEGIES.get(regime, [])

    # Strict mode: must be in regime list
    if mode == "strict":
        return in_regime_list

    # Soft mode: only reject if confidence is high AND strategy not in regime list
    if mode == "soft":
        # If confidence is low, allow all strategies (uncertain regime)
        if confidence < 0.6:
            return True
        # If confidence is high, only allow strategies in the regime list
        return in_regime_list

    # Default fallback: strict behavior
    return in_regime_list


def format_regime(regime_data: dict) -> str:
    """Format regime data for logging."""
    regime = regime_data["regime"]
    conf = regime_data["confidence"]
    adx = regime_data["adx"]
    return f"[REGIME] {regime} (conf={conf:.2f}, ADX={adx:.1f})"
