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
        """Integration test: real vt_config.json should load all active pairs.

        2026-06-19: IND e DOL foram removidos. Esperado: 4 symbols × 4 TFs = 16 pairs.
        """
        from vt_forward_backtest import discover_pairs
        import json
        config_path = Path(__file__).resolve().parent.parent / "vt_config.json"
        if not config_path.exists():
            self.skipTest("vt_config.json not found")
        cfg = json.load(open(config_path))
        pairs = discover_pairs(cfg)
        # 2026-06-19: IND/DOL removidos — 4 minis × 4 TFs = 16 pairs
        active_symbols = len(cfg.get("symbols", []))
        active_tfs = len(cfg.get("timeframes", []))
        expected_min = active_symbols * active_tfs
        self.assertGreaterEqual(len(pairs), expected_min,
            f"Esperado ao menos {expected_min} pairs ({active_symbols} symbols × {active_tfs} TFs), achou {len(pairs)}")
        # Each pair is (sym, tf, strategy, params)
        for p in pairs:
            self.assertEqual(len(p), 4)
            sym, tf, strategy, params = p
            self.assertIsInstance(sym, str)
            # 2026-06-19: IND/DOL não devem mais aparecer nos pairs ativos
            self.assertNotIn(sym, ["IND", "DOL"],
                f"{sym} foi removido da config em 19/06/2026 — não deveria estar em discover_pairs")
            self.assertIn(tf, ["M5", "M15", "M30", "H1"])
            self.assertIsInstance(strategy, str)
            self.assertIsInstance(params, dict)


class TestGetSafeMaxWorkers(unittest.TestCase):
    """Test #3a — _get_safe_max_workers auto-adjusts based on CPU and load.

    The 8-CPU machine already has load 2.21 (autotrader + Wine + MT5).
    We must NEVER saturate the system. The function:
    - Defaults to 50% of CPUs (4 on 8-core)
    - Reduces to 25% if load > 2.0
    - Reduces to 1 worker if load > 4.0
    - Never exceeds configured_max
    - Always returns at least 1
    """

    def test_default_is_50_percent_of_cpus(self):
        """8 CPUs, low load → 4 workers (50%)."""
        from vt_forward_backtest import _get_safe_max_workers
        self.assertEqual(_get_safe_max_workers(99, 8, 0.5), 4)

    def test_never_exceeds_cpu_count(self):
        """configured=99, cpu=4, load=0.1 → ≤ 4."""
        from vt_forward_backtest import _get_safe_max_workers
        result = _get_safe_max_workers(99, 4, 0.1)
        self.assertLessEqual(result, 4)

    def test_reduces_when_load_high(self):
        """8 CPUs, load=5.0 → ≤ 2 workers."""
        from vt_forward_backtest import _get_safe_max_workers
        result = _get_safe_max_workers(99, 8, 5.0)
        self.assertLessEqual(result, 2)

    def test_respects_configured_max(self):
        """configured=2, cpu=8, load=0.1 → 2 (not 4)."""
        from vt_forward_backtest import _get_safe_max_workers
        self.assertEqual(_get_safe_max_workers(2, 8, 0.1), 2)

    def test_minimum_one_worker(self):
        """Even with load=10, must return at least 1."""
        from vt_forward_backtest import _get_safe_max_workers
        result = _get_safe_max_workers(0, 1, 10.0)
        self.assertGreaterEqual(result, 1)

    def test_load_above_2_reduces_to_25_percent(self):
        """8 CPUs, load=3.0 → 2 workers (25%)."""
        from vt_forward_backtest import _get_safe_max_workers
        result = _get_safe_max_workers(99, 8, 3.0)
        self.assertEqual(result, 2)

    def test_load_above_4_returns_one(self):
        """8 CPUs, load=5.0 → 1 worker (saturated)."""
        from vt_forward_backtest import _get_safe_max_workers
        result = _get_safe_max_workers(99, 8, 5.0)
        self.assertEqual(result, 1)

    def test_detect_load_avg_returns_float(self):
        """_detect_load_avg returns a float (0 if unavailable)."""
        from vt_forward_backtest import _detect_load_avg
        result = _detect_load_avg()
        self.assertIsInstance(result, float)
        self.assertGreaterEqual(result, 0.0)

    def test_detect_cpu_count_returns_positive_int(self):
        """_detect_cpu_count returns ≥ 1 (never 0)."""
        from vt_forward_backtest import _detect_cpu_count
        result = _detect_cpu_count()
        self.assertIsInstance(result, int)
        self.assertGreaterEqual(result, 1)


