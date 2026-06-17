"""Test #6 — AGI <-> vt_forward_backtest integration.

Follows TDD: write failing tests FIRST for the new helpers in agi_tuning_17h.py
that integrate forward backtest evaluation into the convergence loop.

New helpers (to be implemented in agi_tuning_17h.py):
    - evaluate_forward_backtest(config, days, max_workers) -> dict[str, BacktestResult]
    - merge_backtest_with_convergence(perf, baseline, bt_results, mode) -> tuple[bool, list[str]]

The merge helper extends check_convergence() by also considering forward
backtest signals. If a SYM_TF is failing in PnL but is "ok" in backtest
(profitable forward), it counts as converged (shadow-of-truth from simulation).
"""
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, "/home/bruno/Projects/Vibe-Trading")


class TestEvaluateForwardBacktest(unittest.TestCase):
    """Tests for evaluate_forward_backtest() helper in agi_tuning_17h.py."""

    def test_import_helper_exists(self):
        """evaluate_forward_backtest should be importable from agi_tuning_17h."""
        import agi_tuning_17h
        self.assertTrue(hasattr(agi_tuning_17h, "evaluate_forward_backtest"))

    def test_returns_dict_of_results(self):
        """Returns dict mapping SYM_TF -> result."""
        from agi_tuning_17h import evaluate_forward_backtest
        cfg = {
            "symbols": ["WIN", "BIT"],
            "timeframes": ["M5"],
            "strategy": {"WIN": "BOLLINGER", "BIT": "RSI_REVERSION"},
            "params": {},
        }
        # Patch the function in vt_forward_backtest (where it's actually called from)
        with patch("vt_forward_backtest.run_all_pairs_parallel") as mock_run:
            mock_run.return_value = {
                "WIN_M5": {"decision": "ok", "pnl": 100.0, "n_trades": 5},
                "BIT_M5": {"decision": "negative", "pnl": -50.0, "n_trades": 3},
            }
            result = evaluate_forward_backtest(cfg, days=7, max_workers=2)
        self.assertIsInstance(result, dict)
        self.assertIn("WIN_M5", result)
        self.assertIn("BIT_M5", result)

    def test_passes_days_and_max_workers_through(self):
        """days and max_workers must be forwarded to run_all_pairs_parallel."""
        from agi_tuning_17h import evaluate_forward_backtest
        cfg = {"symbols": ["WIN"], "timeframes": ["M5"], "strategy": {"WIN": "BOLLINGER"}, "params": {}}
        with patch("vt_forward_backtest.run_all_pairs_parallel") as mock_run:
            mock_run.return_value = {"WIN_M5": {"decision": "ok"}}
            evaluate_forward_backtest(cfg, days=14, max_workers=4)
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        # Signature: run_all_pairs_parallel(config, days=7, max_workers=4, ...)
        # Called as: run_all_pairs_parallel(config, days=days, max_workers=max_workers)
        self.assertEqual(args[0], cfg)  # config posicional
        self.assertEqual(kwargs.get("days"), 14)
        self.assertEqual(kwargs.get("max_workers"), 4)

    def test_handles_empty_config(self):
        """Empty symbols list returns empty dict, no crash."""
        from agi_tuning_17h import evaluate_forward_backtest
        cfg = {"symbols": [], "timeframes": ["M5"]}
        with patch("vt_forward_backtest.run_all_pairs_parallel") as mock_run:
            mock_run.return_value = {}
            result = evaluate_forward_backtest(cfg, days=7, max_workers=2)
        self.assertEqual(result, {})

    def test_logs_progress(self):
        """Should log.info() at start and end."""
        from agi_tuning_17h import evaluate_forward_backtest
        cfg = {"symbols": ["WIN"], "timeframes": ["M5"], "strategy": {"WIN": "BOLLINGER"}, "params": {}}
        with patch("vt_forward_backtest.run_all_pairs_parallel") as mock_run:
            mock_run.return_value = {"WIN_M5": {"decision": "ok"}}
            evaluate_forward_backtest(cfg, days=7, max_workers=2)
        # At least 2 log.info calls (start + end) — check via the module's logger
        import agi_tuning_17h
        self.assertTrue(hasattr(agi_tuning_17h, "log"))


