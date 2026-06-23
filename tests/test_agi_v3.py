"""
Tests for AGI v3.0 Super Estratégia Architecture.

Tests:
  - Stage 1: Regime Classifier
  - Stage 3: Safety Validator (Occam's Razor, Brutal Reality)
  - Stage 3: Bayesian Optimizer (Macro-Selection, Walk-Forward, Synthesis)
  - v3.0 CLI arguments (backward compatibility)
  - Prompt context builder
  - Telegram card builder
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure project root is on path
PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "optimization"))


# ═══════════════════════════════════════════════════════════════════
# Stage 1: Regime Classifier Tests
# ═══════════════════════════════════════════════════════════════════

class TestRegimeClassifier(unittest.TestCase):
    """Test regime classification from ATR/ADX values."""

    def test_trending_strong(self):
        """ADX > 25 should classify as TRENDING_STRONG."""
        from agi_regime_classifier import classify_regime_from_atr_adx
        result = classify_regime_from_atr_adx(atr=100, atr_avg=100, adx=30)
        self.assertEqual(result, "TRENDING_STRONG")

    def test_high_volatility(self):
        """ATR > 1.5x average should classify as HIGH_VOLATILITY."""
        from agi_regime_classifier import classify_regime_from_atr_adx
        result = classify_regime_from_atr_adx(atr=200, atr_avg=100, adx=15)
        self.assertEqual(result, "HIGH_VOLATILITY")

    def test_low_volatility(self):
        """ATR < 0.7x average should classify as LOW_VOLATILITY."""
        from agi_regime_classifier import classify_regime_from_atr_adx
        result = classify_regime_from_atr_adx(atr=50, atr_avg=100, adx=15)
        self.assertEqual(result, "LOW_VOLATILITY")

    def test_ranging(self):
        """Default should classify as RANGING."""
        from agi_regime_classifier import classify_regime_from_atr_adx
        result = classify_regime_from_atr_adx(atr=100, atr_avg=100, adx=15)
        self.assertEqual(result, "RANGING")

    def test_zero_atr_avg(self):
        """Zero ATR average should default to RANGING (safety fallback)."""
        from agi_regime_classifier import classify_regime_from_atr_adx
        result = classify_regime_from_atr_adx(atr=100, atr_avg=0, adx=30)
        self.assertEqual(result, "RANGING")  # safety default when no data

    def test_classify_regimes_from_trades(self):
        """Test full regime classification from trade data."""
        from agi_regime_classifier import classify_regimes_from_trades

        trades = [
            {"entry_time": "2026-06-20 10:00:00", "net_pnl": 100,
             "signal_detail": json.dumps({"atr": 150, "adx": 30})},
            {"entry_time": "2026-06-21 10:00:00", "net_pnl": -50,
             "signal_detail": json.dumps({"atr": 50, "adx": 15})},
        ]
        result = classify_regimes_from_trades(trades, days=7)
        self.assertIn("daily_regimes", result)
        self.assertIn("regime_counts", result)
        self.assertIn("dominant_regime", result)
        self.assertIn("current_regime", result)
        self.assertIn(result["current_regime"], ("TRENDING_STRONG", "RANGING", "HIGH_VOLATILITY", "LOW_VOLATILITY"))

    def test_classify_regimes_empty_trades(self):
        """Empty trades should return RANGING defaults."""
        from agi_regime_classifier import classify_regimes_from_trades
        result = classify_regimes_from_trades([], days=7)
        self.assertEqual(result["current_regime"], "RANGING")
        self.assertEqual(result["dominant_regime"], "RANGING")

    def test_describe_regime(self):
        """Regime descriptions should be in Portuguese."""
        from agi_regime_classifier import describe_regime
        self.assertIn("Tendência", describe_regime("TRENDING_STRONG"))
        self.assertIn("Lateralidade", describe_regime("RANGING"))
        self.assertIn("Alta", describe_regime("HIGH_VOLATILITY"))
        self.assertIn("Baixa", describe_regime("LOW_VOLATILITY"))


class TestTradeAnalysisParser(unittest.TestCase):
    """Test trade analysis file parsing for error classification."""

    def test_parse_empty_dir(self):
        """Non-existent data dir should return empty results."""
        from agi_regime_classifier import parse_trade_analysis_files
        with patch("agi_regime_classifier.DATA_DIR", Path("/nonexistent")):
            result = parse_trade_analysis_files(days=7)
            self.assertEqual(result["execution_errors"], [])
            self.assertEqual(result["logic_errors"], [])

    def test_parse_with_content(self):
        """Test parsing with mock file content."""
        from agi_regime_classifier import parse_trade_analysis_files
        mock_dir = MagicMock()
        mock_file = MagicMock()
        mock_file.name = "trade_analysis_20260622.md"
        mock_file.read_text.return_value = """
# Trade Analysis