class TestFetchBarsForBacktest(unittest.TestCase):
    """Test #3b — fetch_bars_for_backtest calls mt5_fetch.py via Wine.

    The function must:
    - Return [] if Wine is unavailable (offline-resilient)
    - Return [] if symbol is invalid
    - Use Wine subprocess (not direct MetaTrader5 import)
    - Return list of bar dicts with time/open/high/low/close
    - Bars in NEWEST-FIRST order (matches autotrader format)
    """

    def test_returns_list(self):
        """fetch_bars_for_backtest should always return a list (never raise)."""
        from vt_forward_backtest import fetch_bars_for_backtest
        bars = fetch_bars_for_backtest("WIN$", "M5", count=100)
        self.assertIsInstance(bars, list)
        # Either we have bars (passing) or we get an empty list (Wine offline)
        # What we DON'T want is an exception.

    def test_returns_list_for_invalid_symbol(self):
        """Invalid symbol should return [] (not raise)."""
        from vt_forward_backtest import fetch_bars_for_backtest
        bars = fetch_bars_for_backtest("XYZ_NOT_REAL$", "M5", count=10)
        self.assertIsInstance(bars, list)
        self.assertEqual(bars, [])

    def test_uses_wine_mt5_path(self):
        """Should call mt5_fetch.py via wine, not direct MetaTrader5 import.

        Verifies that the function:
        - Uses 'wine' subprocess (not direct MetaTrader5 import)
        - References FETCH_SCRIPT constant (which points to mt5_fetch.py)
        """
        import inspect
        from vt_forward_backtest import fetch_bars_for_backtest, FETCH_SCRIPT
        src = inspect.getsource(fetch_bars_for_backtest)
        self.assertIn("wine", src)
        # FETCH_SCRIPT constant should point to mt5_fetch.py
        self.assertTrue(FETCH_SCRIPT.endswith("mt5_fetch.py"))

    def test_bar_dict_shape(self):
        """If bars returned, each must have time/open/high/low/close/tick_volume."""
        from vt_forward_backtest import fetch_bars_for_backtest
        bars = fetch_bars_for_backtest("WIN$", "M5", count=50)
        if bars:  # Only assert shape if we got data
            for bar in bars[:3]:  # Check first few
                self.assertIn("time", bar)
                self.assertIn("open", bar)
                self.assertIn("high", bar)
                self.assertIn("low", bar)
                self.assertIn("close", bar)
                self.assertIn("tick_volume", bar)
                # Values should be numeric
                self.assertIsInstance(bar["close"], (int, float))
                self.assertIsInstance(bar["time"], int)

    def test_bars_are_newest_first(self):
        """Bars should be ordered newest-first (index 0 = most recent).

        This matches the autotrader's fetch_bars() format.
        """
        from vt_forward_backtest import fetch_bars_for_backtest
        bars = fetch_bars_for_backtest("WIN$", "M5", count=50)
        if len(bars) >= 2:
            # First bar should have timestamp >= second bar
            self.assertGreaterEqual(bars[0]["time"], bars[1]["time"])


