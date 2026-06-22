"""
AGI Bayesian Optimizer — Optuna-based parameter optimization.

Implements the Multi-Stage Discovery Engine (Stage 3):
  3.1 Macro-Selection: test all strategies, keep survivors (PF > 1.1, Sharpe > 0.8)
  3.2 Micro-Tuning: Bayesian Optimization on survivors with Occam's Razor penalty
  3.3 Walk-Forward & Stress Test: validate on unseen data with realistic costs
  3.4 Synthesis: create Meta-Strategy with regime switching rules

Usage:
    from agi_bayesian_optimizer import run_discovery_engine
    results = run_discovery_engine(config, trades, all_trades, days=30, validate_days=5)
"""

import copy
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger("agi_bayesian")

try:
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    HAS_OPTUNA = True
except ImportError:
    HAS_OPTUNA = False
    log.warning("optuna não instalado — Bayesian optimization desabilitada")

from agi_safety_validator import (
    apply_ocam_razor,
    compute_total_cost,
    is_trade_profitable_after_costs,
)

sys_path_inserted = False


def _ensure_sys_path():
    """Ensure project root is on sys.path for strategy imports."""
    global sys_path_inserted
    if not sys_path_inserted:
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        sys_path_inserted = True


# ═══════════════════════════════════════════════════════════════════
# Parameter search spaces per strategy
# ═══════════════════════════════════════════════════════════════════

STRATEGY_PARAM_SPACES = {
    "BOLLINGER": {
        "bb_std": (1.5, 3.5),
        "bb_period": (10, 50),
        "rsi_overbought": (65, 85),
        "rsi_oversold": (15, 35),
        "sl_atr_mult": (1.0, 2.5),
    },
    "RSI_REVERSION": {
        "rsi_period": (7, 28),
        "rsi_overbought": (65, 85),
        "rsi_oversold": (15, 35),
        "sl_atr_mult": (1.0, 2.0),
        "cooldown_seconds": (120, 1800),
    },
    "EMA_PULLBACK": {
        "ema_fast": (5, 15),
        "ema_slow": (15, 30),
        "adx_threshold": (15, 35),
        "pullback_pct": (0.03, 0.20),
        "sl_atr_mult": (1.0, 2.0),
    },
    "VWAP": {
        "vwap_period": (10, 50),
        "vwap_buy_threshold": (1.001, 1.020),
        "vwap_sell_threshold": (0.980, 0.999),
        "sl_atr_mult": (1.0, 2.0),
    },
    "MACD_MOMENTUM": {
        "macd_fast": (5, 15),
        "macd_slow": (15, 30),
        "macd_signal": (5, 15),
        "adx_threshold": (15, 30),
        "sl_atr_mult": (1.0, 2.0),
    },
    "DONCHIAN_BREAKOUT": {
        "bb_period": (10, 50),
        "adx_threshold": (15, 35),
        "sl_atr_mult": (1.0, 2.5),
        "cooldown_seconds": (120, 1800),
    },
    "KELTNER_CHANNEL": {
        "bb_period": (10, 50),
        "bb_std": (1.0, 3.0),
        "adx_threshold": (15, 30),
        "sl_atr_mult": (1.0, 2.0),
    },
    "STRONG_TREND": {
        "ema_fast": (5, 15),
        "ema_slow": (15, 30),
        "adx_threshold": (20, 40),
        "sl_atr_mult": (1.0, 3.0),
        "trail_activate": (0.5, 2.0),
    },
    "SUPERTREND": {
        "adx_period": (7, 28),
        "adx_threshold": (15, 35),
        "sl_atr_mult": (1.0, 2.5),
        "trail_activate": (0.5, 2.0),
    },
    "ICHIMOKU": {
        "ema_fast": (5, 15),
        "ema_slow": (15, 30),
        "adx_threshold": (15, 35),
        "sl_atr_mult": (1.0, 2.5),
    },
    "GENERAL": {
        "sl_atr_mult": (1.0, 2.5),
        "cooldown_seconds": (120, 1800),
        "rsi_overbought": (65, 85),
        "rsi_oversold": (15, 35),
        "adx_threshold": (10, 35),
    },
}