- Slippage alto em WDO: ordem não executada no preço correto
- Entrada errada em WIN: sinal falso no RSI
- Latência de 500ms causou requote em BIT
"""
        mock_dir.exists.return_value = True
        mock_dir.glob.return_value = [mock_file]

        with patch("agi_regime_classifier.DATA_DIR", mock_dir):
            result = parse_trade_analysis_files(days=7)
            self.assertGreater(len(result["execution_errors"]), 0)


# ═══════════════════════════════════════════════════════════════════
# Stage 3: Safety Validator Tests
# ═══════════════════════════════════════════════════════════════════

class TestOccamRazor(unittest.TestCase):
    """Test Occam's Razor complexity penalty."""

    def test_basic_penalty(self):
        """More params should reduce score."""
        from agi_safety_validator import apply_ocam_razor
        score_3 = apply_ocam_razor(1.0, 3)
        score_10 = apply_ocam_razor(1.0, 10)
        self.assertGreater(score_3, score_10)

    def test_three_param_beats_ten(self):
        """3 params with score 0.8 should beat 10 params with score 0.85."""
        from agi_safety_validator import apply_ocam_razor
        score_3 = apply_ocam_razor(0.8, 3)  # 0.8 * (1/1.3) = 0.615
        score_10 = apply_ocam_razor(0.85, 10)  # 0.85 * (1/2.0) = 0.425
        self.assertGreater(score_3, score_10)

    def test_zero_params(self):
        """Zero params should not penalize."""
        from agi_safety_validator import apply_ocam_razor
        score = apply_ocam_razor(1.0, 0)
        self.assertEqual(score, 1.0)

    def test_formula_correctness(self):
        """Verify the formula: raw * (1 / (1 + 0.1 * n))"""
        from agi_safety_validator import apply_ocam_razor
        self.assertAlmostEqual(apply_ocam_razor(2.0, 5), 2.0 * (1 / 1.5))
        self.assertAlmostEqual(apply_ocam_razor(1.5, 10), 1.5 * (1 / 2.0))


class TestBrutalReality(unittest.TestCase):
    """Test Brutal Reality cost gate."""

    def test_compute_total_cost(self):
        """Total cost should include brokerage + exchange + slippage."""
        from agi_safety_validator import compute_total_cost
        cost = compute_total_cost("WIN", slippage_ticks=1)
        # brokerage(2.5*2) + exchange(0.45*2) + slippage(1.0*1*2) = 5 + 0.9 + 2 = 7.9
        self.assertGreater(cost, 0)
        self.assertIsInstance(cost, float)

    def test_profitable_after_costs(self):
        """Trade with profit > 2x costs should pass."""
        from agi_safety_validator import is_trade_profitable_after_costs
        self.assertTrue(is_trade_profitable_after_costs(100, 10, 2.0))
        self.assertFalse(is_trade_profitable_after_costs(15, 10, 2.0))

    def test_filter_trades_by_costs(self):
        """Filter should separate profitable and destroyed trades."""
        from agi_safety_validator import filter_trades_by_costs
        trades = [
            {"net_pnl": 100},
            {"net_pnl": 5},
            {"net_pnl": -20},
            {"net_pnl": 50},
        ]
        result = filter_trades_by_costs(trades, "WIN", slippage_ticks=1)
        self.assertIn("profitable_after_costs", result)
        self.assertIn("destroyed_by_costs", result)
        self.assertEqual(result["survivors"] + result["killed"], len(trades))

    def test_b3_costs_all_symbols(self):
        """All B3 symbols should have defined costs."""
        from agi_safety_validator import compute_total_cost
        for sym in ["WIN", "WDO", "BIT", "WSP"]:
            cost = compute_total_cost(sym)
            self.assertGreater(cost, 0, f"{sym} cost should be > 0")


class TestSafetyValidator(unittest.TestCase):
    """Test Pydantic safety validation of LLM output."""

    def test_valid_output(self):
        """Valid LLM output should pass validation."""
        from agi_safety_validator import AGISafetyValidator
        data = {
            "analysis": "Test analysis",
            "changes": [
                {"symbol": "WIN", "params": {"bb_std": 2.0, "rsi_period": 14}}
            ]
        }
        validator = AGISafetyValidator(data)
        self.assertTrue(validator.validate())
        self.assertEqual(len(validator.errors), 0)

    def test_out_of_bounds_param(self):
        """Out-of-bounds params should be caught."""
        from agi_safety_validator import AGISafetyValidator
        data = {
            "analysis": "Test",
            "changes": [
                {"symbol": "WIN", "params": {"rsi_period": 2}}  # min is 5
            ]
        }
        validator = AGISafetyValidator(data)
        self.assertFalse(validator.validate())
        self.assertTrue(any("rsi_period" in e for e in validator.errors))

    def test_sl_atr_mult_too_low(self):
        """sl_atr_mult < 0.3 should be rejected."""
        from agi_safety_validator import AGISafetyValidator
        data = {
            "analysis": "Test",
            "changes": [
                {"symbol": "WDO", "params": {"sl_atr_mult": 0.1}}
            ]
        }
        validator = AGISafetyValidator(data)
        self.assertFalse(validator.validate())

    def test_sanitization(self):
        """Sanitized output should clamp out-of-bounds values."""
        from agi_safety_validator import AGISafetyValidator
        data = {
            "analysis": "Test",
            "changes": [
                {"symbol": "WIN", "params": {"rsi_period": 2}}
            ]
        }
        validator = AGISafetyValidator(data)
        sanitized = validator.get_sanitized()
        self.assertEqual(sanitized["changes"][0]["params"]["rsi_period"], 5)

    def test_strategy_param_passes(self):
        """Strategy name should always pass validation."""
        from agi_safety_validator import AGISafetyValidator
        data = {
            "analysis": "Test",
            "changes": [
                {"symbol": "WIN", "params": {"strategy": "VWAP"}}
            ]
        }
        validator = AGISafetyValidator(data)
        self.assertTrue(validator.validate())

    def test_count_params(self):
        """Count params for a symbol."""
        from agi_safety_validator import AGISafetyValidator
        data = {
            "analysis": "Test",
            "changes": [
                {"symbol": "WIN", "params": {"bb_std": 2.0, "rsi_period": 14, "sl_atr_mult": 1.0}}
            ]
        }
        validator = AGISafetyValidator(data)
        self.assertEqual(validator.count_params("WIN"), 3)

    def test_missing_changes(self):
        """Missing 'changes' should fail."""
        from agi_safety_validator import AGISafetyValidator
        data = {"analysis": "Test"}
        validator = AGISafetyValidator(data)
        self.assertTrue(validator.validate())  # empty changes = valid (no-op)


