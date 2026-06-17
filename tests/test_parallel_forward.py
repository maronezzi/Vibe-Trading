"""Test #1 — Parallel forward backtest module scaffold.

Following TDD: scaffold + import test. Confirms the 3 stub functions
(discover_pairs, run_all_pairs_parallel, _get_safe_max_workers) are
importable from vt_forward_backtest.

The next tasks (2-5) will replace the stubs with real implementations.
"""
import sys
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))


class TestParallelModuleImport(unittest.TestCase):
    """Tests that the parallel backtest module is importable with expected API."""

    def test_module_imports(self):
        """The vt_forward_backtest module should be importable."""
        import vt_forward_backtest  # noqa: F401
        self.assertTrue(True)  # ImportError would fail the test

    def test_discover_pairs_callable(self):
        """discover_pairs function should exist and be callable."""
        from vt_forward_backtest import discover_pairs
        self.assertTrue(callable(discover_pairs))

    def test_run_all_pairs_parallel_callable(self):
        """run_all_pairs_parallel function should exist and be callable."""
        from vt_forward_backtest import run_all_pairs_parallel
        self.assertTrue(callable(run_all_pairs_parallel))

    def test_get_safe_max_workers_callable(self):
        """_get_safe_max_workers function should exist and be callable."""
        from vt_forward_backtest import _get_safe_max_workers
        self.assertTrue(callable(_get_safe_max_workers))