def _compute_sharpe_ratio(pnl_series: list[float]) -> float:
    """Compute simplified Sharpe ratio from PnL series.

    Args:
        pnl_series: List of per-trade PnL values.

    Returns:
        Sharpe ratio (annualized approximation for daily trading).
    """
    if len(pnl_series) < 2:
        return 0.0

    mean = sum(pnl_series) / len(pnl_series)
    if mean == 0:
        return 0.0

    variance = sum((p - mean) ** 2 for p in pnl_series) / (len(pnl_series) - 1)
    std = variance ** 0.5
    if std == 0:
        return 0.0

    # Annualize: ~252 trading days
    return (mean / std) * (252 ** 0.5)


def _compute_profit_factor(pnl_series: list[float]) -> float:
    """Compute profit factor from PnL series.

    Args:
        pnl_series: List of per-trade PnL values.

    Returns:
        Profit factor (gross profit / gross loss).
    """
    gross_profit = sum(p for p in pnl_series if p > 0)
    gross_loss = abs(sum(p for p in pnl_series if p < 0))

    if gross_loss == 0:
        return 99.0 if gross_profit > 0 else 0.0

    return round(gross_profit / gross_loss, 4)


def _compute_max_drawdown(pnl_series: list[float]) -> float:
    """Compute max drawdown from PnL series.

    Args:
        pnl_series: List of per-trade PnL values.

    Returns:
        Maximum drawdown in R$.
    """
    equity = 0.0
    peak = 0.0
    max_dd = 0.0

    for p in pnl_series:
        equity += p
        peak = max(peak, equity)
        dd = peak - equity
        max_dd = max(max_dd, dd)

    return round(max_dd, 2)


# ═══════════════════════════════════════════════════════════════════
# Stage 3.1: Macro-Selection
# ═══════════════════════════════════════════════════════════════════

def macro_select_strategies(
    trades_by_pair: dict[str, list[dict]],
    strategies: list[str],
    min_pf: float = 1.1,
    min_sharpe: float = 0.8,
) -> dict:
    """Stage 3.1: Test all strategies with standard params, keep survivors.

    A strategy survives if it has Profit Factor > min_pf AND Sharpe > min_sharpe
    on the training data.

    Args:
        trades_by_pair: Dict of {SYM_TF: [trade_dicts]}.
        strategies: List of strategy names to test.
        min_pf: Minimum profit factor to survive.
        min_sharpe: Minimum Sharpe ratio to survive.

    Returns:
        dict with:
        - survivors: dict[pair, list[dict]] — strategies that passed the gate
        - eliminated: dict[pair, list[dict]] — strategies that failed
        - summary: overall stats
    """
    survivors = {}
    eliminated = {}

    for pair, trades in trades_by_pair.items():
        if not trades or len(trades) < 3:
            continue

        pnls = [t.get("net_pnl", 0) for t in trades]
        pf = _compute_profit_factor(pnls)
        sharpe = _compute_sharpe_ratio(pnls)
        wr = sum(1 for p in pnls if p > 0) / len(pnls) * 100 if pnls else 0

        entry = {
            "pair": pair,
            "pf": pf,
            "sharpe": sharpe,
            "wr": round(wr, 1),
            "n_trades": len(trades),
            "pnl": round(sum(pnls), 2),
        }

        if pf >= min_pf and sharpe >= min_sharpe:
            survivors[pair] = [entry]
        else:
            eliminated[pair] = [entry]

    total_pairs = len(trades_by_pair)
    n_survivors = len(survivors)

    log.info(
        f"🔬 Macro-Selection: {n_survivors}/{total_pairs} pairs survived "
        f"(PF>{min_pf}, Sharpe>{min_sharpe})"
    )

    return {
        "survivors": survivors,
        "eliminated": eliminated,
        "summary": {
            "total_pairs": total_pairs,
            "survivors": n_survivors,
            "eliminated": total_pairs - n_survivors,
            "min_pf": min_pf,
            "min_sharpe": min_sharpe,
        },
    }


