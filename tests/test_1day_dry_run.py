"""
Testes adversariais do dry-run 1 dia (Fase 5.2).

Cada cenário adversarial isolado + validação end-to-end do run_dry_run().
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from vt_validate_1day import (
    MockEnvironment,
    SCENARIOS,
    run_dry_run,
    render_markdown,
)


@pytest.fixture
def env():
    return MockEnvironment()


# ── Cenários adversariais isolados (10) ─────────────────────────────────────
class TestScenarios:
    def test_mt5_ping_timeout_recovers(self, env):
        from vt_validate_1day import scenario_mt5_ping_timeout
        assert scenario_mt5_ping_timeout(env) is True

    def test_db_locked_unlocks(self, env):
        from vt_validate_1day import scenario_db_locked
        assert scenario_db_locked(env) is True

    def test_autotrader_crash_rebuilds_state(self, env):
        from vt_validate_1day import scenario_autotrader_crash
        # cria position no MT5, corrompe state, rebuild
        env.open_position("WINQ26", "BUY", 175000, 200)
        env.state_positions = {}  # crash limpou state
        assert scenario_autotrader_crash(env) is True

    def test_mt5_orphan_ingested(self, env):
        from vt_validate_1day import scenario_mt5_orphan
        assert scenario_mt5_orphan(env) is True

    def test_sl_fail_triggers_emergency_close(self, env):
        from vt_validate_1day import scenario_sl_fail_emergency
        assert scenario_sl_fail_emergency(env) is True

    def test_kill_switch_activates(self, env):
        from vt_validate_1day import scenario_kill_switch
        assert scenario_kill_switch(env) is True

    def test_consecutive_loss_halts(self, env):
        from vt_validate_1day import scenario_consecutive_loss
        assert scenario_consecutive_loss(env) is True

    def test_concurrent_orders_blocked(self, env):
        from vt_validate_1day import scenario_concurrent_orders
        assert scenario_concurrent_orders(env) is True

    def test_ghost_trade_pnl_persisted(self, env):
        from vt_validate_1day import scenario_ghost_with_pnl
        assert scenario_ghost_with_pnl(env) is True

    def test_state_corrupt_rebuilds(self, env):
        from vt_validate_1day import scenario_state_corrupt
        env.open_position("WINQ26", "BUY", 175000, 200)
        env.state_positions = {}
        assert scenario_state_corrupt(env) is True


# ── Lei 3: SL obrigatório no mock ───────────────────────────────────────────
class TestLei3InMock:
    def test_open_position_requires_sl(self, env):
        """Lei 3: open_position com sl=0 levanta (nunca deve acontecer)."""
        with pytest.raises(ValueError, match="Lei 3"):
            env.open_position("WINQ26", "BUY", 175000, sl_pts=0)

    def test_sl_coverage_always_100(self, env):
        """Após simulação, SL coverage deve ser 100% (Lei 3)."""
        env.open_position("WINQ26", "BUY", 175000, 200)
        env.open_position("WDOQ26", "SELL", 5000, 50)
        inv = env.check_invariants(10)
        assert inv.sl_coverage_pct == 100.0


# ── Run dry-run end-to-end ──────────────────────────────────────────────────
class TestRunDryRun:
    def test_run_returns_success(self):
        """run_dry_run com seed fixo → sucesso (10/10 cenários, drift 0)."""
        report = run_dry_run(seed=42)
        assert report.success is True
        assert report.exit_code == 0
        assert len(report.scenarios_passed) == 10
        assert len(report.scenarios_failed) == 0

    def test_all_10_scenarios_run(self):
        report = run_dry_run(seed=42)
        assert len(report.scenarios_run) == 10

    def test_invariants_collected_per_hour(self):
        report = run_dry_run(seed=42)
        # 9h às 16h = 8 horas
        assert len(report.invariants_by_hour) == 8

    def test_drift_under_threshold(self):
        report = run_dry_run(seed=42)
        assert report.drift_max < 5.0

    def test_report_has_atesto(self):
        report = run_dry_run(seed=42)
        assert "100%" in report.atesto or "sucesso" in report.atesto.lower()

    def test_markdown_render_includes_sections(self):
        report = run_dry_run(seed=42)
        md = render_markdown(report)
        assert "Resumo" in md
        assert "Cenários de falha" in md
        assert "Invariantes por hora" in md
        assert "Atesto" in md

    def test_dry_run_deterministic_with_seed(self):
        """Mesma seed → mesmo resultado (reprodutível)."""
        r1 = run_dry_run(seed=123)
        r2 = run_dry_run(seed=123)
        assert r1.total_orders == r2.total_orders
        assert r1.exit_code == r2.exit_code