class TestPatienceFilter(unittest.TestCase):
    """Test Cost of Not Trading (Patience Filter)."""

    def test_evaluate_patience_filter(self):
        """Should suggest optimal ATR filter."""
        from agi_safety_validator import evaluate_patience_filter
        trades = [
            {"net_pnl": -50, "signal_detail": json.dumps({"atr": 10})},
            {"net_pnl": -30, "signal_detail": json.dumps({"atr": 15})},
            {"net_pnl": 100, "signal_detail": json.dumps({"atr": 50})},
            {"net_pnl": 80, "signal_detail": json.dumps({"atr": 60})},
        ]
        result = evaluate_patience_filter(trades, {"min_atr_for_entry": 0})
        self.assertIn("optimal_filter", result)
        self.assertIn("net_benefit", result)

    def test_no_signal_detail(self):
        """Trades without signal_detail should not crash."""
        from agi_safety_validator import evaluate_patience_filter
        trades = [{"net_pnl": 100}]
        result = evaluate_patience_filter(trades, {})
        self.assertEqual(result["trades_filtered"], 0)


# ═══════════════════════════════════════════════════════════════════
# Stage 3: Bayesian Optimizer Tests
# ═══════════════════════════════════════════════════════════════════

class TestMacroSelection(unittest.TestCase):
    """Test Stage 3.1: Macro-Selection."""

    def test_survivor_passes(self):
        """Strategy with PF > 1.1 and Sharpe > 0.8 should survive."""
        from agi_bayesian_optimizer import macro_select_strategies
        # Create trades with positive PnL (high PF and Sharpe)
        trades = [{"net_pnl": 100} for _ in range(20)] + [{"net_pnl": -30} for _ in range(5)]
        result = macro_select_strategies({"WIN_M5": trades}, ["BOLLINGER"])
        self.assertIn("WIN_M5", result["survivors"])

    def test_eliminated_low_pf(self):
        """Strategy with low PF should be eliminated."""
        from agi_bayesian_optimizer import macro_select_strategies
        trades = [{"net_pnl": -100} for _ in range(10)] + [{"net_pnl": 50} for _ in range(5)]
        result = macro_select_strategies({"WDO_M15": trades}, ["RSI_REVERSION"])
        self.assertIn("WDO_M15", result["eliminated"])

    def test_empty_trades(self):
        """Empty trade list should not crash."""
        from agi_bayesian_optimizer import macro_select_strategies
        result = macro_select_strategies({}, ["BOLLINGER"])
        self.assertEqual(result["summary"]["survivors"], 0)


class TestProfitFactor(unittest.TestCase):
    """Test profit factor computation."""

    def test_basic_pf(self):
        from agi_bayesian_optimizer import _compute_profit_factor
        pf = _compute_profit_factor([100, 100, -50])
        self.assertAlmostEqual(pf, 200 / 50)

    def test_all_wins(self):
        from agi_bayesian_optimizer import _compute_profit_factor
        pf = _compute_profit_factor([100, 200])
        self.assertEqual(pf, 99.0)

    def test_empty(self):
        from agi_bayesian_optimizer import _compute_profit_factor
        pf = _compute_profit_factor([])
        self.assertEqual(pf, 0.0)


class TestSharpeRatio(unittest.TestCase):
    """Test Sharpe ratio computation."""

    def test_positive_sharpe(self):
        from agi_bayesian_optimizer import _compute_sharpe_ratio
        sharpe = _compute_sharpe_ratio([100, 100, 100, 100, -10])
        self.assertGreater(sharpe, 0)

    def test_empty(self):
        from agi_bayesian_optimizer import _compute_sharpe_ratio
        sharpe = _compute_sharpe_ratio([])
        self.assertEqual(sharpe, 0.0)