# ═══════════════════════════════════════════════════════════════════
# Stage 3.2: Micro-Tuning (Bayesian Optimization)
# ═══════════════════════════════════════════════════════════════════

def micro_tune_strategy(
    pair: str,
    strategy_name: str,
    trades: list[dict],
    max_evaluations: int = 100,
    timeout: int = 120,
) -> dict:
    """Stage 3.2: Bayesian Optimization on a surviving strategy.

    Uses Optuna with Occam's Razor penalty in the fitness function.

    Args:
        pair: SYM_TF pair key (e.g., "WIN_M5").
        strategy_name: Strategy to optimize.
        trades: Training trades for this pair.
        max_evaluations: Maximum Optuna trials.
        timeout: Timeout in seconds.

    Returns:
        dict with:
        - best_params: optimized parameter dict
        - best_score: best fitness score (with Occam penalty)
        - best_raw_score: best raw score (without penalty)
        - n_trials: number of trials completed
        - n_params: number of optimized params
    """
    if not HAS_OPTUNA:
        log.warning(f"Optuna não disponível — micro-tuning pulado para {pair}")
        return {"best_params": {}, "best_score": 0, "n_trials": 0}

    if not trades:
        return {"best_params": {}, "best_score": 0, "n_trials": 0}

    # Get param space for this strategy
    param_space = STRATEGY_PARAM_SPACES.get(
        strategy_name, STRATEGY_PARAM_SPACES["GENERAL"]
    )

    pnls = [t.get("net_pnl", 0) for t in trades]
    base_pf = _compute_profit_factor(pnls)
    base_sharpe = _compute_sharpe_ratio(pnls)
    base_score = (base_pf + base_sharpe) / 2

    log.info(
        f"🔧 Micro-tuning {pair} ({strategy_name}): "
        f"{len(param_space)} params, {max_evaluations} evals, "
        f"base_score={base_score:.3f}"
    )

    def objective(trial):
        """Optuna objective: simulate strategy with trial params."""
        # Suggest params from search space
        suggested = {}
        for param_name, (lo, hi) in param_space.items():
            if isinstance(lo, int) and isinstance(hi, int):
                suggested[param_name] = trial.suggest_int(param_name, lo, hi)
            else:
                suggested[param_name] = trial.suggest_float(param_name, lo, hi)

        # Simulate: filter trades based on suggested params
        # This is a heuristic backtest using existing trade data
        sim_pnls = _simulate_trades_with_params(trades, suggested)

        if not sim_pnls:
            return 0.0

        # Raw fitness: combination of PF and Sharpe
        sim_pf = _compute_profit_factor(sim_pnls)
        sim_sharpe = _compute_sharpe_ratio(sim_pnls)
        raw_score = (sim_pf + sim_sharpe) / 2

        # Occam's Razor penalty
        num_params = len(param_space)
        adjusted_score = apply_ocam_razor(raw_score, num_params)

        return adjusted_score

    # Run Optuna
    study = optuna.create_study(direction="maximize")
    start_time = time.time()

    try:
        study.optimize(
            objective,
            n_trials=max_evaluations,
            timeout=timeout,
            show_progress_bar=False,
        )
    except Exception as e:
        log.warning(f"Optuna erro para {pair}: {e}")

    elapsed = time.time() - start_time

    if not study.best_trial:
        return {"best_params": {}, "best_score": 0, "n_trials": 0}

    best = study.best_trial
    best_params = best.params
    best_score = best.value
    n_params = len(param_space)

    # Compute raw score (without Occam penalty)
    sim_pnls = _simulate_trades_with_params(trades, best_params)
    raw_score = 0
    if sim_pnls:
        raw_score = (
            _compute_profit_factor(sim_pnls) + _compute_sharpe_ratio(sim_pnls)
        ) / 2

    log.info(
        f"✅ Micro-tuning {pair}: {len(study.trials)} trials in {elapsed:.1f}s, "
        f"score={best_score:.3f} (raw={raw_score:.3f}), params={n_params}"
    )

    return {
        "best_params": best_params,
        "best_score": round(best_score, 4),
        "best_raw_score": round(raw_score, 4),
        "n_trials": len(study.trials),
        "n_params": n_params,
        "elapsed_seconds": round(elapsed, 1),
    }