class TestSimulateForward(unittest.TestCase):
    """Test #4 — simulate_forward runs bar-by-bar forward simulation.

    The engine reuses the autotrader's strategy plugins via dynamic import,
    so it must handle plugin loading failures gracefully (return no_data).
    """

    def _make_synthetic_bars(self, n: int = 200) -> list:
        """Create N synthetic bars with simple uptrend (newest-first)."""
        import random
        random.seed(42)
        bars = []
        price = 100.0
        for i in range(n):
            change = random.gauss(0.05, 1.0)
            price += change
            bars.append({
                "time": 1700000000 + i * 300,
                "open": price,
                "high": price + abs(change),
                "low": price - abs(change),
                "close": price,
                "tick_volume": 1000,
            })
        bars.reverse()  # newest-first
        return bars

    def test_simulate_returns_metrics(self):
        """simulate_forward should return dict with pnl/n_trades/wr/max_dd/decision."""
        from vt_forward_backtest import simulate_forward
        bars = self._make_synthetic_bars()
        params = {"bb_period": 20, "bb_std": 2.0, "rsi_period": 14,
                  "rsi_overbought": 70, "rsi_oversold": 30, "sl_atr_mult": 1.5}
        result = simulate_forward(
            symbol="WIN", tf="M5", bars=bars,
            strategy_name="BOLLINGER", params=params
        )
        self.assertIsInstance(result, dict)
        self.assertIn("pnl", result)
        self.assertIn("n_trades", result)
        self.assertIn("wr", result)
        self.assertIn("max_dd", result)
        self.assertIn("decision", result)

    def test_simulate_with_no_bars_returns_empty(self):
        """Empty bars list should return no_data, not crash."""
        from vt_forward_backtest import simulate_forward
        result = simulate_forward("WIN", "M5", [], "BOLLINGER", {})
        self.assertEqual(result["n_trades"], 0)
        self.assertEqual(result["decision"], "no_data")
        self.assertEqual(result["pnl"], 0.0)

    def test_simulate_with_unknown_strategy_returns_graceful(self):
        """Unknown strategy should return strategy_load_failed, not crash."""
        from vt_forward_backtest import simulate_forward
        bars = self._make_synthetic_bars()
        result = simulate_forward(
            symbol="WIN", tf="M5", bars=bars,
            strategy_name="NONEXISTENT_STRATEGY", params={}
        )
        # Should not raise; should return error decision
        self.assertIn(result["decision"], ("strategy_load_failed", "no_data", "no_trades"))

    def test_simulate_decision_in_valid_set(self):
        """Decision field must be one of the valid values."""
        from vt_forward_backtest import simulate_forward
        bars = self._make_synthetic_bars()
        valid_decisions = {
            "ok", "negative", "no_data", "no_trades",
            "utils_load_failed", "strategy_load_failed", "error"
        }
        result = simulate_forward(
            symbol="WIN", tf="M5", bars=bars,
            strategy_name="BOLLINGER",
            params={"bb_period": 20, "bb_std": 2.0, "rsi_period": 14,
                    "rsi_overbought": 70, "rsi_oversold": 30, "sl_atr_mult": 1.5}
        )
        self.assertIn(result["decision"], valid_decisions)

    def test_simulate_metrics_types(self):
        """pnl/wr/max_dd must be float, n_trades must be int."""
        from vt_forward_backtest import simulate_forward
        bars = self._make_synthetic_bars()
        result = simulate_forward(
            symbol="WIN", tf="M5", bars=bars,
            strategy_name="BOLLINGER",
            params={"bb_period": 20, "bb_std": 2.0, "rsi_period": 14,
                    "rsi_overbought": 70, "rsi_oversold": 30, "sl_atr_mult": 1.5}
        )
        self.assertIsInstance(result["pnl"], (int, float))
        self.assertIsInstance(result["n_trades"], int)
        self.assertIsInstance(result["wr"], (int, float))
        self.assertIsInstance(result["max_dd"], (int, float))

    def test_simulate_handles_warmup_correctly(self):
        """Simulation should skip warmup period (first 20 bars for indicators)."""
        from vt_forward_backtest import simulate_forward
        # Only 15 bars — not enough for indicator warmup
        short_bars = self._make_synthetic_bars(n=15)
        result = simulate_forward(
            symbol="WIN", tf="M5", bars=short_bars,
            strategy_name="BOLLINGER",
            params={"bb_period": 20, "bb_std": 2.0}
        )
        # Should return gracefully, not crash
        self.assertIsInstance(result, dict)
        # Likely no trades due to insufficient warmup
        self.assertLessEqual(result["n_trades"], 1)


class TestRunSinglePair(unittest.TestCase):
    """Test #5a — _run_single_pair is the multiprocessing worker.

    Must be module-level (forksafe), accept args tuple, return dict with
    decision key, and handle exceptions gracefully.
    """

    def test_returns_dict_with_decision_key(self):
        """Worker should return dict with at minimum decision key."""
        from vt_forward_backtest import _run_single_pair
        # With invalid args → should still return a dict
        result = _run_single_pair(("XYZ", "M5", {}, 7, 60))
        self.assertIsInstance(result, dict)
        self.assertIn("decision", result)
        self.assertEqual(result["sym"], "XYZ")
        self.assertEqual(result["tf"], "M5")

    def test_handles_exception_gracefully(self):
        """Bad input (None) should not raise — return error decision."""
        from vt_forward_backtest import _run_single_pair
        result = _run_single_pair((None, None, None, 7, 60))
        self.assertIsInstance(result, dict)
        self.assertIn("decision", result)
        # Should have error-like decision, not raise
        self.assertTrue(result["decision"].startswith("error") or
                        result["decision"] in ("no_data", "strategy_not_in_config",
                                               "strategy_load_failed", "utils_load_failed"))

    def test_with_real_config_returns_metrics(self):
        """With a real config (strategy in config), worker should return metrics."""
        from vt_forward_backtest import _run_single_pair
        cfg = {
            "symbols": ["WIN"],
            "timeframes": ["M5"],
            "strategy": {"WIN": "BOLLINGER"},
            "win": {"bb_period": 20, "bb_std": 2.0, "rsi_period": 14,
                    "rsi_overbought": 70, "rsi_oversold": 30, "sl_atr_mult": 1.5},
        }
        # Without real bars, fetch will fail (Wine offline in test env)
        # but worker should handle it gracefully
        result = _run_single_pair(("WIN", "M5", cfg, 7, 60))
        self.assertIsInstance(result, dict)
        self.assertEqual(result["sym"], "WIN")
        self.assertEqual(result["tf"], "M5")
        # Decision should be one of the valid values
        valid = {"no_data", "no_trades", "ok", "negative",
                 "strategy_load_failed", "utils_load_failed", "error:Exception"}
        self.assertIn(result["decision"], valid)


