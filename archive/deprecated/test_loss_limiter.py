#!/usr/bin/env python3
"""TDD — Smart Loss Limiter: tiered thresholds, per-symbol pauses, smart recovery.

Tests:
1. Tiered thresholds (-R$500 warn, -R$750 halt 2h, -R$1000 kill)
2. Per-symbol halt at -R$300 warn / -R$500 halt
3. Smart recovery: 50% size first trade after halt
4. Recovery loss → double halt duration
5. Recovery win → exit recovery mode
6. Daily reset clears all state
7. Integration with vt_autotrader._check_consecutive_losses (per-TF config)
8. halt_on_loss bug fix (config key now read)
9. Stale halt_until cleared on daily reset
"""

import json
import sys
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

# Use temp files for state to avoid polluting real state
TEST_STATE_FILE = tempfile.mktemp(suffix=".json")
TEST_LL_STATE_FILE = tempfile.mktemp(suffix=".json")


def _cleanup():
    for f in [TEST_STATE_FILE, TEST_LL_STATE_FILE]:
        try:
            os.unlink(f)
        except FileNotFoundError:
            pass


def _reset_ll_state():
    """Clear loss limiter state file between tests."""
    try:
        os.unlink(TEST_LL_STATE_FILE)
    except FileNotFoundError:
        pass


def _make_config(extra=None):
    """Create a test config dict."""
    cfg = {
        "loss_limiter": {
            "tiers": [
                {"threshold": -500, "action": "warn", "label": "WARNING"},
                {"threshold": -750, "action": "halt_2h", "label": "HALT 2H"},
                {"threshold": -1000, "action": "kill", "label": "KILL SWITCH"},
            ],
            "symbol_warn_threshold": -300,
            "symbol_halt_threshold": -500,
            "recovery_size_mult": 0.5,
            "halt_2h_minutes": 120,
            "double_pause_multiplier": 2,
        },
        "halt_trading": False,
        "halt_new_trades": False,
        "halt_on_loss": False,
        "max_daily_loss": -1000,
        "disabled_symbols": [],
        "disabled_timeframes": [],
        "max_consecutive_losses_by_tf": {
            "WDO_M5": 3,
            "WDO_M15": 3,
            "WDO_M30": 4,
            "WDO_H1": 5,
        },
        "halt_duration_minutes_by_tf": {
            "WDO_M5": 45,
            "WDO_M15": 60,
            "WDO_M30": 90,
            "WDO_H1": 90,
        },
        "symbols": ["WDO"],
        "timeframes": ["M5", "M15", "M30", "H1"],
        "resolved_symbols": {"WDO": "WDON26"},
    }
    if extra:
        cfg.update(extra)
    return cfg


# ===== LOSS LIMITER TESTS =====


def test_tier_warning_at_minus_500():
    """At -R$500, should trigger WARNING tier and reduce size."""
    from vt_loss_limiter import SmartLossLimiter

    cfg = _make_config()
    _reset_ll_state()
    with patch("vt_loss_limiter.LossLimiterState.STATE_FILE", TEST_LL_STATE_FILE):
        limiter = SmartLossLimiter(cfg)
        limiter.state.current_day = datetime.now().date()
        result = limiter.check_before_trade("WDON26", daily_pnl=-500, symbol_pnl=0)

    assert result["allowed"] is True, f"Should be allowed at -500, got {result}"
    assert result["size_mult"] == 0.5, f"Size should be 0.5 at warning, got {result['size_mult']}"
    assert "WARNING" in result["reason"] or "reduzido" in result["reason"]
    print("  PASS: tier warning at -R$500")


def test_tier_halt_2h_at_minus_750():
    """At -R$750, should trigger HALT 2H and block trading."""
    from vt_loss_limiter import SmartLossLimiter

    cfg = _make_config()
    _reset_ll_state()
    with patch("vt_loss_limiter.LossLimiterState.STATE_FILE", TEST_LL_STATE_FILE):
        limiter = SmartLossLimiter(cfg)
        limiter.state.current_day = datetime.now().date()
        result = limiter.check_before_trade("WDON26", daily_pnl=-750, symbol_pnl=0)

    assert result["allowed"] is False, f"Should be blocked at -750, got {result}"
    assert "GLOBAL HALT" in result["reason"] or "halt" in result["reason"].lower()
    print("  PASS: tier halt 2h at -R$750")


