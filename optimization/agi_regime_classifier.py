"""
AGI Regime Classifier — Classify trading days into market regimes.

Uses ATR and ADX from trade data to classify regimes:
  - TRENDING_STRONG: ADX > 25, directional moves
  - RANGING: ADX < 20, low ATR, sideways
  - HIGH_VOLATILITY: ATR > 1.5x 20-day average
  - LOW_VOLATILITY: ATR < 0.7x 20-day average

Also reads trade_analysis_YYYYMMDD.md files to separate execution errors
(slippage, latency) from logic errors.

Usage:
    from agi_regime_classifier import classify_regimes, classify_current_regime
    regimes = classify_regimes(trades, days=30)
    current = classify_current_regime(trades)
"""

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger("agi_regime")

PROJECT_DIR = Path(__file__).parent.parent
DATA_DIR = PROJECT_DIR / "data"

# Regime constants
REGIMES = ("TRENDING_STRONG", "RANGING", "HIGH_VOLATILITY", "LOW_VOLATILITY")

# Risk tags for next day (from Stage 2 macro intel)
RISK_TAGS = (
    "HIGH_VOLATILITY_EXPECTED",
    "TREND_DAY_PROBABLE",
    "LOW_VOLATILITY_EXPECTED",
    "EVENT_RISK",
)


def classify_regime_from_atr_adx(atr: float, atr_avg: float, adx: float) -> str:
    """Classify a single day's regime from ATR, ATR average, and ADX values.

    Args:
        atr: Current day's ATR value.
        atr_avg: 20-day average ATR for context.
        adx: Current ADX value (trend strength).

    Returns:
        One of: TRENDING_STRONG, RANGING, HIGH_VOLATILITY, LOW_VOLATILITY.
    """
    if atr_avg <= 0:
        # No ATR context — safe default (can't confirm trend without ATR data)
        return "RANGING"

    atr_ratio = atr / atr_avg

    # ADX > 25 = strong trend
    if adx > 25:
        return "TRENDING_STRONG"

    # High volatility: ATR > 1.5x average
    if atr_ratio > 1.5:
        return "HIGH_VOLATILITY"

    # Low volatility: ATR < 0.7x average
    if atr_ratio < 0.7:
        return "LOW_VOLATILITY"

    # Default: ranging
    return "RANGING"


def classify_regimes_from_trades(trades: list[dict], days: int = 30) -> dict:
    """Classify trading days into regimes based on trade data.

    Groups trades by day, computes daily ATR/ADX averages, and classifies.

    Args:
        trades: List of trade dicts (from SQLite). Each should have
                entry_time, net_pnl, and optionally signal_detail (JSON).
        days: Number of days to look back.

    Returns:
        dict with keys:
        - daily_regimes: dict[date_str, regime_str]
        - regime_counts: dict[regime_str, count]
        - dominant_regime: str (most frequent)
        - current_regime: str (today's or last day's)
    """
    if not trades:
        return {
            "daily_regimes": {},
            "regime_counts": {r: 0 for r in REGIMES},
            "dominant_regime": "RANGING",
            "current_regime": "RANGING",
        }

    # Group trades by day
    daily_data = {}
    for t in trades:
        entry = t.get("entry_time", "")
        if not entry:
            continue
        day_str = str(entry)[:10]  # YYYY-MM-DD
        if day_str not in daily_data:
            daily_data[day_str] = {"trades": [], "atr_values": [], "adx_values": []}
        daily_data[day_str]["trades"].append(t)

        # Extract ATR/ADX from signal_detail if available
        signal = t.get("signal_detail")
        if isinstance(signal, str):
            try:
                signal = json.loads(signal)
            except (json.JSONDecodeError, TypeError):
                signal = None
        if isinstance(signal, dict):
            atr_val = signal.get("atr")
            adx_val = signal.get("adx")
            if atr_val is not None:
                daily_data[day_str]["atr_values"].append(float(atr_val))
            if adx_val is not None:
                daily_data[day_str]["adx_values"].append(float(adx_val))

    # Compute global ATR average (20-day)
    all_atr = []
    for d in daily_data.values():
        all_atr.extend(d["atr_values"])
    global_atr_avg = sum(all_atr) / len(all_atr) if all_atr else 1.0

    # Classify each day
    daily_regimes = {}
    for day_str, d in sorted(daily_data.items()):
        atr_vals = d["atr_values"]
        adx_vals = d["adx_values"]

        if atr_vals:
            day_atr = sum(atr_vals) / len(atr_vals)
        else:
            # Estimate from trade PnL volatility
            pnls = [t.get("net_pnl", 0) for t in d["trades"]]
            day_atr = abs(max(pnls) - min(pnls)) if len(pnls) > 1 else global_atr_avg

        day_adx = sum(adx_vals) / len(adx_vals) if adx_vals else 20.0  # neutral default

        regime = classify_regime_from_atr_adx(day_atr, global_atr_avg, day_adx)
        daily_regimes[day_str] = regime

    # Count regimes
    regime_counts = {}
    for r in REGIMES:
        regime_counts[r] = sum(1 for v in daily_regimes.values() if v == r)

    # Dominant = most frequent
    dominant = max(regime_counts, key=lambda k: regime_counts.get(k, 0)) if regime_counts else "RANGING"

    # Current = last day
    sorted_days = sorted(daily_regimes.keys())
    current = daily_regimes[sorted_days[-1]] if sorted_days else "RANGING"

    return {
        "daily_regimes": daily_regimes,
        "regime_counts": regime_counts,
        "dominant_regime": dominant,
        "current_regime": current,
    }


