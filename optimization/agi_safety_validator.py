"""
AGI Safety Validator — Pydantic schema for LLM output validation.

Implements the 3 Safety Pillars:
  1. Occam's Razor (Complexity Penalty): fitness penalizes many params
  2. Cost of Not Trading (Patience Filter): min_atr_for_entry is critical
  3. Brutal Reality (Embedded Costs): Net Profit must > Total_Cost * 2

Also validates LLM JSON output against rigid schema before application.
LLM CANNOT suggest rsi_period < 5 or sl_atr_mult < 1.0 etc.

Usage:
    from agi_safety_validator import AGISafetyValidator, apply_ocam_razor
    validated = AGISafetyValidator(**llm_json)
    score = apply_ocam_razor(raw_score, num_params)
"""

import json
import logging
from typing import Optional

log = logging.getLogger("agi_safety")

# ═══════════════════════════════════════════════════════════════════
# Safety Pillar 1: Occam's Razor (Complexity Penalty)
# ═══════════════════════════════════════════════════════════════════

def apply_ocam_razor(raw_score: float, num_params: int) -> float:
    """Apply Occam's Razor penalty to fitness score.

    Formula: adjusted_score = raw_score * (1.0 / (1.0 + 0.1 * num_params))

    A 3-param strategy with score 0.8 beats a 10-param with score 0.85:
      - 3 params: 0.8 * (1 / 1.3) = 0.615
      - 10 params: 0.85 * (1 / 2.0) = 0.425

    Args:
        raw_score: The raw fitness score (e.g., profit factor, Sharpe ratio).
        num_params: Number of tunable parameters in the strategy.

    Returns:
        Adjusted score penalized for complexity.
    """
    if num_params < 0:
        num_params = 0
    return raw_score * (1.0 / (1.0 + 0.1 * num_params))


# ═══════════════════════════════════════════════════════════════════
# Safety Pillar 3: Brutal Reality (Embedded Costs)
# ═══════════════════════════════════════════════════════════════════

# B3 standard cost model
B3_COSTS = {
    "WIN": {"brokerage": 2.5, "exchange_fee": 0.45, "slippage_per_tick": 1.0, "tick": 5},
    "WDO": {"brokerage": 2.5, "exchange_fee": 0.70, "slippage_per_tick": 5.0, "tick": 0.5},
    "BIT": {"brokerage": 2.5, "exchange_fee": 0.30, "slippage_per_tick": 10.0, "tick": 0.01},
    "WSP": {"brokerage": 2.5, "exchange_fee": 0.40, "slippage_per_tick": 2.5, "tick": 0.01},
}


def compute_total_cost(
    symbol: str,
    slippage_ticks: int = 1,
    cost_model: str = "b3_standard",
) -> float:
    """Compute total round-trip cost per trade including B3 fees + slippage.

    Args:
        symbol: Root symbol (WIN, WDO, BIT, WSP).
        slippage_ticks: Number of ticks of slippage per side.
        cost_model: Cost model name (currently only 'b3_standard').

    Returns:
        Total cost in R$ per round-trip trade.
    """
    sym = symbol.upper()[:3]
    specs = B3_COSTS.get(sym, B3_COSTS["WIN"])

    brokerage = specs["brokerage"] * 2  # entry + exit
    exchange = specs["exchange_fee"] * 2
    slippage = specs["slippage_per_tick"] * slippage_ticks * 2  # both sides

    return round(brokerage + exchange + slippage, 2)


def is_trade_profitable_after_costs(
    net_pnl: float,
    total_cost: float,
    min_profit_multiple: float = 2.0,
) -> bool:
    """Check if a trade is profitable after embedded costs.

    Brutal Reality rule: Net Profit must be > Total_Cost * min_profit_multiple.
    This ensures profit pays B3 fees + brokerage + slippage AND has leftover.

    Args:
        net_pnl: Net profit/loss of the trade in R$.
        total_cost: Total round-trip cost in R$.
        min_profit_multiple: Minimum profit multiple over costs (default 2.0).

    Returns:
        True if the trade passes the profitability test.
    """
    return net_pnl > (total_cost * min_profit_multiple)