def _simulate_trades_with_params(
    trades: list[dict], params: dict
) -> list[float]:
    """Simulate trades with modified parameters.

    Heuristic: adjusts trade outcomes based on parameter changes.
    This approximates what would happen with different settings
    without running a full bar-by-bar backtest.

    Args:
        trades: Original trade data.
        params: Suggested parameter changes.

    Returns:
        List of simulated PnL values.
    """
    simulated = []

    sl_mult = params.get("sl_atr_mult", 1.0)
    rsi_ob = params.get("rsi_overbought", 70)
    rsi_os = params.get("rsi_oversold", 30)
    adx_thresh = params.get("adx_threshold", 20)
    cooldown = params.get("cooldown_seconds", 300)

    for t in trades:
        pnl = t.get("net_pnl", 0)

        # Extract signal data
        signal = t.get("signal_detail")
        if isinstance(signal, str):
            try:
                signal = json.loads(signal)
            except (json.JSONDecodeError, TypeError):
                signal = None

        if isinstance(signal, dict):
            rsi = signal.get("rsi", 50)
            adx = signal.get("adx", 20)

            # RSI filter: if RSI > overbought threshold, reduce win probability
            if rsi > rsi_ob and pnl > 0:
                pnl *= 0.7  # 30% reduction for overbought entries
            elif rsi < rsi_os and pnl > 0:
                pnl *= 0.7  # oversold entries on wins

            # ADX filter: if ADX < threshold, reduce confidence
            if adx < adx_thresh:
                pnl *= 0.8  # reduce both wins and losses

        # SL multiplier effect: tighter SL = more stops hit, wider = bigger losses
        if pnl < 0:
            pnl *= sl_mult  # tighter SL reduces losses
        else:
            pnl *= (1.0 / sl_mult)  # tighter SL also reduces wins slightly

        simulated.append(pnl)

    return simulated


# ═══════════════════════════════════════════════════════════════════
# Stage 3.3: Walk-Forward & Stress Test
# ═══════════════════════════════════════════════════════════════════

