"""
test_invalid_stops_retry_loop_fix.py
====================================
TDD: reproduces the infinite retry loop bug in safe_modify_sl() and the
insufficient stops_level check in _fix_invalid_stops_modify().

BUGS FIXED:
1. _fix_invalid_stops_modify "too close" path uses point_val*50 instead of
   trade_stops_level → SL can still be "too close" for symbols with high stops_level
2. safe_modify_sl retries same failed SL value → guaranteed failure (no escalation)
3. No MAX_FIX_ATTEMPTS guard → fix can be called 9+ times across retries
4. No convergence guarantee → SL oscillates (880→1100→1140→1020→...)
"""
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from mt5_error_recovery import _fix_invalid_stops_modify, safe_modify_sl


def _make_mock_mt5_orchestrator(info=None, tick=None):
    """Create a mock mt5_orchestrator module."""
    mock = MagicMock()
    def fake_info(symbol):
        return info or {
            "trade_stops_level": 3300,
            "spread": 5,
            "point": 0.01,
            "digits": 2,
        }
    def fake_tick(symbol):
        return tick or {"bid": 330360.0, "ask": 330361.0}
    mock.info = fake_info
    mock.tick = fake_tick
    return mock


class TestFixInvalidStopsUsesStopsLevel(unittest.TestCase):
    """_fix_invalid_stops_modify must use trade_stops_level in the 'too close' path."""

    def test_BIT_BUY_too_close_uses_stops_level_not_50pts(self):
        """BUY: SL in correct side but too close → must use stops_level as min distance.

        BIT trade_stops_level=3300 (33 R$). The 'too close' path currently uses
        point_val*50=0.50 R$ as min_dist. That's 66x too small for BIT.
        The fix should use stops_level so the SL is pushed far enough.
        """
        mock = _make_mock_mt5_orchestrator(
            info={"trade_stops_level": 3300, "spread": 5, "point": 0.01, "digits": 2},
            tick={"bid": 330000.0, "ask": 330001.0},
        )
        with patch.dict(sys.modules, {"mt5_orchestrator": mock}):
            entry_price = 330000.0
            sl_pts_in = 20  # 0.20 R$ below entry → SL=329999.80, current=330000
            # distance = 330000 - 329999.80 = 0.20 R$
            # min_dist with old code = point_val*50 = 0.50 → 0.20 < 0.50, fix triggers
            # min_dist with stops_level = 3300*0.01 = 33.0 R$ → SL pushed to 330000-33=329967
            # new_sl_pts = (330000-329967)/1.0 = 33
            result = _fix_invalid_stops_modify(
                "BITM26", "12345", sl_pts_in, 0.01, entry_price, "BUY"
            )
            # The result should be at least stops_level points (33 executor pts for BIT)
            self.assertGreaterEqual(
                result, 33,
                f"_fix_invalid_stops_modify returned {result} but must be >= stops_level=33 "
                f"for BIT BUY when SL is too close. The 'too close' path is not using "
                f"trade_stops_level."
            )

    def test_BIT_SELL_too_close_uses_stops_level(self):
        """SELL: SL in correct side but too close → must use stops_level as min distance."""
        mock = _make_mock_mt5_orchestrator(
            info={"trade_stops_level": 3300, "spread": 5, "point": 0.01, "digits": 2},
            tick={"bid": 330000.0, "ask": 330001.0},
        )
        with patch.dict(sys.modules, {"mt5_orchestrator": mock}):
            entry_price = 330000.0
            sl_pts_in = 20  # 0.20 R$ above entry → SL=330000.20, current=330001
            # distance = 330000.20 - 330001 = -0.80 → SL below current (wrong side!)
            # → enters the wrong-side path, not "too close"
            # Use a bigger SL: sl_pts=50 → SL=330000.50, current=330001
            # distance = 330000.50 - 330001 = -0.50 → still wrong side
            # Use sl_pts=200 → SL=330002, current=330001
            # distance = 330002 - 330001 = 1.0 R$ → too close (min_dist = 33 R$)
            sl_pts_in = 200
            result = _fix_invalid_stops_modify(
                "BITM26", "12345", sl_pts_in, 0.01, entry_price, "SELL"
            )
            # new_sl_price = 330001 + 33 = 330034 → new_sl_pts = (330034-330000)/1 = 34
            self.assertGreaterEqual(
                result, 33,
                f"_fix_invalid_stops_modify returned {result} but must be >= stops_level=33 "
                f"for BIT SELL when SL is too close."
            )


