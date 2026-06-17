"""Test — Shadow Mode: snapshot, shadow optimization, comparison, report.

Shadow mode runs AGI on a sandboxed copy of vt_config.json, then compares
the result to the live run. Live config is NEVER touched by shadow.
"""
import sys
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


if __name__ == "__main__":
    unittest.main()