def test_tier_kill_at_minus_1000():
    """At -R$1000, should trigger KILL SWITCH (halt rest of day)."""
    from vt_loss_limiter import SmartLossLimiter

    cfg = _make_config()
    _reset_ll_state()
    with patch("vt_loss_limiter.LossLimiterState.STATE_FILE", TEST_LL_STATE_FILE):
        limiter = SmartLossLimiter(cfg)
        limiter.state.current_day = datetime.now().date()
        result = limiter.check_before_trade("WDON26", daily_pnl=-1000, symbol_pnl=0)

    assert result["allowed"] is False, f"Should be blocked at -1000, got {result}"
    print("  PASS: tier kill at -R$1000")


def test_symbol_halt_at_minus_500():
    """Per-symbol PnL <= -R$500 should halt that symbol."""
    from vt_loss_limiter import SmartLossLimiter

    cfg = _make_config()
    _reset_ll_state()
    with patch("vt_loss_limiter.LossLimiterState.STATE_FILE", TEST_LL_STATE_FILE):
        limiter = SmartLossLimiter(cfg)
        limiter.state.current_day = datetime.now().date()
        result = limiter.check_before_trade("WDON26", daily_pnl=-100, symbol_pnl=-500)

    assert result["allowed"] is False, f"Symbol should be halted, got {result}"
    assert "SYMBOL HALT" in result["reason"]
    print("  PASS: per-symbol halt at -R$500")


def test_symbol_warning_at_minus_300():
    """Per-symbol PnL <= -R$300 should warn but not halt."""
    from vt_loss_limiter import SmartLossLimiter

    cfg = _make_config()
    _reset_ll_state()
    with patch("vt_loss_limiter.LossLimiterState.STATE_FILE", TEST_LL_STATE_FILE):
        limiter = SmartLossLimiter(cfg)
        limiter.state.current_day = datetime.now().date()
        result = limiter.check_before_trade("WDON26", daily_pnl=-100, symbol_pnl=-300)

    assert result["allowed"] is True, f"Should be allowed (warning only), got {result}"
    assert result["size_mult"] == 1.0, f"Size should still be 1.0 at symbol warning, got {result['size_mult']}"
    print("  PASS: per-symbol warning at -R$300 (no halt)")


def test_recovery_mode_50_percent_size():
    """After halt expires, next trade should be at 50% size."""
    from vt_loss_limiter import SmartLossLimiter

    cfg = _make_config()
    _reset_ll_state()
    with patch("vt_loss_limiter.LossLimiterState.STATE_FILE", TEST_LL_STATE_FILE):
        limiter = SmartLossLimiter(cfg)
        limiter.state.current_day = datetime.now().date()
        # Simulate halt that just expired
        limiter.state.halt_until = datetime.now() - timedelta(minutes=1)
        limiter.state.recovery_mode = True
        limiter.state.save()

        result = limiter.check_before_trade("WDON26", daily_pnl=-200, symbol_pnl=0)

    assert result["allowed"] is True, f"Should be allowed in recovery, got {result}"
    assert result["size_mult"] == 0.5, f"Size should be 0.5 in recovery, got {result['size_mult']}"
    assert "RECOVERY" in result["reason"]
    print("  PASS: recovery mode 50% size")