def filter_trades_by_costs(
    trades: list[dict],
    symbol: str,
    slippage_ticks: int = 1,
) -> dict:
    """Filter trades by the Brutal Reality cost gate.

    Args:
        trades: List of trade dicts with 'net_pnl' key.
        symbol: Root symbol for cost calculation.
        slippage_ticks: Slippage ticks per side.

    Returns:
        dict with:
        - profitable_after_costs: trades that pass the gate
        - destroyed_by_costs: trades that fail (profit < 2x costs)
        - total_cost: cost per trade
        - survivors: count of profitable trades
        - killed: count of destroyed trades
    """
    total_cost = compute_total_cost(symbol, slippage_ticks)
    profitable = []
    destroyed = []

    for t in trades:
        pnl = t.get("net_pnl", 0)
        if is_trade_profitable_after_costs(pnl, total_cost):
            profitable.append(t)
        else:
            destroyed.append(t)

    return {
        "profitable_after_costs": profitable,
        "destroyed_by_costs": destroyed,
        "total_cost": total_cost,
        "survivors": len(profitable),
        "killed": len(destroyed),
    }


# ═══════════════════════════════════════════════════════════════════
# LLM Output Validation (Pydantic-like schema)
# ═══════════════════════════════════════════════════════════════════

# Hard bounds for parameters that LLM CANNOT violate
PARAM_HARD_BOUNDS = {
    "rsi_period": (5, 50),
    "rsi_overbought": (60, 90),
    "rsi_oversold": (10, 40),
    "sl_atr_mult": (0.8, 5.0),  # was (0.3, 5.0) — 0.3 results in noise-level stops
    "trail_activate": (0.3, 5.0),
    "trail_distance": (0.1, 3.0),
    "cooldown_seconds": (60, 7200),
    "max_daily_trades": (1, 50),
    "bb_std": (1.0, 4.0),
    "bb_period": (5, 100),
    "adx_threshold": (5, 50),
    "adx_period": (5, 50),
    "vwap_period": (5, 100),
    "vwap_buy_threshold": (1.0, 1.1),
    "vwap_sell_threshold": (0.9, 1.0),
    "ema_fast": (3, 20),
    "ema_slow": (10, 50),
    "macd_fast": (3, 20),
    "macd_slow": (10, 50),
    "macd_signal": (3, 20),
    "breakeven_minutes": (1, 120),
    "time_trail_minutes": (5, 240),
    "max_position_minutes": (10, 480),
    "pullback_pct": (0.01, 0.50),
    "min_atr_for_entry": (0.0, 1000.0),  # Patience Filter
    "hard_exit_minutes": (15, 480),
}