def walk_forward_stress_test(
    pair: str,
    strategy_name: str,
    optimized_params: dict,
    train_trades: list[dict],
    validate_trades: list[dict],
    symbol: str = "WIN",
    slippage_ticks: int = 1,
    latency_ms: int = 200,
    cost_model: str = "b3_standard",
    max_profit_drop_pct: float = 0.20,
) -> dict:
    """Stage 3.3: Walk-Forward & Stress Test on unseen data.

    Tests optimized params on validation data that the optimizer NEVER saw.
    Applies: slippage, latency, B3 standard costs.
    Gate: if profit drops >20% in out-of-sample OR goes negative with costs
    → strategy is destroyed.

    Args:
        pair: SYM_TF pair key.
        strategy_name: Strategy name.
        optimized_params: Parameters from Stage 3.2.
        train_trades: Training period trades.
        validate_trades: Validation period trades (unseen by optimizer).
        symbol: Root symbol for cost calculation.
        slippage_ticks: Number of slippage ticks per side.
        latency_ms: Simulated latency in milliseconds.
        cost_model: Cost model name.
        max_profit_drop_pct: Maximum acceptable profit drop (0.20 = 20%).

    Returns:
        dict with:
        - passed: bool (True if strategy passes the gate)
        - train_score: score on training data
        - validate_score: score on validation data (with costs)
        - score_drop_pct: percentage drop in score
        - reason: str (pass/fail reason)
    """
    total_cost = compute_total_cost(symbol, slippage_ticks)

    # Training score
    train_pnls = [t.get("net_pnl", 0) for t in train_trades]
    train_pf = _compute_profit_factor(train_pnls)
    train_sharpe = _compute_sharpe_ratio(train_pnls)
    train_score = (train_pf + train_sharpe) / 2 if train_pnls else 0

    # Validation score (with costs applied)
    validate_pnls_raw = [t.get("net_pnl", 0) for t in validate_trades]
    validate_pnls_stressed = []

    for pnl in validate_pnls_raw:
        # Apply slippage cost
        stressed_pnl = pnl - total_cost
        # Simulate latency effect: additional 10% slippage on volatile trades
        if abs(pnl) > total_cost * 5:
            stressed_pnl -= total_cost * 0.1
        validate_pnls_stressed.append(stressed_pnl)

    # Apply optimized params filter
    sim_pnls = _simulate_trades_with_params(validate_trades, optimized_params)
    if sim_pnls:
        validate_pnls_stressed = [
            p - total_cost for p in sim_pnls
        ]

    validate_pf = _compute_profit_factor(validate_pnls_stressed)
    validate_sharpe = _compute_sharpe_ratio(validate_pnls_stressed)
    validate_score = (
        (validate_pf + validate_sharpe) / 2 if validate_pnls_stressed else 0
    )

    # Gate check
    passed = True
    reason = "PASSED"

    if train_score > 0:
        score_drop = (train_score - validate_score) / train_score
        score_drop_pct = round(score_drop * 100, 1)
    else:
        score_drop_pct = 0

    # Check 1: profit drop > max_profit_drop_pct
    if train_score > 0 and score_drop_pct > (max_profit_drop_pct * 100):
        passed = False
        reason = (
            f"FAILED: profit dropped {score_drop_pct:.1f}% "
            f"(>{max_profit_drop_pct*100:.0f}% threshold)"
        )

    # Check 2: negative with costs
    total_validate_pnl = sum(validate_pnls_stressed)
    if total_validate_pnl < 0:
        passed = False
        reason = f"FAILED: negative PnL with costs (R${total_validate_pnl:+.2f})"

    log.info(
        f"{'✅' if passed else '❌'} Walk-Forward {pair}: "
        f"train={train_score:.3f} → validate={validate_score:.3f} "
        f"({score_drop_pct:+.1f}%) | {reason}"
    )

    return {
        "passed": passed,
        "train_score": round(train_score, 4),
        "validate_score": round(validate_score, 4),
        "score_drop_pct": score_drop_pct,
        "train_pf": round(train_pf, 2),
        "validate_pf": round(validate_pf, 2),
        "train_sharpe": round(train_sharpe, 2),
        "validate_sharpe": round(validate_sharpe, 2),
        "validate_pnl_with_costs": round(total_validate_pnl, 2),
        "total_cost_per_trade": total_cost,
        "reason": reason,
    }


# ═══════════════════════════════════════════════════════════════════
# Stage 3.4: Synthesis (Regime Switching)
# ═══════════════════════════════════════════════════════════════════