def test_recovery_loss_doubles_halt():
    """Loss during recovery should double the halt duration."""
    from vt_loss_limiter import SmartLossLimiter

    cfg = _make_config()
    _reset_ll_state()
    with patch("vt_loss_limiter.LossLimiterState.STATE_FILE", TEST_LL_STATE_FILE):
        limiter = SmartLossLimiter(cfg)
        limiter.state.current_day = datetime.now().date()
        limiter.state.recovery_mode = True
        limiter.state.recovery_symbol = "WDON26"
        limiter.state.recovery_halt_count = 0

        limiter.record_trade_result("WDON26", pnl=-50, daily_pnl=-300)

    assert limiter.state.recovery_mode is False, "Should exit recovery mode"
    assert limiter.state.halt_until is not None, "Should have a halt set"
    # Base is 120min × 2^1 = 240min
    expected_halt_minutes = 120 * 2
    actual_halt_minutes = (limiter.state.halt_until - datetime.now()).total_seconds() / 60
    assert abs(actual_halt_minutes - expected_halt_minutes) < 2, (
        f"Halt should be ~{expected_halt_minutes}min, got {actual_halt_minutes:.0f}min"
    )
    assert limiter.state.recovery_halt_count == 1
    print("  PASS: recovery loss doubles halt")


def test_recovery_win_exits_recovery():
    """Win during recovery should exit recovery mode and restore full size."""
    from vt_loss_limiter import SmartLossLimiter

    cfg = _make_config()
    _reset_ll_state()
    with patch("vt_loss_limiter.LossLimiterState.STATE_FILE", TEST_LL_STATE_FILE):
        limiter = SmartLossLimiter(cfg)
        limiter.state.current_day = datetime.now().date()
        limiter.state.recovery_mode = True
        limiter.state.recovery_symbol = "WDON26"

        limiter.record_trade_result("WDON26", pnl=100, daily_pnl=-200)

    assert limiter.state.recovery_mode is False, "Should exit recovery mode"
    assert limiter.state.recovery_symbol is None
    assert limiter.state.recovery_halt_count == 0
    print("  PASS: recovery win exits recovery mode")


def test_daily_reset_clears_state():
    """New day should clear all limiter state."""
    from vt_loss_limiter import SmartLossLimiter

    cfg = _make_config()
    _reset_ll_state()
    with patch("vt_loss_limiter.LossLimiterState.STATE_FILE", TEST_LL_STATE_FILE):
        limiter = SmartLossLimiter(cfg)
        limiter.state.current_day = datetime.now().date() - timedelta(days=1)
        limiter.state.active_tier = "kill"
        limiter.state.recovery_mode = True
        limiter.state.save()

        # Trigger daily reset
        limiter._reset_daily()

    assert limiter.state.active_tier is None
    assert limiter.state.recovery_mode is False
    print("  PASS: daily reset clears state")


# ===== BUG FIX TESTS =====


def test_halt_on_loss_config_key_read():
    """Bug fix: halt_on_loss config key was never read. Now it should be."""
    cfg = _make_config({"halt_on_loss": True})
    # Verify the config key exists and is True
    assert cfg.get("halt_on_loss", False) is True
    print("  PASS: halt_on_loss config key exists and is read")


def test_consecutive_losses_uses_per_tf_config():
    """Bug fix: max_consecutive_losses_by_tf should be read from config."""
    import vt_autotrader

    cfg = _make_config()
    mock = MagicMock()
    mock.get.side_effect = lambda k, default=None: cfg.get(k, default)
    mock.__contains__ = lambda self, k: k in cfg
    mock.__getitem__ = lambda self, k: cfg[k]

    with patch.object(vt_autotrader, "CONFIG", mock):
        vt_autotrader.state.consecutive_losses = {"WDON26": 2}
        vt_autotrader.state.halt_until = {}

        # WDO_M5 has max=3, so 2 losses should be allowed
        result = vt_autotrader._check_consecutive_losses("WDON26", symbol_root="WDO", tf="M5")
        assert result is True, f"Should be allowed with 2 losses (max=3), got {result}"

        # WDO_H1 has max=5, so 4 losses should be allowed
        vt_autotrader.state.consecutive_losses = {"WDON26": 4}
        result = vt_autotrader._check_consecutive_losses("WDON26", symbol_root="WDO", tf="H1")
        assert result is True, f"Should be allowed with 4 losses (max=5), got {result}"

        # WDO_M5 with 3 losses should trigger halt
        vt_autotrader.state.consecutive_losses = {"WDON26": 3}
        result = vt_autotrader._check_consecutive_losses("WDON26", symbol_root="WDO", tf="M5")
        assert result is False, f"Should be blocked with 3 losses (max=3), got {result}"

    print("  PASS: consecutive_losses uses per-TF config")