class AGISafetyValidator:
    """Validates LLM-generated JSON against safety bounds.

    This is the rigid schema that LLM output must pass before application.
    LLM CANNOT suggest values outside the hard bounds.

    Usage:
        validator = AGISafetyValidator(llm_json_dict)
        is_valid = validator.validate()
        errors = validator.errors
        sanitized = validator.get_sanitized()
    """

    def __init__(self, data: dict):
        """Initialize with LLM output dict.

        Args:
            data: Dict from LLM JSON (must have 'changes' array).
        """
        self.data = data
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def validate(self) -> bool:
        """Validate the entire LLM output.

        Returns:
            True if valid (may have warnings), False if critical errors.
        """
        self.errors = []
        self.warnings = []

        if not isinstance(self.data, dict):
            self.errors.append("Output is not a dict")
            return False

        changes = self.data.get("changes", [])
        if not isinstance(changes, list):
            self.errors.append("'changes' must be an array")
            return False

        for i, change in enumerate(changes):
            self._validate_change(i, change)

        return len(self.errors) == 0

    def _validate_change(self, idx: int, change: dict):
        """Validate a single change entry."""
        if not isinstance(change, dict):
            self.errors.append(f"changes[{idx}]: not a dict")
            return

        symbol = change.get("symbol", "")
        if not symbol:
            self.errors.append(f"changes[{idx}]: missing 'symbol'")
            return

        params = change.get("params", {})
        if not isinstance(params, dict):
            self.errors.append(f"changes[{idx}].params: not a dict")
            return

        for key, value in params.items():
            self._validate_param(idx, symbol, key, value)

    def _validate_param(self, idx: int, symbol: str, key: str, value):
        """Validate a single parameter value against hard bounds."""
        # Strategy key is always a string
        if key == "strategy":
            if not isinstance(value, str):
                self.errors.append(
                    f"changes[{idx}].{symbol}.{key}: must be string, got {type(value).__name__}"
                )
            return

        # Numeric params
        if not isinstance(value, (int, float)):
            self.errors.append(
                f"changes[{idx}].{symbol}.{key}: must be numeric, got {type(value).__name__}"
            )
            return

        # Check hard bounds
        if key in PARAM_HARD_BOUNDS:
            lo, hi = PARAM_HARD_BOUNDS[key]
            if value < lo or value > hi:
                self.errors.append(
                    f"changes[{idx}].{symbol}.{key}: {value} outside hard bounds [{lo}, {hi}]"
                )

    def get_sanitized(self) -> dict:
        """Return sanitized version of the data with bounds clamped.

        Returns:
            Copy of the data with all numeric params clamped to bounds.
        """
        import copy
        sanitized = copy.deepcopy(self.data)

        for change in sanitized.get("changes", []):
            params = change.get("params", {})
            for key, value in list(params.items()):
                if key == "strategy" or not isinstance(value, (int, float)):
                    continue
                if key in PARAM_HARD_BOUNDS:
                    lo, hi = PARAM_HARD_BOUNDS[key]
                    if value < lo:
                        params[key] = lo
                        self.warnings.append(
                            f"{change.get('symbol')}.{key}: clamped {value} → {lo}"
                        )
                    elif value > hi:
                        params[key] = hi
                        self.warnings.append(
                            f"{change.get('symbol')}.{key}: clamped {value} → {hi}"
                        )

        return sanitized

    def count_params(self, symbol: str) -> int:
        """Count how many params are being changed for a symbol.

        Used for Occam's Razor penalty calculation.

        Args:
            symbol: Symbol to count params for.

        Returns:
            Number of params in the change for this symbol.
        """
        for change in self.data.get("changes", []):
            if change.get("symbol", "").upper() == symbol.upper():
                return len(change.get("params", {}))
        return 0


# ═══════════════════════════════════════════════════════════════════
# Safety Pillar 2: Cost of Not Trading (Patience Filter)
# ═══════════════════════════════════════════════════════════════════

def evaluate_patience_filter(trades: list[dict], config: dict) -> dict:
    """Evaluate if the patience filter (min_atr_for_entry) is properly tuned.

    The biggest edge in day trading is NOT trading on noise days.
    This function analyzes whether raising the ATR filter would have
    prevented losing trades while preserving winning ones.

    Args:
        trades: List of trade dicts with signal_detail containing ATR.
        config: Current config with min_atr_for_entry values.

    Returns:
        dict with:
        - current_filter: current min_atr_for_entry value
        - optimal_filter: suggested optimal value
        - trades_filtered: number of trades that would be filtered
        - losses_avoided: losses that would be avoided
        - wins_lost: winning trades that would also be lost
    """
    current_filter = config.get("min_atr_for_entry", 0)

    # Extract ATR from signal_detail
    atr_trades = []
    for t in trades:
        signal = t.get("signal_detail")
        if isinstance(signal, str):
            try:
                signal = json.loads(signal)
            except (json.JSONDecodeError, TypeError):
                continue
        if isinstance(signal, dict) and signal.get("atr"):
            atr_trades.append({
                "atr": float(signal["atr"]),
                "net_pnl": t.get("net_pnl", 0),
            })

    if not atr_trades:
        return {
            "current_filter": current_filter,
            "optimal_filter": current_filter,
            "trades_filtered": 0,
            "losses_avoided": 0,
            "wins_lost": 0,
        }

    # Try different ATR thresholds
    atr_values = sorted(set(t["atr"] for t in atr_trades))
    best_threshold = current_filter
    best_score = 0  # losses_avoided - wins_lost

    for threshold in atr_values:
        losses_avoided = sum(
            1 for t in atr_trades
            if t["atr"] < threshold and t["net_pnl"] < 0
        )
        wins_lost = sum(
            1 for t in atr_trades
            if t["atr"] < threshold and t["net_pnl"] > 0
        )
        score = losses_avoided - wins_lost
        if score > best_score:
            best_score = score
            best_threshold = threshold

    # Count impact of optimal filter
    trades_filtered = sum(1 for t in atr_trades if t["atr"] < best_threshold)
    losses_avoided = sum(
        1 for t in atr_trades
        if t["atr"] < best_threshold and t["net_pnl"] < 0
    )
    wins_lost = sum(
        1 for t in atr_trades
        if t["atr"] < best_threshold and t["net_pnl"] > 0
    )

    return {
        "current_filter": current_filter,
        "optimal_filter": round(best_threshold, 2),
        "trades_filtered": trades_filtered,
        "losses_avoided": losses_avoided,
        "wins_lost": wins_lost,
        "net_benefit": losses_avoided - wins_lost,
    }