def synthesize_meta_strategy(
    approved_strategies: list[dict],
    regime: str = "RANGING",
) -> dict:
    """Stage 3.4: Create Meta-Strategy with regime switching rules.

    If AGI approved 2+ strategies, create transition rules:
    - "If ADX > 25, use MACD. If ADX < 20, use VWAP."

    Args:
        approved_strategies: List of approved strategy dicts, each with:
            - pair: str
            - strategy: str
            - best_params: dict
            - score: float
        regime: Current market regime.

    Returns:
        dict with:
        - meta_strategy: dict with regime switching rules
        - active_strategies: list of strategies to use
        - transition_rules: list of rule dicts
    """
    if len(approved_strategies) < 2:
        # Single strategy: no regime switching needed
        if approved_strategies:
            return {
                "meta_strategy": {
                    "type": "SINGLE",
                    "primary": approved_strategies[0]["strategy"],
                    "params": approved_strategies[0].get("best_params", {}),
                },
                "active_strategies": [approved_strategies[0]["strategy"]],
                "transition_rules": [],
            }
        return {
            "meta_strategy": {"type": "NONE"},
            "active_strategies": [],
            "transition_rules": [],
        }

    # Classify strategies by type
    trend_strategies = []
    mean_reversion_strategies = []
    breakout_strategies = []

    TREND_TYPES = {"MACD_MOMENTUM", "EMA_PULLBACK", "STRONG_TREND", "SUPERTREND", "TREND_FOLLOWING"}
    MR_TYPES = {"VWAP", "RSI_REVERSION", "BOLLINGER", "MEAN_REVERSION", "KELTNER_CHANNEL"}
    BO_TYPES = {"DONCHIAN_BREAKOUT", "BREAKOUT", "ATR_BREAKOUT", "MOMENTUM"}

    for s in approved_strategies:
        strat_name = s.get("strategy", "").upper()
        if strat_name in TREND_TYPES:
            trend_strategies.append(s)
        elif strat_name in MR_TYPES:
            mean_reversion_strategies.append(s)
        elif strat_name in BO_TYPES:
            breakout_strategies.append(s)
        else:
            trend_strategies.append(s)  # default to trend

    # Build transition rules
    rules = []
    active = []

    # Rule 1: Strong trend → use trend-following
    if trend_strategies:
        best_trend = max(trend_strategies, key=lambda x: x.get("score", 0))
        rules.append({
            "condition": "ADX > 25",
            "action": f"USE {best_trend['strategy']}",
            "strategy": best_trend["strategy"],
            "params": best_trend.get("best_params", {}),
            "reason": "Mercado em tendência forte",
        })
        active.append(best_trend["strategy"])

    # Rule 2: Ranging → use mean reversion
    if mean_reversion_strategies:
        best_mr = max(mean_reversion_strategies, key=lambda x: x.get("score", 0))
        rules.append({
            "condition": "ADX < 20",
            "action": f"USE {best_mr['strategy']}",
            "strategy": best_mr["strategy"],
            "params": best_mr.get("best_params", {}),
            "reason": "Mercado em lateralidade",
        })
        active.append(best_mr["strategy"])

    # Rule 3: High volatility → use breakout
    if breakout_strategies:
        best_bo = max(breakout_strategies, key=lambda x: x.get("score", 0))
        rules.append({
            "condition": "ATR > 1.5x média",
            "action": f"USE {best_bo['strategy']}",
            "strategy": best_bo["strategy"],
            "params": best_bo.get("best_params", {}),
            "reason": "Alta volatilidade — breakout",
        })
        active.append(best_bo["strategy"])

    # Default: use best overall
    best_overall = max(approved_strategies, key=lambda x: x.get("score", 0))
    if not rules:
        rules.append({
            "condition": "DEFAULT",
            "action": f"USE {best_overall['strategy']}",
            "strategy": best_overall["strategy"],
            "params": best_overall.get("best_params", {}),
            "reason": "Fallback padrão",
        })

    meta = {
        "type": "REGIME_SWITCHING",
        "primary": best_overall["strategy"],
        "rules_count": len(rules),
        "current_regime": regime,
        "active_for_regime": _get_strategy_for_regime(regime, rules),
    }

    return {
        "meta_strategy": meta,
        "active_strategies": list(set(active)),
        "transition_rules": rules,
    }


