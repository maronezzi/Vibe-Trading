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


if __name__ == "__main__":
    unittest.main()