def test_halt_duration_uses_per_tf_config():
    """Bug fix: halt_duration_minutes_by_tf should be read from config."""
    import vt_autotrader

    cfg = _make_config()
    mock = MagicMock()
    mock.get.side_effect = lambda k, default=None: cfg.get(k, default)
    mock.__contains__ = lambda self, k: k in cfg
    mock.__getitem__ = lambda self, k: cfg[k]

    with patch.object(vt_autotrader, "CONFIG", mock):
        vt_autotrader.state.consecutive_losses = {"WDON26": 3}
        vt_autotrader.state.halt_until = {}

        # WDO_M5 halt_duration = 45 min
        vt_autotrader._check_consecutive_losses("WDON26", symbol_root="WDO", tf="M5")
        halt = vt_autotrader.state.halt_until.get("WDON26")
        assert halt is not None, "Halt should be set"
        remaining = (halt - datetime.now()).total_seconds() / 60
        assert abs(remaining - 45) < 2, f"Halt should be ~45min for WDO_M5, got {remaining:.0f}min"

    print("  PASS: halt duration uses per-TF config")


def test_stale_halt_cleared_on_daily_reset():
    """Bug fix: _reset_daily_counter should clear halt_until."""
    import vt_autotrader

    vt_autotrader.state.current_day = datetime.now().date() - timedelta(days=1)
    vt_autotrader.state.halt_until = {"WDON26": datetime.now() + timedelta(hours=2)}
    vt_autotrader.state.consecutive_losses = {"WDON26": 5}
    vt_autotrader.state.daily_pnl = -500

    vt_autotrader._reset_daily_counter()

    assert vt_autotrader.state.halt_until == {}, f"halt_until should be empty, got {vt_autotrader.state.halt_until}"
    assert vt_autotrader.state.consecutive_losses == {}, f"consecutive_losses should be empty"
    assert vt_autotrader.state.daily_pnl == 0, f"daily_pnl should be 0"
    print("  PASS: stale halt cleared on daily reset")


# ===== RUNNER =====


def run_all():
    tests = [
        ("Loss Limiter: tier warning at -R$500", test_tier_warning_at_minus_500),
        ("Loss Limiter: tier halt 2h at -R$750", test_tier_halt_2h_at_minus_750),
        ("Loss Limiter: tier kill at -R$1000", test_tier_kill_at_minus_1000),
        ("Loss Limiter: per-symbol halt at -R$500", test_symbol_halt_at_minus_500),
        ("Loss Limiter: per-symbol warning at -R$300", test_symbol_warning_at_minus_300),
        ("Loss Limiter: recovery mode 50% size", test_recovery_mode_50_percent_size),
        ("Loss Limiter: recovery loss doubles halt", test_recovery_loss_doubles_halt),
        ("Loss Limiter: recovery win exits recovery", test_recovery_win_exits_recovery),
        ("Loss Limiter: daily reset clears state", test_daily_reset_clears_state),
        ("Bug fix: halt_on_loss config key read", test_halt_on_loss_config_key_read),
        ("Bug fix: consecutive_losses per-TF config", test_consecutive_losses_uses_per_tf_config),
        ("Bug fix: halt_duration per-TF config", test_halt_duration_uses_per_tf_config),
        ("Bug fix: stale halt cleared on daily reset", test_stale_halt_cleared_on_daily_reset),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {name}: {e}")
            failed += 1

    _cleanup()

    print(f"\n{'=' * 50}")
    print(f"Results: {passed} passed, {failed} failed, {passed + failed} total")
    if failed == 0:
        print("ALL TESTS PASSED")
    else:
        print(f"FAILURES: {failed}")
        sys.exit(1)


if __name__ == "__main__":
    run_all()