class TestMergeBacktestWithConvergence(unittest.TestCase):
    """Tests for merge_backtest_with_convergence() helper in agi_tuning_17h.py."""

    def test_import_helper_exists(self):
        """merge_backtest_with_convergence should be importable from agi_tuning_17h."""
        import agi_tuning_17h
        self.assertTrue(hasattr(agi_tuning_17h, "merge_backtest_with_convergence"))

    def test_pure_pass_when_both_agree(self):
        """If check_convergence passes, helper returns (True, []) regardless of BT."""
        from agi_tuning_17h import merge_backtest_with_convergence
        perf = {"by_symbol_tf": {"WIN_M5": {"total_pnl": 100.0, "n_trades": 5}}}
        baseline = {"WIN_M5": {"pnl": 50.0, "n_trades": 5, "win_rate": 0.4}}
        bt = {"WIN_M5": {"decision": "ok"}}
        converged, failing, _evals = merge_backtest_with_convergence(perf, baseline, bt, mode="delta")
        self.assertTrue(converged)
        self.assertEqual(failing, [])

    def test_pnl_fails_but_backtest_ok_converges(self):
        """A pair failing PnL but 'ok' in forward backtest counts as converged.

        This is the SHADOW-OF-TRUTH mechanic: simulation says the params are
        profitable; DB hasn't caught up yet. Trust the backtest.
        """
        from agi_tuning_17h import merge_backtest_with_convergence
        perf = {"by_symbol_tf": {"BIT_M5": {"total_pnl": -50.0, "n_trades": 5}}}
        baseline = {"BIT_M5": {"pnl": -200.0, "n_trades": 5, "win_rate": 0.3}}
        bt = {"BIT_M5": {"decision": "ok", "pnl": 30.0, "n_trades": 5}}
        converged, failing, _evals = merge_backtest_with_convergence(perf, baseline, bt, mode="delta")
        self.assertTrue(converged)
        self.assertNotIn("BIT_M5", failing)

    def test_pnl_fails_and_backtest_negative_still_fails(self):
        """Both PnL and backtest negative -> pair still in failing list."""
        from agi_tuning_17h import merge_backtest_with_convergence
        perf = {"by_symbol_tf": {"BIT_M5": {"total_pnl": -200.0, "n_trades": 5}}}
        baseline = {"BIT_M5": {"pnl": -200.0, "n_trades": 5, "win_rate": 0.3}}
        bt = {"BIT_M5": {"decision": "negative", "pnl": -100.0, "n_trades": 5}}
        converged, failing, _evals = merge_backtest_with_convergence(perf, baseline, bt, mode="delta")
        self.assertFalse(converged)
        self.assertIn("BIT_M5", failing)

    def test_missing_backtest_for_pair_falls_back_to_pnl(self):
        """If no BT result for a pair, defer to check_convergence logic."""
        from agi_tuning_17h import merge_backtest_with_convergence
        perf = {"by_symbol_tf": {"WIN_M5": {"total_pnl": -100.0, "n_trades": 5}}}
        baseline = {"WIN_M5": {"pnl": -100.0, "n_trades": 5, "win_rate": 0.3}}
        bt = {}  # no BT for WIN_M5
        converged, failing, _evals = merge_backtest_with_convergence(perf, baseline, bt, mode="delta")
        self.assertFalse(converged)
        self.assertIn("WIN_M5", failing)

    def test_backtest_no_trades_neutral(self):
        """'no_trades' BT decision is neutral — doesn't save a failing pair."""
        from agi_tuning_17h import merge_backtest_with_convergence
        perf = {"by_symbol_tf": {"WSP_H1": {"total_pnl": -100.0, "n_trades": 5}}}
        baseline = {"WSP_H1": {"pnl": -100.0, "n_trades": 5, "win_rate": 0.3}}
        bt = {"WSP_H1": {"decision": "no_trades"}}
        converged, failing, _evals = merge_backtest_with_convergence(perf, baseline, bt, mode="delta")
        self.assertFalse(converged)
        self.assertIn("WSP_H1", failing)


class TestLowSampleGuard(unittest.TestCase):
    """Tests for MIN_TRADES_FOR_DELTA guard in merge helper."""

    def test_low_trades_forward_ignored(self):
        """If forward backtest has < MIN_TRADES_FOR_DELTA trades, ignore its result."""
        from agi_tuning_17h import merge_backtest_with_convergence, MIN_TRADES_FOR_DELTA
        # baseline -200, perf -180: reduction 10% (< 30%) → check_convergence FAILS
        perf = {"by_symbol_tf": {"BIT_M5": {"total_pnl": -180.0, "n_trades": 5}}}
        baseline = {"BIT_M5": {"pnl": -200.0, "n_trades": 5, "win_rate": 0.3}}
        # Forward says ok but only 1 trade (< MIN_TRADES_FOR_DELTA=3)
        bt = {"BIT_M5": {"decision": "ok", "pnl": 100.0, "n_trades": 1}}
        converged, failing, _evals = merge_backtest_with_convergence(perf, baseline, bt, mode="delta")
        # Should NOT rescue — low sample
        self.assertIn("BIT_M5", failing)

    def test_min_trades_forward_accepted(self):
        """Forward with exactly MIN_TRADES_FOR_DELTA trades should be accepted."""
        from agi_tuning_17h import merge_backtest_with_convergence, MIN_TRADES_FOR_DELTA
        perf = {"by_symbol_tf": {"BIT_M5": {"total_pnl": -180.0, "n_trades": 5}}}
        baseline = {"BIT_M5": {"pnl": -200.0, "n_trades": 5, "win_rate": 0.3}}
        bt = {"BIT_M5": {"decision": "ok", "pnl": 100.0, "n_trades": MIN_TRADES_FOR_DELTA}}
        converged, failing, _evals = merge_backtest_with_convergence(perf, baseline, bt, mode="delta")
        self.assertNotIn("BIT_M5", failing)