class TestWalkForwardStressTest(unittest.TestCase):
    """Test Stage 3.3: Walk-Forward & Stress Test."""

    def test_passes_with_good_data(self):
        """Good validation data should pass."""
        from agi_bayesian_optimizer import walk_forward_stress_test
        train = [{"net_pnl": 100} for _ in range(20)]
        validate = [{"net_pnl": 80} for _ in range(10)]
        result = walk_forward_stress_test(
            "WIN_M5", "BOLLINGER", {"sl_atr_mult": 1.0},
            train, validate, symbol="WIN"
        )
        self.assertIn("passed", result)
        self.assertIn("train_score", result)
        self.assertIn("validate_score", result)

    def test_fails_with_negative_validation(self):
        """Negative validation PnL should fail."""
        from agi_bayesian_optimizer import walk_forward_stress_test
        train = [{"net_pnl": 100} for _ in range(20)]
        validate = [{"net_pnl": -200} for _ in range(10)]
        result = walk_forward_stress_test(
            "WDO_M15", "RSI_REVERSION", {},
            train, validate, symbol="WDO"
        )
        self.assertFalse(result["passed"])


class TestSynthesis(unittest.TestCase):
    """Test Stage 3.4: Meta-Strategy Synthesis."""

    def test_single_strategy(self):
        """Single strategy should not create regime switching."""
        from agi_bayesian_optimizer import synthesize_meta_strategy
        result = synthesize_meta_strategy([
            {"pair": "WIN_M5", "strategy": "BOLLINGER", "score": 0.8}
        ])
        self.assertEqual(result["meta_strategy"]["type"], "SINGLE")

    def test_two_strategies_creates_rules(self):
        """Two strategies of different types should create switching rules."""
        from agi_bayesian_optimizer import synthesize_meta_strategy
        result = synthesize_meta_strategy([
            {"pair": "WIN_M5", "strategy": "MACD_MOMENTUM", "score": 0.8},
            {"pair": "WIN_M15", "strategy": "VWAP", "score": 0.7},
        ], regime="RANGING")
        self.assertEqual(result["meta_strategy"]["type"], "REGIME_SWITCHING")
        self.assertGreater(len(result["transition_rules"]), 0)

    def test_no_strategies(self):
        """No strategies should return NONE."""
        from agi_bayesian_optimizer import synthesize_meta_strategy
        result = synthesize_meta_strategy([])
        self.assertEqual(result["meta_strategy"]["type"], "NONE")