class TestDiscoverPairs(unittest.TestCase):
    """Test #2 — discover_pairs reads vt_config.json dynamically.

    Critical: NO HARDCODED SYMBOLS. Adding a new asset to config should
    auto-discover without code changes. This is what makes the system
    future-proof for new indices (VALE3, PETR4, etc).
    """

    def _make_config(self, symbols, timeframes, per_tf_overrides=None,
                    strategy_map=None, params_map=None):
        """Build a minimal config dict for testing."""
        cfg = {
            "symbols": symbols,
            "timeframes": timeframes,
            "strategy": strategy_map or {s: "RSI_REVERSION" for s in symbols},
            "per_symbol_timeframes": per_tf_overrides or {},
        }
        # Default params for each symbol (lowercase key)
        default_params = params_map or {s.lower(): {"sl_atr_mult": 0.8} for s in symbols}
        cfg.update(default_params)
        return cfg

    def test_discovers_all_default_pairs(self):
        """2 symbols × 2 TFs = 4 pairs."""
        from vt_forward_backtest import discover_pairs
        cfg = self._make_config(
            symbols=["WIN", "BIT"],
            timeframes=["M5", "M15"],
        )
        pairs = discover_pairs(cfg)
        self.assertEqual(len(pairs), 4)
        syms_tfs = {(p[0], p[1]) for p in pairs}
        self.assertEqual(
            syms_tfs,
            {("WIN", "M5"), ("WIN", "M15"), ("BIT", "M5"), ("BIT", "M15")},
        )

    def test_respects_per_symbol_timeframes_override(self):
        """BIT only operates M30/H1 → not in pairs for M5/M15."""
        from vt_forward_backtest import discover_pairs
        cfg = self._make_config(
            symbols=["WIN", "BIT"],
            timeframes=["M5", "M15", "M30", "H1"],
            per_tf_overrides={"BIT": ["M30", "H1"]},
        )
        pairs = discover_pairs(cfg)
        syms_tfs = {(p[0], p[1]) for p in pairs}
        # WIN has all 4, BIT has only 2
        self.assertEqual(len(pairs), 6)
        self.assertIn(("BIT", "M30"), syms_tfs)
        self.assertIn(("BIT", "H1"), syms_tfs)
        self.assertNotIn(("BIT", "M5"), syms_tfs)
        self.assertNotIn(("BIT", "M15"), syms_tfs)

    def test_returns_strategy_and_params_per_pair(self):
        """Each pair should include (sym, tf, strategy_name, params_dict)."""
        from vt_forward_backtest import discover_pairs
        cfg = self._make_config(
            symbols=["WIN"],
            timeframes=["M5"],
            strategy_map={"WIN": "BOLLINGER"},
            params_map={"win": {"bb_period": 20, "bb_std": 2.0}},
        )
        pairs = discover_pairs(cfg)
        self.assertEqual(len(pairs), 1)
        sym, tf, strategy, params = pairs[0]
        self.assertEqual(sym, "WIN")
        self.assertEqual(tf, "M5")
        self.assertEqual(strategy, "BOLLINGER")
        self.assertEqual(params["bb_period"], 20)
        self.assertEqual(params["bb_std"], 2.0)

    def test_skips_symbols_without_strategy(self):
        """Symbol without strategy assignment should be skipped."""
        from vt_forward_backtest import discover_pairs
        cfg = self._make_config(
            symbols=["WIN", "XYZ"],
            timeframes=["M5"],
            strategy_map={"WIN": "BOLLINGER"},  # XYZ missing
        )
        pairs = discover_pairs(cfg)
        syms = {p[0] for p in pairs}
        self.assertIn("WIN", syms)
        self.assertNotIn("XYZ", syms)

    def test_new_symbol_in_config_is_picked_up_automatically(self):
        """Adding a new asset (VALE3) to config should be auto-discovered."""
        from vt_forward_backtest import discover_pairs
        cfg = self._make_config(
            symbols=["WIN", "BIT", "VALE3"],
            timeframes=["M5", "M15"],
            strategy_map={
                "WIN": "BOLLINGER",
                "BIT": "RSI_REVERSION",
                "VALE3": "EMA_PULLBACK",  # new symbol
            },
            params_map={
                "win": {"bb_period": 20},
                "bit": {"rsi_period": 14},
                "vale3": {"ema_fast": 9, "ema_slow": 21},  # new params
            },
        )
        pairs = discover_pairs(cfg)
        syms = {p[0] for p in pairs}
        self.assertIn("VALE3", syms)
        # Check VALE3 M5 has the right strategy + params
        vale3_m5 = next(p for p in pairs if p[0] == "VALE3" and p[1] == "M5")
        self.assertEqual(vale3_m5[2], "EMA_PULLBACK")
        self.assertEqual(vale3_m5[3]["ema_fast"], 9)
        self.assertEqual(vale3_m5[3]["ema_slow"], 21)

    def test_per_tf_params_override_base_params(self):
        """If config has per-TF params, they should override base params."""
        from vt_forward_backtest import discover_pairs
        cfg = {
            "symbols": ["WIN"],
            "timeframes": ["M5", "M15"],
            "strategy": {"WIN": "BOLLINGER"},
            "win": {
                "bb_period": 20,
                "bb_std": 2.0,
                "M15": {"bb_period": 30},  # M15 uses different period
            },
        }
        pairs = discover_pairs(cfg)
        # Find M5 and M15 pairs
        m5 = next(p for p in pairs if p[1] == "M5")
        m15 = next(p for p in pairs if p[1] == "M15")
        # M5 uses base
        self.assertEqual(m5[3]["bb_period"], 20)
        # M15 uses override (merged with base bb_std=2.0)
        self.assertEqual(m15[3]["bb_period"], 30)
        self.assertEqual(m15[3]["bb_std"], 2.0)

    def test_empty_config_returns_empty_list(self):
        """Empty config (no symbols) should return empty list, not error."""
        from vt_forward_backtest import discover_pairs
        pairs = discover_pairs({"symbols": [], "timeframes": ["M5"]})
        self.assertEqual(pairs, [])

    def test_current_vt_config_loads_correctly(self):
        """Integration test: real vt_config.json should load all 6 symbols × 4 TFs = 24 pairs."""
        from vt_forward_backtest import discover_pairs
        import json
        config_path = Path(__file__).resolve().parent.parent / "vt_config.json"
        if not config_path.exists():
            self.skipTest("vt_config.json not found")
        cfg = json.load(open(config_path))
        pairs = discover_pairs(cfg)
        # Should have at least 6 symbols × 4 TFs = 24 pairs
        self.assertGreaterEqual(len(pairs), 24)
        # Each pair is (sym, tf, strategy, params)
        for p in pairs:
            self.assertEqual(len(p), 4)
            sym, tf, strategy, params = p
            self.assertIsInstance(sym, str)
            self.assertIn(tf, ["M5", "M15", "M30", "H1"])
            self.assertIsInstance(strategy, str)
            self.assertIsInstance(params, dict)


if __name__ == "__main__":
    unittest.main()