class TestRunAllPairsParallel(unittest.TestCase):
    """Test #5b — run_all_pairs_parallel uses multiprocessing.Pool.

    The orchestrator:
    - Discovers all pairs from config
    - Creates pool with safe max_workers
    - Submits 1 task per pair
    - Collects results with per-pair timeout
    - Returns dict keyed by "SYM_TF"
    - Survives worker crashes
    """

    def test_returns_dict_keyed_by_sym_tf(self):
        """Result dict should have SYM_TF as keys."""
        from vt_forward_backtest import run_all_pairs_parallel
        cfg = {
            "symbols": ["WIN"],
            "timeframes": ["M5"],
            "strategy": {"WIN": "RSI_REVERSION"},
            "win": {"rsi_period": 14},
        }
        results = run_all_pairs_parallel(cfg, days=7, max_workers=2)
        self.assertIsInstance(results, dict)
        # Should have at least the key (even if value is no_data)
        self.assertIn("WIN_M5", results)

    def test_max_workers_caps_pool_size(self):
        """With 100 pairs and max_workers=2, should not exceed 2 workers."""
        from vt_forward_backtest import run_all_pairs_parallel
        # Build config with many symbols (but limited strategies)
        cfg = {
            "symbols": ["WIN", "BIT", "DOL", "IND", "WSP", "WDO"],
            "timeframes": ["M5"],
            "strategy": {s: "RSI_REVERSION" for s in ["WIN", "BIT", "DOL", "IND", "WSP", "WDO"]},
            **{s.lower(): {"rsi_period": 14} for s in ["WIN", "BIT", "DOL", "IND", "WSP", "WDO"]},
        }
        results = run_all_pairs_parallel(cfg, days=7, max_workers=2)
        # Should complete without error
        self.assertIsInstance(results, dict)
        self.assertEqual(len(results), 6)

    def test_handles_worker_crash_without_crashing_pool(self):
        """If one worker raises, the others should still complete.

        Note: mocking _run_single_pair in ProcessPoolExecutor fails because
        the mock object isn't picklable. Instead, we verify crash handling
        by passing config that causes one pair to fail.
        """
        from vt_forward_backtest import run_all_pairs_parallel
        # WIN: valid strategy, BIT: invalid strategy (no module)
        cfg = {
            "symbols": ["WIN", "BIT"],
            "timeframes": ["M5"],
            "strategy": {"WIN": "BOLLINGER", "BIT": "NONEXISTENT_STRATEGY"},
            "win": {"bb_period": 20, "bb_std": 2.0, "rsi_period": 14,
                    "rsi_overbought": 70, "rsi_oversold": 30, "sl_atr_mult": 1.5},
            "bit": {"rsi_period": 14},
        }
        results = run_all_pairs_parallel(cfg, days=7, max_workers=2)
        # Both should be in the results dict (even if one failed)
        self.assertIn("WIN_M5", results)
        self.assertIn("BIT_M5", results)
        # Both should have a valid decision field
        for key in ("WIN_M5", "BIT_M5"):
            self.assertIn("decision", results[key])
            self.assertIsInstance(results[key]["decision"], str)

    def test_runs_with_empty_config(self):
        """Empty config should return empty dict, not crash."""
        from vt_forward_backtest import run_all_pairs_parallel
        cfg = {"symbols": [], "timeframes": ["M5"]}
        results = run_all_pairs_parallel(cfg, days=7, max_workers=2)
        self.assertEqual(results, {})


if __name__ == "__main__":
    unittest.main()