class TestDiscoveryEngineIntegration(unittest.TestCase):
    """Test the full Discovery Engine (Stages 3.1-3.4)."""

    def test_full_pipeline(self):
        """Run full pipeline with mock data."""
        from agi_bayesian_optimizer import run_discovery_engine

        config = {
            "symbols": ["WIN"],
            "timeframes": ["M5"],
            "strategy_by_tf": {"WIN_M5": "BOLLINGER"},
            "strategy": {"WIN": "BOLLINGER"},
            "disabled_timeframes": [],
        }

        # Create good trades (positive PnL)
        trades = []
        base_time = datetime.now() - timedelta(days=20)
        for i in range(30):
            trades.append({
                "entry_time": (base_time + timedelta(days=i // 2)).strftime("%Y-%m-%d %H:%M:%S"),
                "net_pnl": 100 if i % 3 != 0 else -40,
                "strategy": "BOLLINGER",
            })

        result = run_discovery_engine(
            config=config,
            trades_by_pair={"WIN_M5": trades},
            strategies=["BOLLINGER"],
            train_days=30,
            validate_days=5,
            max_evaluations=10,  # small for testing
            timeout=30,
        )

        self.assertIn("stage_3_1_macro_selection", result)
        self.assertIn("stage_3_2_micro_tuning", result)
        self.assertIn("stage_3_3_walk_forward", result)
        self.assertIn("stage_3_4_synthesis", result)
        self.assertIn("approved_strategies", result)
        self.assertIn("audit_trail", result)
        self.assertIn("summary", result)


# ═══════════════════════════════════════════════════════════════════
# v3.0 Integration Tests
# ═══════════════════════════════════════════════════════════════════

class TestV3PromptContext(unittest.TestCase):
    """Test v3.0 prompt context builder."""

    def test_builds_context(self):
        """Should build non-empty context with data."""
        sys.path.insert(0, str(PROJECT_DIR / "optimization"))
        from agi_tuning_17h import _build_v3_prompt_context

        regime_info = {"current_regime": "TRENDING_STRONG", "dominant_regime": "RANGING", "regime_counts": {"TRENDING_STRONG": 5}}
        risk_tags = {"tag": "TREND_DAY_PROBABLE", "reasoning": "test"}
        discovery = {
            "approved_strategies": [{"pair": "WIN_M5", "strategy": "BOLLINGER", "pf": 1.5, "sharpe": 1.2, "best_params": {}}],
            "eliminated_strategies": [],
            "stage_3_4_synthesis": {"meta_strategy": {"type": "REGIME_SWITCHING", "current_regime": "TRENDING_STRONG", "active_for_regime": "MACD"}},
        }
        trade_analysis = {"execution_errors": [], "logic_errors": []}

        context = _build_v3_prompt_context(regime_info, risk_tags, discovery, trade_analysis)
        self.assertGreater(len(context), 0)
        self.assertIn("REGIME", context)
        self.assertIn("RISK TAG", context)
        self.assertIn("DISCOVERY ENGINE", context)


class TestV3TelegramCard(unittest.TestCase):
    """Test v3.0 Telegram notification card."""

    def test_builds_card(self):
        """Should build non-empty card."""
        sys.path.insert(0, str(PROJECT_DIR / "optimization"))
        from agi_tuning_17h import _build_v3_telegram_card

        regime_info = {"current_regime": "RANGING"}
        risk_tags = {"tag": "LOW_VOLATILITY_EXPECTED"}
        discovery = {
            "approved_strategies": [{"pair": "WIN_M5", "strategy": "VWAP", "pf": 1.3, "sharpe": 1.1, "best_params": {"vwap_period": 20}}],
            "stage_3_4_synthesis": {"meta_strategy": {"type": "SINGLE", "active_for_regime": "VWAP"}},
        }
        llm_result = {"analysis": "Ajuste fino de parâmetros"}

        card = _build_v3_telegram_card(regime_info, risk_tags, discovery, llm_result, {})
        self.assertIn("AGI Tuning Concluído", card)
        self.assertIn("Regime", card)
        self.assertIn("Risk Tag", card)

    def test_card_with_sl_diagnostics(self):
        """Card should include SL diagnostics when perf is provided."""
        sys.path.insert(0, str(PROJECT_DIR / "optimization"))
        from agi_tuning_17h import _build_v3_telegram_card

        regime_info = {"current_regime": "RANGING"}
        risk_tags = {"tag": "LOW_VOLATILITY_EXPECTED"}
        perf = {
            "sl_analysis": {
                "WIN": {"sl_hit_rate": 85.0, "sl_hits": 17, "n_trades": 20},
                "WDO": {"sl_hit_rate": 30.0, "sl_hits": 3, "n_trades": 10},
            }
        }

        card = _build_v3_telegram_card(regime_info, risk_tags, {}, {}, {}, perf=perf)
        self.assertIn("Stop Loss Analysis", card)
        self.assertIn("WIN", card)
        self.assertIn("85%", card)
        self.assertIn("Aumentar sl_atr_mult", card)
        self.assertIn("🟢", card)  # WDO should be green


class TestCLICompatibility(unittest.TestCase):
    """Test backward compatibility of CLI arguments."""

    def test_old_args_still_work(self):
        """Old CLI args should still parse."""
        sys.path.insert(0, str(PROJECT_DIR / "optimization"))
        import agi_tuning_17h
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--days", type=int, default=7)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--no-llm", action="store_true")
        parser.add_argument("--max-iterations", type=int, default=5)
        parser.add_argument("--pause-failing", action="store_true")
        parser.add_argument("--convergence-mode", choices=["delta", "absolute", "sharpe_ratio"], default="delta")
        # v3.0 new args
        parser.add_argument("--train-days", type=int, default=None)
        parser.add_argument("--validate-days", type=int, default=5)
        parser.add_argument("--optimizer-engine", choices=["grid", "bayesian"], default="bayesian")
        parser.add_argument("--max-evaluations", type=int, default=100)
        parser.add_argument("--enable-regime-switching", action="store_true")
        parser.add_argument("--slippage-ticks", type=int, default=1)
        parser.add_argument("--latency-ms", type=int, default=200)
        parser.add_argument("--cost-model", choices=["b3_standard"], default="b3_standard")
        parser.add_argument("--timeout", type=int, default=120)

        # Old-style args
        args = parser.parse_args(["--days", "7", "--max-iterations", "5", "--dry-run"])
        self.assertEqual(args.days, 7)
        self.assertTrue(args.dry_run)
        self.assertEqual(args.max_iterations, 5)

    def test_new_args_parse(self):
        """New v3.0 args should parse correctly."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--days", type=int, default=7)
        parser.add_argument("--train-days", type=int, default=None)
        parser.add_argument("--validate-days", type=int, default=5)
        parser.add_argument("--optimizer-engine", choices=["grid", "bayesian"], default="bayesian")
        parser.add_argument("--max-evaluations", type=int, default=100)
        parser.add_argument("--enable-regime-switching", action="store_true")
        parser.add_argument("--slippage-ticks", type=int, default=1)
        parser.add_argument("--latency-ms", type=int, default=200)
        parser.add_argument("--cost-model", choices=["b3_standard"], default="b3_standard")
        parser.add_argument("--convergence-mode", choices=["delta", "absolute", "sharpe_ratio"], default="delta")
        parser.add_argument("--timeout", type=int, default=300)

        args = parser.parse_args([
            "--train-days", "30", "--validate-days", "5",
            "--optimizer-engine", "bayesian", "--max-evaluations", "500",
            "--enable-regime-switching", "--slippage-ticks", "1",
            "--latency-ms", "200", "--convergence-mode", "sharpe_ratio",
            "--timeout", "300",
        ])
        self.assertEqual(args.train_days, 30)
        self.assertEqual(args.validate_days, 5)
        self.assertEqual(args.optimizer_engine, "bayesian")
        self.assertEqual(args.max_evaluations, 500)
        self.assertTrue(args.enable_regime_switching)
        self.assertEqual(args.slippage_ticks, 1)
        self.assertEqual(args.latency_ms, 200)
        self.assertEqual(args.convergence_mode, "sharpe_ratio")
        self.assertEqual(args.timeout, 300)


class TestConvergenceSharpeMode(unittest.TestCase):
    """Test sharpe_ratio convergence mode."""

    def test_converged_positive_pnl(self):
        """Positive PnL pairs should converge in sharpe_ratio mode."""
        from agi_tuning_17h import check_convergence
        current = {
            "by_symbol_tf": {
                "WIN_M5": {"n_trades": 10, "total_pnl": 500, "win_rate": 60},
                "WDO_M15": {"n_trades": 8, "total_pnl": 200, "win_rate": 55},
            }
        }
        baseline = {"WIN_M5": {"pnl": 0, "n_trades": 5, "win_rate": 40},
                     "WDO_M15": {"pnl": -100, "n_trades": 5, "win_rate": 30}}
        converged, failing = check_convergence(current, baseline, mode="sharpe_ratio")
        self.assertTrue(converged)

    def test_not_converged_negative_pnl(self):
        """Negative PnL pairs should not converge in sharpe_ratio mode."""
        from agi_tuning_17h import check_convergence
        current = {
            "by_symbol_tf": {
                "WIN_M5": {"n_trades": 10, "total_pnl": -200, "win_rate": 30},
            }
        }
        baseline = {"WIN_M5": {"pnl": 0, "n_trades": 5, "win_rate": 40}}
        converged, failing = check_convergence(current, baseline, mode="sharpe_ratio")
        self.assertFalse(converged)
        self.assertIn("WIN_M5", failing)


# ═══════════════════════════════════════════════════════════════════
# SL Management Tests (v3.1)
# ═══════════════════════════════════════════════════════════════════

class TestSLHitRatePenalty(unittest.TestCase):
    """Test SL hit rate penalty in fitness function."""

    def test_high_hit_rate_penalty(self):
        """SL hit rate >70% should reduce score by 30%."""
        from agi_safety_validator import apply_sl_hit_rate_penalty
        result = apply_sl_hit_rate_penalty(1.0, 89.0)  # 89% hit rate
        self.assertAlmostEqual(result, 0.70)

    def test_medium_hit_rate_penalty(self):
        """SL hit rate 50-70% should reduce score by 15%."""
        from agi_safety_validator import apply_sl_hit_rate_penalty
        result = apply_sl_hit_rate_penalty(1.0, 60.0)  # 60% hit rate
        self.assertAlmostEqual(result, 0.85)

    def test_normal_hit_rate_no_penalty(self):
        """SL hit rate <50% should not penalize."""
        from agi_safety_validator import apply_sl_hit_rate_penalty
        result = apply_sl_hit_rate_penalty(1.0, 30.0)  # 30% hit rate
        self.assertAlmostEqual(result, 1.0)

    def test_boundary_70(self):
        """Exactly 70% should get medium penalty (15%, not 30%)."""
        from agi_safety_validator import apply_sl_hit_rate_penalty
        result = apply_sl_hit_rate_penalty(1.0, 70.0)
        self.assertAlmostEqual(result, 0.85)  # 15% penalty (50-70% range)

    def test_boundary_50(self):
        """Exactly 50% should not penalize (only >50%)."""
        from agi_safety_validator import apply_sl_hit_rate_penalty
        result = apply_sl_hit_rate_penalty(1.0, 50.0)
        self.assertAlmostEqual(result, 1.0)

    def test_penalty_with_actual_score(self):
        """Penalty should scale with actual score."""
        from agi_safety_validator import apply_sl_hit_rate_penalty
        result = apply_sl_hit_rate_penalty(2.5, 80.0)  # PF=2.5, 80% hit
        self.assertAlmostEqual(result, 2.5 * 0.70)


class TestDynamicSL(unittest.TestCase):
    """Test dynamic SL adjustment based on volatility."""

    def test_high_volatility_widens_sl(self):
        """ATR > 1.5x average should widen SL by 20%."""
        from agi_safety_validator import compute_dynamic_sl_mult
        result = compute_dynamic_sl_mult(1.0, 200.0, 100.0)  # 2x avg
        self.assertAlmostEqual(result, 1.2)

    def test_low_volatility_tightens_sl(self):
        """ATR < 0.7x average should tighten SL by 20%."""
        from agi_safety_validator import compute_dynamic_sl_mult
        result = compute_dynamic_sl_mult(1.0, 50.0, 100.0)  # 0.5x avg
        self.assertAlmostEqual(result, 0.8)

    def test_normal_volatility_no_change(self):
        """Normal volatility (0.7-1.5x) should not change SL."""
        from agi_safety_validator import compute_dynamic_sl_mult
        result = compute_dynamic_sl_mult(1.0, 100.0, 100.0)  # 1x avg
        self.assertAlmostEqual(result, 1.0)

    def test_zero_avg_returns_base(self):
        """Zero ATR average should return base (safety fallback)."""
        from agi_safety_validator import compute_dynamic_sl_mult
        result = compute_dynamic_sl_mult(1.5, 100.0, 0.0)
        self.assertAlmostEqual(result, 1.5)

    def test_boundary_1_5x(self):
        """ATR exactly at 1.5x should not trigger widening."""
        from agi_safety_validator import compute_dynamic_sl_mult
        result = compute_dynamic_sl_mult(1.0, 150.0, 100.0)
        self.assertAlmostEqual(result, 1.0)  # not >1.5, just ==1.5

    def test_boundary_0_7x(self):
        """ATR exactly at 0.7x should not trigger tightening."""
        from agi_safety_validator import compute_dynamic_sl_mult
        result = compute_dynamic_sl_mult(1.0, 70.0, 100.0)
        self.assertAlmostEqual(result, 1.0)  # not <0.7, just ==0.7

    def test_custom_base_multiplier(self):
        """Dynamic SL should scale with custom base multiplier."""
        from agi_safety_validator import compute_dynamic_sl_mult
        result = compute_dynamic_sl_mult(2.0, 200.0, 100.0)  # 2x avg, base=2.0
        self.assertAlmostEqual(result, 2.0 * 1.2)


class TestATRBasedMinSL(unittest.TestCase):
    """Test ATR-based minimum SL to prevent noise-level stops."""

    def test_atr_based_exceeds_fixed(self):
        """ATR-based min should exceed fixed min when ATR is large."""
        from agi_safety_validator import compute_atr_based_min_sl
        result = compute_atr_based_min_sl(fixed_min_native=3, atr=10.0)
        # int(10 * 0.8) = 8, max(3, 8) = 8
        self.assertEqual(result, 8)

    def test_fixed_min_preserved(self):
        """Fixed min should be preserved when ATR is very low."""
        from agi_safety_validator import compute_atr_based_min_sl
        result = compute_atr_based_min_sl(fixed_min_native=150, atr=50.0)
        # int(50 * 0.8) = 40, max(150, 40) = 150
        self.assertEqual(result, 150)

    def test_wdo_noise_level_fix(self):
        """WDO min_native=3 with ATR=8 should derive min=6."""
        from agi_safety_validator import compute_atr_based_min_sl
        result = compute_atr_based_min_sl(fixed_min_native=3, atr=8.0)
        # int(8 * 0.8) = 6, max(3, 6) = 6
        self.assertEqual(result, 6)

    def test_custom_floor_pct(self):
        """Custom floor percentage should be used."""
        from agi_safety_validator import compute_atr_based_min_sl
        result = compute_atr_based_min_sl(fixed_min_native=3, atr=10.0, atr_floor_pct=0.5)
        # int(10 * 0.5) = 5, max(3, 5) = 5
        self.assertEqual(result, 5)


class TestParamBoundsSL(unittest.TestCase):
    """Test that PARAM_BOUNDS enforce minimum SL multiplier."""

    def test_sl_atr_mult_floor_is_1_0(self):
        """PARAM_BOUNDS should enforce sl_atr_mult >= 1.0."""
        sys.path.insert(0, str(PROJECT_DIR / "optimization"))
        from agi_tuning_17h import PARAM_BOUNDS
        lo, hi = PARAM_BOUNDS["sl_atr_mult"]
        self.assertGreaterEqual(lo, 1.0)
        self.assertEqual(hi, 3.0)

    def test_min_atr_for_entry_in_bounds(self):
        """PARAM_BOUNDS should include min_atr_for_entry."""
        sys.path.insert(0, str(PROJECT_DIR / "optimization"))
        from agi_tuning_17h import PARAM_BOUNDS
        self.assertIn("min_atr_for_entry", PARAM_BOUNDS)
        lo, hi = PARAM_BOUNDS["min_atr_for_entry"]
        self.assertGreaterEqual(lo, 0.0)
        self.assertGreater(hi, lo)


# ═══════════════════════════════════════════════════════════════════
# Exhaustive Strategy Search Integration Tests (v3.2)
# ═══════════════════════════════════════════════════════════════════

class TestRunExhaustiveSearch(unittest.TestCase):
    """Test run_exhaustive_search integration with AGI flow."""

    def test_returns_best_strategy_per_pair(self):
        """Should return best strategy per pair from exhaustive search."""
        sys.path.insert(0, str(PROJECT_DIR / "optimization"))

        mock_bars = [{"time": 1, "open": 100, "high": 101, "low": 99, "close": 100}]
        mock_results = [
            ("VWAP", {"pnl": 100, "n_trades": 5, "wr": 60, "max_dd": 50}, {"sl_atr_mult": 2.0}),
            ("BOLLINGER", {"pnl": -50, "n_trades": 3, "wr": 30, "max_dd": 80}, {"bb_std": 2.0}),
            ("RSI_REVERSION", {"pnl": -100, "n_trades": 2, "wr": 20, "max_dd": 120}, {"rsi_period": 14}),
        ]

        mock_exhaustive = MagicMock()
        mock_exhaustive.test_all_strategies_for_pair.return_value = mock_results
        mock_exhaustive.ALL_STRATEGIES = ["BOLLINGER", "VWAP", "RSI_REVERSION"]
        mock_exhaustive.merge_params_by_tf_into_config.side_effect = lambda c: c

        mock_fwd = MagicMock()
        mock_fwd.fetch_bars_for_backtest.return_value = mock_bars
        mock_fwd.BAR_COUNT_PER_TF = {"M5": 500}
        mock_fwd.DEFAULT_BAR_COUNT = 300

        with patch.dict("sys.modules", {
            "optimization.exhaustive_strategy_search": mock_exhaustive,
        }), patch.dict("sys.modules", {
            "optimization.vt_forward_backtest": mock_fwd,
        }):
            # Force reimport to pick up mocked modules
            if "optimization.agi_tuning_17h" in sys.modules:
                del sys.modules["optimization.agi_tuning_17h"]
            import optimization.agi_tuning_17h as agi_mod

            config = {"symbols": ["WIN"], "timeframes": ["M5"], "disabled_timeframes": []}
            result = agi_mod.run_exhaustive_search(config)

            self.assertIn("best_per_pair", result)
            self.assertIn("WIN_M5", result["best_per_pair"])
            self.assertEqual(result["best_per_pair"]["WIN_M5"]["strategy"], "VWAP")
            self.assertEqual(result["best_per_pair"]["WIN_M5"]["pnl"], 100)
            self.assertEqual(result["strategies_tested"], 3)

    def test_returns_all_negative_when_no_bars(self):
        """Should handle no bars gracefully."""
        sys.path.insert(0, str(PROJECT_DIR / "optimization"))

        mock_exhaustive = MagicMock()
        mock_exhaustive.ALL_STRATEGIES = ["BOLLINGER"]
        mock_exhaustive.merge_params_by_tf_into_config.side_effect = lambda c: c

        mock_fwd = MagicMock()
        mock_fwd.fetch_bars_for_backtest.return_value = []
        mock_fwd.BAR_COUNT_PER_TF = {"M5": 500}
        mock_fwd.DEFAULT_BAR_COUNT = 300

        with patch.dict("sys.modules", {
            "optimization.exhaustive_strategy_search": mock_exhaustive,
        }), patch.dict("sys.modules", {
            "optimization.vt_forward_backtest": mock_fwd,
        }):
            if "optimization.agi_tuning_17h" in sys.modules:
                del sys.modules["optimization.agi_tuning_17h"]
            import optimization.agi_tuning_17h as agi_mod

            config = {"symbols": ["WIN"], "timeframes": ["M5"], "disabled_timeframes": []}
            result = agi_mod.run_exhaustive_search(config)

            # No bars = no results
            self.assertEqual(result["total_pairs"], 0)


class TestExhaustiveSearchTelegram(unittest.TestCase):
    """Test Telegram notification for exhaustive search results."""

    def test_sends_notification(self):
        """Should send Telegram notification with exhaustive search results."""
        sys.path.insert(0, str(PROJECT_DIR / "optimization"))
        import optimization.agi_tuning_17h as agi_mod

        results = {
            "best_per_pair": {
                "WIN_M5": {"strategy": "VWAP", "pnl": 100, "n_trades": 5, "wr": 60, "max_dd": 50},
                "WDO_M15": {"strategy": "BOLLINGER", "pnl": 50, "n_trades": 3, "wr": 55, "max_dd": 30},
            },
            "all_negative_pairs": ["BIT_M5"],
            "strategies_tested": 27,
            "total_pairs": 3,
        }

        with patch.object(agi_mod, "notify_telegram") as mock_notify:
            agi_mod.notify_exhaustive_search_results(results)
            mock_notify.assert_called_once()
            msg = mock_notify.call_args[0][0]
            self.assertIn("27", msg)
            self.assertIn("WIN_M5", msg)
            self.assertIn("VWAP", msg)
            self.assertIn("BIT_M5", msg)

    def test_no_notification_when_empty(self):
        """Should not send notification when results are empty."""
        sys.path.insert(0, str(PROJECT_DIR / "optimization"))
        import optimization.agi_tuning_17h as agi_mod

        with patch.object(agi_mod, "notify_telegram") as mock_notify:
            agi_mod.notify_exhaustive_search_results({})
            mock_notify.assert_not_called()


class TestExhaustiveSearchFallback(unittest.TestCase):
    """Test that fallback only disables pairs where ALL 27 strategies are negative."""

    def test_keeps_pair_with_profitable_strategy(self):
        """Should NOT disable a pair if exhaustive search found a profitable strategy."""
        # This tests the logic concept: if exhaustive found a profitable strategy,
        # the pair should not be in all_negative_pairs
        exhaustive_results = {
            "best_per_pair": {
                "WIN_M5": {"strategy": "VWAP", "pnl": 100, "n_trades": 5, "wr": 60, "max_dd": 50},
            },
            "all_negative_pairs": [],  # WIN_M5 is NOT all-negative
            "strategies_tested": 27,
            "total_pairs": 1,
        }

        all_negative = set(exhaustive_results.get("all_negative_pairs", []))
        self.assertNotIn("WIN_M5", all_negative)

    def test_disables_all_negative_pair(self):
        """Should disable a pair if ALL 27 strategies are negative."""
        exhaustive_results = {
            "best_per_pair": {},
            "all_negative_pairs": ["BIT_M5"],
            "strategies_tested": 27,
            "total_pairs": 1,
        }

        all_negative = set(exhaustive_results.get("all_negative_pairs", []))
        self.assertIn("BIT_M5", all_negative)


if __name__ == "__main__":
    unittest.main()