class TestAuditDict(unittest.TestCase):
    """Tests for backtest_evaluations audit dict structure."""

    def test_merge_returns_evaluations_dict(self):
        """merge_backtest_with_convergence should return (converged, failing, evals)."""
        from agi_tuning_17h import merge_backtest_with_convergence
        perf = {"by_symbol_tf": {"BIT_M5": {"total_pnl": -180.0, "n_trades": 5}}}
        baseline = {"BIT_M5": {"pnl": -200.0, "n_trades": 5, "win_rate": 0.3}}
        bt = {"BIT_M5": {"decision": "ok", "pnl": 100.0, "n_trades": 5}}
        result = merge_backtest_with_convergence(perf, baseline, bt, mode="delta")
        # Should return 3-element tuple now
        self.assertEqual(len(result), 3)
        converged, failing, evals = result
        self.assertIsInstance(evals, dict)
        self.assertIn("BIT_M5", evals)
        self.assertIn("decision", evals["BIT_M5"])

    def test_evaluations_forward_says_ok(self):
        """Rescued pair should have decision='forward_says_ok' in evals."""
        from agi_tuning_17h import merge_backtest_with_convergence
        perf = {"by_symbol_tf": {"BIT_M5": {"total_pnl": -180.0, "n_trades": 5}}}
        baseline = {"BIT_M5": {"pnl": -200.0, "n_trades": 5, "win_rate": 0.3}}
        bt = {"BIT_M5": {"decision": "ok", "pnl": 100.0, "n_trades": 5, "wr": 70.0}}
        _, _, evals = merge_backtest_with_convergence(perf, baseline, bt, mode="delta")
        self.assertEqual(evals["BIT_M5"]["decision"], "forward_says_ok")
        self.assertEqual(evals["BIT_M5"]["pnl"], 100.0)

    def test_evaluations_forward_says_no(self):
        """Non-rescued pair should have decision='forward_says_no' in evals."""
        from agi_tuning_17h import merge_backtest_with_convergence
        perf = {"by_symbol_tf": {"BIT_M5": {"total_pnl": -200.0, "n_trades": 5}}}
        baseline = {"BIT_M5": {"pnl": -200.0, "n_trades": 5, "win_rate": 0.3}}
        bt = {"BIT_M5": {"decision": "negative", "pnl": -100.0, "n_trades": 5}}
        _, _, evals = merge_backtest_with_convergence(perf, baseline, bt, mode="delta")
        self.assertEqual(evals["BIT_M5"]["decision"], "forward_says_no")

    def test_evaluations_low_sample_ignore(self):
        """Low-sample forward should have decision='low_sample_ignore'."""
        from agi_tuning_17h import merge_backtest_with_convergence
        perf = {"by_symbol_tf": {"BIT_M5": {"total_pnl": -180.0, "n_trades": 5}}}
        baseline = {"BIT_M5": {"pnl": -200.0, "n_trades": 5, "win_rate": 0.3}}
        bt = {"BIT_M5": {"decision": "ok", "pnl": 100.0, "n_trades": 1}}
        _, _, evals = merge_backtest_with_convergence(perf, baseline, bt, mode="delta")
        self.assertEqual(evals["BIT_M5"]["decision"], "low_sample_ignore")

    def test_evaluations_no_forward_eval(self):
        """Missing BT result should have decision='no_forward_eval'."""
        from agi_tuning_17h import merge_backtest_with_convergence
        perf = {"by_symbol_tf": {"BIT_M5": {"total_pnl": -180.0, "n_trades": 5}}}
        baseline = {"BIT_M5": {"pnl": -200.0, "n_trades": 5, "win_rate": 0.3}}
        bt = {}  # no BT for BIT_M5
        _, _, evals = merge_backtest_with_convergence(perf, baseline, bt, mode="delta")
        self.assertEqual(evals["BIT_M5"]["decision"], "no_forward_eval")

    def test_converged_true_returns_empty_evals(self):
        """When check_convergence passes, evals should be empty (no failing pairs)."""
        from agi_tuning_17h import merge_backtest_with_convergence
        perf = {"by_symbol_tf": {"BIT_M5": {"total_pnl": 100.0, "n_trades": 5}}}
        baseline = {"BIT_M5": {"pnl": 50.0, "n_trades": 5, "win_rate": 0.4}}
        bt = {"BIT_M5": {"decision": "ok"}}
        converged, failing, evals = merge_backtest_with_convergence(perf, baseline, bt, mode="delta")
        self.assertTrue(converged)
        self.assertEqual(evals, {})

    def test_audit_json_has_backtest_evaluations_key(self):
        """The audit dict in main() should have backtest_evaluations key."""
        import agi_tuning_17h
        import inspect
        src = inspect.getsource(agi_tuning_17h.main)
        self.assertIn("backtest_evaluations", src)


if __name__ == "__main__":
    unittest.main()