def classify_current_regime(trades: list[dict]) -> str:
    """Quick helper: return just the current regime string.

    Args:
        trades: List of trade dicts from the last N days.

    Returns:
        Regime string for the most recent trading day.
    """
    result = classify_regimes_from_trades(trades)
    return result["current_regime"]


# ═══════════════════════════════════════════════════════════════════
# Trade Analysis File Parser (Stage 1.3)
# ═══════════════════════════════════════════════════════════════════

# Patterns to detect execution errors vs logic errors
EXECUTION_ERROR_PATTERNS = [
    r"slippage",
    r"latência",
    r"latency",
    r"requote",
    r"rejeitad[ao]",
    r"timeout.*execução",
    r"ordem.*não.*executad",
    r"preço.*mudou",
    r"spread.*alto",
    r"connection.*lost",
    r"disconnect",
    r"server.*error",
]

LOGIC_ERROR_PATTERNS = [
    r"entrada.*errada",
    r"sinal.*falso",
    r"stop.*atingido.*rápido",
    r"trend.*reversal",
    r"WR.*baixa",
    r"drawdown",
    r"perda.*sequencial",
    r"entry.*filter.*failed",
    r"wrong.*direction",
]


def parse_trade_analysis_files(days: int = 7) -> dict:
    """Read data/trade_analysis_YYYYMMDD.md files to classify errors.

    Separates execution errors (slippage, latency) from logic errors
    (bad entries, false signals).

    Args:
        days: Number of days to look back.

    Returns:
        dict with:
        - execution_errors: list of {date, description, count}
        - logic_errors: list of {date, description, count}
        - raw_files: dict[date_str, file_content]
    """
    execution_errors = []
    logic_errors = []
    raw_files = {}

    if not DATA_DIR.exists():
        return {
            "execution_errors": execution_errors,
            "logic_errors": logic_errors,
            "raw_files": raw_files,
        }

    cutoff = datetime.now() - timedelta(days=days)

    for md_file in sorted(DATA_DIR.glob("trade_analysis_*.md")):
        # Extract date from filename
        match = re.search(r"(\d{8})", md_file.name)
        if not match:
            continue
        date_str = match.group(1)
        try:
            file_date = datetime.strptime(date_str, "%Y%m%d")
        except ValueError:
            continue
        if file_date < cutoff:
            continue

        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception:
            continue

        raw_files[date_str] = content

        # Scan line by line for error patterns
        for line in content.split("\n"):
            line_lower = line.lower().strip()
            if not line_lower:
                continue

            is_execution = any(
                re.search(p, line_lower) for p in EXECUTION_ERROR_PATTERNS
            )
            is_logic = any(
                re.search(p, line_lower) for p in LOGIC_ERROR_PATTERNS
            )

            if is_execution:
                execution_errors.append({
                    "date": date_str,
                    "description": line.strip()[:200],
                    "type": "execution",
                })
            elif is_logic:
                logic_errors.append({
                    "date": date_str,
                    "description": line.strip()[:200],
                    "type": "logic",
                })

    return {
        "execution_errors": execution_errors,
        "logic_errors": logic_errors,
        "raw_files": {k: v[:500] for k, v in raw_files.items()},  # truncate
    }


def describe_regime(regime: str) -> str:
    """Return a human-readable description of a regime (Portuguese)."""
    descriptions = {
        "TRENDING_STRONG": "Tendência Forte (ADX > 25)",
        "RANGING": "Lateralidade (ADX < 20)",
        "HIGH_VOLATILITY": "Alta Volatilidade (ATR > 1.5x média)",
        "LOW_VOLATILITY": "Baixa Volatilidade (ATR < 0.7x média)",
    }
    return descriptions.get(regime, regime)
