"""Test — Shadow Mode: snapshot, shadow optimization, comparison, report.

Shadow mode runs AGI on a sandboxed copy of vt_config.json, then compares
the result to the live run. Live config is NEVER touched by shadow.
"""
import sys
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestShadowModuleImport(unittest.TestCase):
    """All 4 shadow functions should be importable from agi_tuning_17h."""

    def test_snapshot_live_config_importable(self):
        from agi_tuning_17h import snapshot_live_config
        self.assertTrue(callable(snapshot_live_config))

    def test_run_shadow_optimization_importable(self):
        from agi_tuning_17h import run_shadow_optimization
        self.assertTrue(callable(run_shadow_optimization))

    def test_compare_live_vs_shadow_importable(self):
        from agi_tuning_17h import compare_live_vs_shadow
        self.assertTrue(callable(compare_live_vs_shadow))

    def test_write_comparison_report_importable(self):
        from agi_tuning_17h import write_comparison_report
        self.assertTrue(callable(write_comparison_report))


class TestSnapshotLiveConfig(unittest.TestCase):
    """snapshot_live_config creates a timestamped copy with atomic write."""

    def test_snapshot_creates_file(self):
        from agi_tuning_17h import snapshot_live_config
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"strategy": {"BIT": "RSI_REVERSION"}, "bit": {"sl_atr_mult": 0.8}}, f)
            tmp = Path(f.name)
        try:
            snap = snapshot_live_config(tmp)
            self.assertTrue(snap.exists())
            self.assertIn("vt_config_live_", snap.name)
            # Content is identical
            orig = json.load(open(tmp))
            copy = json.load(open(snap))
            self.assertEqual(orig, copy)
        finally:
            os.unlink(str(tmp))
            if snap.exists():
                os.unlink(str(snap))

    def test_snapshot_atomic_uses_rename(self):
        """Snapshot should use atomic write (tmp file + rename)."""
        import inspect
        from agi_tuning_17h import snapshot_live_config
        src = inspect.getsource(snapshot_live_config)
        self.assertIn(".tmp", src)
        self.assertIn("rename", src)


class TestCompareLiveVsShadow(unittest.TestCase):
    """compare_live_vs_shadow diffs two audit dicts."""

    def test_compares_changes_per_pair(self):
        from agi_tuning_17h import compare_live_vs_shadow
        live = {
            "iterations": [
                {"iteration": 1, "changes": [
                    {"symbol": "BIT", "params": {"sl_atr_mult": 0.6}, "reason": "x"},
                    {"symbol": "WIN", "params": {"cooldown_seconds": 900}, "reason": "y"},
                ], "failing_pairs": ["WIN_M5"], "converged": False},
            ],
            "converged": False,
        }
        shadow = {
            "iterations": [
                {"iteration": 1, "changes": [
                    {"symbol": "BIT", "params": {"sl_atr_mult": 0.7}, "reason": "x"},
                ], "failing_pairs": [], "converged": True},
            ],
            "converged": True,
        }
        diff = compare_live_vs_shadow(live, shadow)
        self.assertIn("agreements", diff)
        self.assertIn("disagreements", diff)
        self.assertIn("convergence_diff", diff)
        # BIT is a disagreement (0.6 vs 0.7)
        bit_diff = next(d for d in diff["disagreements"] if d["symbol"] == "BIT")
        self.assertEqual(bit_diff["live"]["sl_atr_mult"], 0.6)
        self.assertEqual(bit_diff["shadow"]["sl_atr_mult"], 0.7)
        # WIN is live-only
        self.assertTrue(any(c["symbol"] == "WIN" for c in diff["live_only"]))
        # Convergence diff
        self.assertEqual(diff["convergence_diff"]["live"], False)
        self.assertEqual(diff["convergence_diff"]["shadow"], True)

    def test_no_iterations_returns_empty_diff(self):
        from agi_tuning_17h import compare_live_vs_shadow
        diff = compare_live_vs_shadow({"iterations": []}, {"iterations": []})
        self.assertEqual(diff["agreements"], [])
        self.assertEqual(diff["disagreements"], [])

    def test_same_changes_are_agreements(self):
        from agi_tuning_17h import compare_live_vs_shadow
        both = {
            "iterations": [{"changes": [
                {"symbol": "BIT", "params": {"sl_atr_mult": 0.6}},
            ]}],
        }
        diff = compare_live_vs_shadow(both, both)
        self.assertEqual(len(diff["agreements"]), 1)
        self.assertEqual(diff["agreements"][0]["symbol"], "BIT")

    def test_shadow_only_changes(self):
        from agi_tuning_17h import compare_live_vs_shadow
        live = {"iterations": []}
        shadow = {"iterations": [{"changes": [
            {"symbol": "DOL", "params": {"cooldown_seconds": 300}},
        ]}]}
        diff = compare_live_vs_shadow(live, shadow)
        self.assertEqual(len(diff["shadow_only"]), 1)
        self.assertEqual(diff["shadow_only"][0]["symbol"], "DOL")


class TestWriteComparisonReport(unittest.TestCase):
    """write_comparison_report saves to timestamped file."""

    def test_writes_to_timestamped_file(self):
        from agi_tuning_17h import write_comparison_report
        diff = {
            "agreements": [{"symbol": "BIT", "params": {"sl_atr_mult": 0.7}}],
            "disagreements": [],
            "live_only": [],
            "shadow_only": [],
            "convergence_diff": {"live": True, "shadow": True},
            "failing_diff": {"live": [], "shadow": []},
        }
        path = write_comparison_report(diff, audit_path=Path("/tmp/vt_agi_audit.json"))
        self.assertTrue(path.exists())
        self.assertIn("vt_agi_comparison_", path.name)
        loaded = json.load(open(str(path)))
        self.assertEqual(loaded["agreements"][0]["symbol"], "BIT")
        # Cleanup
        os.unlink(str(path))


class TestRunShadowOptimization(unittest.TestCase):
    """run_shadow_optimization runs AGI on a sandboxed config."""

    def test_runs_dry_run_against_snapshot(self):
        from agi_tuning_17h import run_shadow_optimization
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({
                "strategy": {"BIT": "RSI_REVERSION"},
                "bit": {"sl_atr_mult": 0.8, "cooldown_seconds": 600},
                "win": {"bb_period": 20, "bb_std": 2.0},
                "_version": 100,
                "symbols": ["BIT"],
                "timeframes": ["M5"],
            }, f)
            snap_path = Path(f.name)
        try:
            shadow_audit = run_shadow_optimization(
                snap_path, perf={"by_symbol_tf": {}}, issues=[],
                days=7, use_forward=False,
            )
            self.assertIsInstance(shadow_audit, dict)
            # Either got a valid audit or empty (if no trades)
            if shadow_audit:
                self.assertIn("converged", shadow_audit)
            self.assertTrue(snap_path.exists())
        finally:
            os.unlink(str(snap_path))

    def test_does_not_modify_snapshot_file(self):
        """The snapshot file must remain unchanged after shadow run."""
        from agi_tuning_17h import run_shadow_optimization
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            cfg = {
                "strategy": {"BIT": "RSI_REVERSION"},
                "bit": {"sl_atr_mult": 0.8},
                "_version": 100,
            }
            json.dump(cfg, f)
            snap_path = Path(f.name)
        try:
            run_shadow_optimization(snap_path, perf={}, issues=[], days=7, use_forward=False)
            after = json.load(open(str(snap_path)))
            self.assertEqual(after["bit"]["sl_atr_mult"], 0.8)
        finally:
            os.unlink(str(snap_path))


if __name__ == "__main__":
    unittest.main()