# ═══════════════════════════════════════════════════════════════════
# SL Quality Metrics & Dynamic Adjustment
# ═══════════════════════════════════════════════════════════════════

def apply_sl_hit_rate_penalty(raw_score: float, sl_hit_rate: float) -> float:
    """Apply fitness penalty for strategies with high SL hit rates.

    If >70% of exits are SL_SERVIDOR, the strategy's fitness score is
    reduced by 30%. This penalizes strategies where stops are too tight
    and get hit by normal market noise before the trade develops.

    Args:
        raw_score: The raw fitness score (e.g., profit factor, Sharpe ratio).
        sl_hit_rate: SL hit rate as percentage (0-100).

    Returns:
        Adjusted score penalized for excessive SL hits.
    """
    if sl_hit_rate > 70.0:
        return raw_score * 0.70  # 30% penalty
    elif sl_hit_rate > 50.0:
        return raw_score * 0.85  # 15% penalty
    return raw_score


def compute_dynamic_sl_mult(
    base_sl_mult: float,
    current_atr: float,
    atr_20d_avg: float,
) -> float:
    """Compute adaptive SL multiplier based on current volatility vs average.

    - If ATR > 1.5x 20-day average: SL = base_sl_mult × 1.2 (wider, high vol)
    - If ATR < 0.7x 20-day average: SL = base_sl_mult × 0.8 (tighter, low vol)
    - Otherwise: SL = base_sl_mult (normal)

    Args:
        base_sl_mult: The base sl_atr_mult parameter.
        current_atr: Current ATR value.
        atr_20d_avg: 20-day average ATR.

    Returns:
        Adjusted SL multiplier.
    """
    if atr_20d_avg <= 0:
        return base_sl_mult

    atr_ratio = current_atr / atr_20d_avg

    if atr_ratio > 1.5:
        # High volatility — widen SL
        return base_sl_mult * 1.2
    elif atr_ratio < 0.7:
        # Low volatility — tighten SL (but floor is enforced by min_native)
        return base_sl_mult * 0.8
    return base_sl_mult


def compute_atr_based_min_sl(
    fixed_min_native: int,
    atr: float,
    atr_floor_pct: float = 0.8,
) -> int:
    """Compute ATR-based minimum SL to prevent noise-level stops.

    Instead of using a fixed min_native (e.g., WDO=3 pts = R$1.50),
    derive the minimum from ATR so the SL always has room to breathe.

    Formula: min_from_atr = max(fixed_min_native, int(atr * atr_floor_pct))

    Args:
        fixed_min_native: The instrument's fixed minimum SL in native points.
        atr: Current ATR value.
        atr_floor_pct: Minimum SL as fraction of ATR (default 0.8 = 80%).

    Returns:
        Minimum SL in native points (never below fixed_min_native).
    """
    min_from_atr = int(atr * atr_floor_pct)
    return max(fixed_min_native, min_from_atr)