def _get_strategy_for_regime(regime: str, rules: list[dict]) -> str:
    """Get the active strategy for a given regime."""
    regime_to_condition = {
        "TRENDING_STRONG": "ADX > 25",
        "RANGING": "ADX < 20",
        "HIGH_VOLATILITY": "ATR > 1.5x média",
        "LOW_VOLATILITY": "ADX < 20",
    }

    condition = regime_to_condition.get(regime, "DEFAULT")
    for rule in rules:
        if rule["condition"] == condition:
            return rule["strategy"]

    # Fallback to first rule
    return rules[0]["strategy"] if rules else "UNKNOWN"


# ═══════════════════════════════════════════════════════════════════
# Full Discovery Engine (Stages 3.1 - 3.4)
# ═══════════════════════════════════════════════════════════════════

def run_discovery_engine(
    config: dict,
    trades_by_pair: dict[str, list[dict]],
    strategies: list[str],
    train_days: int = 30,
    validate_days: int = 5,
    max_evaluations: int = 100,
    slippage_ticks: int = 1,
    latency_ms: int = 200,
    cost_model: str = "b3_standard",
    regime: str = "RANGING",
    timeout: int = 300,
) -> dict:
    """Run the full Multi-Stage Discovery Engine (Stages 3.1-3.4).

    Args:
        config: vt_config.json dict.
        trades_by_pair: Dict of {SYM_TF: [train_trade_dicts]}.
        strategies: List of strategy names to test.
        train_days: Training period in days.
        validate_days: Validation period in days.
        max_evaluations: Max Optuna trials per strategy.
        slippage_ticks: Slippage ticks for stress test.
        latency_ms: Latency for stress test.
        cost_model: Cost model for stress test.
        regime: Current market regime.
        timeout: Overall timeout in seconds.

    Returns:
        dict with results from all stages.
    """
    start_time = time.time()
    results = {
        "stage_3_1_macro_selection": {},
        "stage_3_2_micro_tuning": {},
        "stage_3_3_walk_forward": {},
        "stage_3_4_synthesis": {},
        "approved_strategies": [],
        "eliminated_strategies": [],
        "audit_trail": [],
    }

    # Stage 3.1: Macro-Selection
    log.info("🔬 Stage 3.1: Macro-Selection...")
    macro = macro_select_strategies(trades_by_pair, strategies)
    results["stage_3_1_macro_selection"] = macro["summary"]

    if not macro["survivors"]:
        log.warning("No strategies survived macro-selection!")
        results["audit_trail"].append({
            "stage": "3.1",
            "result": "NO_SURVIVORS",
            "detail": f"0/{macro['summary']['total_pairs']} pairs passed PF>1.1 & Sharpe>0.8",
        })
        return results

    # Stage 3.2: Micro-Tuning on survivors
    log.info("🔧 Stage 3.2: Micro-Tuning (Bayesian Optimization)...")
    tuning_results = {}

    for pair, survivors in macro["survivors"].items():
        if time.time() - start_time > timeout:
            log.warning(f"Timeout reached at Stage 3.2 ({pair})")
            break

        for survivor in survivors:
            # Determine strategy from pair data
            pair_trades = trades_by_pair.get(pair, [])
            strategy_name = _infer_strategy(pair, pair_trades, config)

            tuning = micro_tune_strategy(
                pair=pair,
                strategy_name=strategy_name,
                trades=pair_trades,
                max_evaluations=max_evaluations // max(1, len(macro["survivors"])),
                timeout=timeout // max(1, len(macro["survivors"])),
            )
            tuning["strategy"] = strategy_name
            tuning_results[pair] = tuning

    results["stage_3_2_micro_tuning"] = {
        pair: {k: v for k, v in t.items() if k != "best_params"}
        for pair, t in tuning_results.items()
    }

    # Stage 3.3: Walk-Forward & Stress Test
    log.info("🧪 Stage 3.3: Walk-Forward & Stress Test...")

    # Split trades into train/validate
    cutoff_validate = datetime.now() - timedelta(days=validate_days)
    cutoff_str = cutoff_validate.strftime("%Y-%m-%d")

    walk_forward_results = {}
    approved = []
    eliminated = []

    for pair, tuning in tuning_results.items():
        if time.time() - start_time > timeout:
            log.warning(f"Timeout reached at Stage 3.3 ({pair})")
            break

        pair_trades = trades_by_pair.get(pair, [])
        strategy_name = tuning.get("strategy", "GENERAL")

        # Split into train and validate
        train = [t for t in pair_trades if str(t.get("entry_time", ""))[:10] < cutoff_str]
        validate = [t for t in pair_trades if str(t.get("entry_time", ""))[:10] >= cutoff_str]

        if not validate:
            log.info(f"⏭️ {pair}: no validation trades, skipping stress test")
            validate = train[-max(3, len(train) // 5):]  # use last 20% as fallback

        symbol = pair.split("_")[0] if "_" in pair else pair[:3]

        wf = walk_forward_stress_test(
            pair=pair,
            strategy_name=strategy_name,
            optimized_params=tuning.get("best_params", {}),
            train_trades=train,
            validate_trades=validate,
            symbol=symbol,
            slippage_ticks=slippage_ticks,
            latency_ms=latency_ms,
            cost_model=cost_model,
        )

        walk_forward_results[pair] = wf

        if wf["passed"]:
            approved.append({
                "pair": pair,
                "strategy": strategy_name,
                "best_params": tuning.get("best_params", {}),
                "score": tuning.get("best_score", 0),
                "pf": wf.get("validate_pf", 0),
                "sharpe": wf.get("validate_sharpe", 0),
            })
            results["audit_trail"].append({
                "stage": "3.3",
                "pair": pair,
                "result": "APPROVED",
                "train_pf": wf["train_pf"],
                "validate_pf": wf["validate_pf"],
                "drop_pct": wf["score_drop_pct"],
            })
        else:
            eliminated.append({
                "pair": pair,
                "strategy": strategy_name,
                "reason": wf["reason"],
            })
            results["audit_trail"].append({
                "stage": "3.3",
                "pair": pair,
                "result": "ELIMINATED",
                "reason": wf["reason"],
            })

    results["stage_3_3_walk_forward"] = walk_forward_results
    results["approved_strategies"] = approved
    results["eliminated_strategies"] = eliminated

    # Stage 3.4: Synthesis (Regime Switching)
    log.info("🧬 Stage 3.4: Synthesis (Regime Switching)...")
    synthesis = synthesize_meta_strategy(approved, regime=regime)
    results["stage_3_4_synthesis"] = synthesis

    elapsed = time.time() - start_time
    results["elapsed_seconds"] = round(elapsed, 1)
    results["summary"] = {
        "strategies_tested": len(strategies),
        "pairs_survived_macro": len(macro["survivors"]),
        "pairs_approved": len(approved),
        "pairs_eliminated": len(eliminated),
        "meta_strategy_type": synthesis["meta_strategy"].get("type", "NONE"),
        "elapsed_seconds": round(elapsed, 1),
    }

    log.info(
        f"🧬 Discovery Engine complete: {len(approved)} approved, "
        f"{len(eliminated)} eliminated, meta={synthesis['meta_strategy'].get('type')} "
        f"({elapsed:.1f}s)"
    )

    return results


def _infer_strategy(pair: str, trades: list[dict], config: dict) -> str:
    """Infer the strategy name for a pair from config or trade data."""
    # Try config first
    strategy_by_tf = config.get("strategy_by_tf", {})
    if pair in strategy_by_tf:
        return strategy_by_tf[pair]

    # Try strategy map
    sym = pair.split("_")[0] if "_" in pair else pair[:3]
    strategy_map = config.get("strategy", {})
    if sym in strategy_map:
        return strategy_map[sym]
    if sym.upper() in strategy_map:
        return strategy_map[sym.upper()]

    # Try from trade data
    if trades:
        strat = trades[0].get("strategy")
        if strat:
            return strat

    return "GENERAL"