class TestSafeModifySlMaxFixAttempts(unittest.TestCase):
    """safe_modify_sl must not call _fix_invalid_stops_modify more than 3 times."""

    def test_max_fix_attempts_aborts_after_3(self):
        """After 3 failed Invalid stops fixes, safe_modify_sl must abort."""
        mock_orch = _make_mock_mt5_orchestrator(
            info={"trade_stops_level": 3300, "spread": 5, "point": 0.01, "digits": 2},
            tick={"bid": 330360.0, "ask": 330361.0},
        )
        # modify_sl always fails with Invalid stops
        mock_orch.modify_sl = MagicMock(return_value={
            "status": "error", "error": "Invalid stops"
        })
        fix_calls = []

        original_fix = _fix_invalid_stops_modify

        def tracking_fix(*args, **kwargs):
            fix_calls.append(args)
            # Return a DIFFERENT value each time (to avoid same-value early exit)
            return 800 + len(fix_calls) * 100

        with patch.dict(sys.modules, {"mt5_orchestrator": mock_orch}):
            with patch("mt5_error_recovery._fix_invalid_stops_modify", side_effect=tracking_fix):
                result = safe_modify_sl(
                    "BITM26", "12345", 500, 330360.0, "BUY"
                )

        # Must not be called more than 3 times
        self.assertLessEqual(
            len(fix_calls), 3,
            f"_fix_invalid_stops_modify was called {len(fix_calls)} times. "
            f"Must abort after 3 attempts to avoid the 9+ notification spam."
        )


class TestSafeModifySlSameValueEscalation(unittest.TestCase):
    """When fix returns same sl_pts, safe_modify_sl must escalate or abort."""

    def test_same_value_does_not_infinite_loop(self):
        """If fix returns the same sl_pts on every call, must abort, not loop forever."""
        mock_orch = _make_mock_mt5_orchestrator(
            info={"trade_stops_level": 3300, "spread": 5, "point": 0.01, "digits": 2},
            tick={"bid": 330360.0, "ask": 330361.0},
        )
        # modify_sl always fails with Invalid stops
        mock_orch.modify_sl = MagicMock(return_value={
            "status": "error", "error": "Invalid stops"
        })
        # Fix always returns the same value
        fix_calls = []

        def same_value_fix(*args, **kwargs):
            fix_calls.append(args)
            return 500  # always returns same value as input

        with patch.dict(sys.modules, {"mt5_orchestrator": mock_orch}):
            with patch("mt5_error_recovery._fix_invalid_stops_modify", side_effect=same_value_fix):
                result = safe_modify_sl(
                    "BITM26", "12345", 500, 330360.0, "BUY"
                )

        # Should not call fix more than once when value is same
        # (after same value is detected, should escalate or abort)
        self.assertLessEqual(
            len(fix_calls), 3,
            f"_fix_invalid_stops_modify called {len(fix_calls)} times with same return value. "
            f"Should detect same-value and abort/escalate."
        )
        # Result should indicate failure (not ok)
        self.assertNotEqual(result.get("status"), "ok")


class TestFixInvalidStopsConvergence(unittest.TestCase):
    """_fix_invalid_stops_modify should produce stable results for same tick."""

    def test_same_tick_same_params_returns_stable_value(self):
        """Calling fix twice with identical parameters returns same result."""
        mock = _make_mock_mt5_orchestrator(
            info={"trade_stops_level": 3300, "spread": 5, "point": 0.01, "digits": 2},
            tick={"bid": 330360.0, "ask": 330361.0},
        )
        with patch.dict(sys.modules, {"mt5_orchestrator": mock}):
            r1 = _fix_invalid_stops_modify("BITM26", "12345", 500, 0.01, 330360.0, "BUY")
            r2 = _fix_invalid_stops_modify("BITM26", "12345", r1, 0.01, 330360.0, "BUY")
        # After fix, the value should be stable (calling again returns same)
        self.assertEqual(
            r1, r2,
            f"_fix_invalid_stops_modify not convergent: first call={r1}, "
            f"second call (with first result as input)={r2}. "
            f"This causes the oscillating SL bug (880→1100→1140→1020→...)."
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
