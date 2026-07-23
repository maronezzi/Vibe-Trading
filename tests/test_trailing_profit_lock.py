"""Testes unitários para core/vt_trailing_profit_lock.py (Wave 1110)."""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.vt_trailing_profit_lock import (
    TrailingAction,
    TrailingDecision,
    _compute_trail_factor,
    get_trailing_state,
    reset_trailing,
    update_trailing,
)

# ─── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    """Isola o state file em tmp_path para cada teste."""
    state_file = tmp_path / "vt_trailing_profit_lock.json"
    monkeypatch.setattr("core.vt_trailing_profit_lock.STATE_PATH", state_file)
    yield state_file


BASE_CONFIG = {
    "trailing_activation_pct": 0.50,
    "trailing_floor_pct": 0.50,
}


# ─── _compute_trail_factor ─────────────────────────────────────────────────

class TestTrailFactor:
    def test_below_activation_returns_floor(self):
        assert _compute_trail_factor(0.3, 0.5) == 0.5
        assert _compute_trail_factor(0.49, 0.5) == 0.5

    def test_at_activation_returns_floor(self):
        assert _compute_trail_factor(0.5, 0.5) == 0.5

    def test_midpoint(self):
        # progress=0.75 → t=(0.75-0.5)/0.5=0.5 → factor=0.5+0.5*0.5=0.75
        assert abs(_compute_trail_factor(0.75, 0.5) - 0.75) < 1e-9

    def test_at_target_returns_1(self):
        assert _compute_trail_factor(1.0, 0.5) == 1.0

    def test_above_target_capped_at_1(self):
        assert _compute_trail_factor(1.5, 0.5) == 1.0


# ─── update_trailing ───────────────────────────────────────────────────────

class TestUpdateTrailing:
    def test_below_activation_hold(self):
        d = update_trailing(50.0, 250.0, BASE_CONFIG)
        assert d.action == TrailingAction.HOLD

    def test_activation_tighten(self):
        d = update_trailing(125.0, 250.0, BASE_CONFIG)
        assert d.action == TrailingAction.TIGHTEN
        assert d.peak == 125.0
        assert d.floor == 62.5  # 125 * 0.5

    def test_peak_ratchets_up(self):
        update_trailing(125.0, 250.0, BASE_CONFIG)
        d = update_trailing(180.0, 250.0, BASE_CONFIG)
        assert d.action == TrailingAction.TIGHTEN
        assert d.peak == 180.0
        # progress=180/250=0.72, factor=0.5+(0.72-0.5)/0.5*0.5=0.72
        assert d.floor == pytest.approx(180.0 * 0.72, abs=0.01)

    def test_peak_does_not_decrease(self):
        update_trailing(180.0, 250.0, BASE_CONFIG)
        d = update_trailing(150.0, 250.0, BASE_CONFIG)
        assert d.peak == 180.0  # ratchet: não desce

    def test_breach_when_pnl_below_floor(self):
        update_trailing(180.0, 250.0, BASE_CONFIG)
        # floor ≈ 180*0.72 = 129.6
        d = update_trailing(120.0, 250.0, BASE_CONFIG)
        assert d.action == TrailingAction.BREACH
        assert d.pnl == 120.0
        assert d.floor > 120.0

    def test_no_breach_above_floor(self):
        update_trailing(180.0, 250.0, BASE_CONFIG)
        # floor ≈ 129.6, pnl=140 > floor
        d = update_trailing(140.0, 250.0, BASE_CONFIG)
        assert d.action != TrailingAction.BREACH

    def test_target_delegates_to_profit_lock(self):
        d = update_trailing(260.0, 250.0, BASE_CONFIG)
        assert d.action == TrailingAction.TARGET

    def test_target_zero_safe(self):
        d = update_trailing(100.0, 0.0, BASE_CONFIG)
        assert d.action == TrailingAction.HOLD

    def test_negative_pnl_hold(self):
        d = update_trailing(-50.0, 250.0, BASE_CONFIG)
        assert d.action == TrailingAction.HOLD


# ─── State persistence ─────────────────────────────────────────────────────

class TestStatePersistence:
    def test_state_written_on_activation(self, isolate_state):
        update_trailing(130.0, 250.0, BASE_CONFIG)
        state = get_trailing_state()
        assert state["activated"] is True
        assert state["peak"] == 130.0
        # progress=130/250=0.52, factor=0.5+(0.52-0.5)/0.5*0.5=0.52
        # floor=130*0.52=67.6
        assert state["floor"] == pytest.approx(67.6, abs=0.01)

    def test_state_survives_reload(self, isolate_state):
        update_trailing(130.0, 250.0, BASE_CONFIG)
        # Simula restart: lê state de novo
        d = update_trailing(140.0, 250.0, BASE_CONFIG)
        assert d.peak == 140.0  # ratchet up

    def test_reset_clears_state(self, isolate_state):
        update_trailing(130.0, 250.0, BASE_CONFIG)
        reset_trailing()
        assert get_trailing_state() == {}

    def test_day_rollover_expires(self, isolate_state):
        update_trailing(130.0, 250.0, BASE_CONFIG)
        # Simula dia anterior
        state = json.loads(isolate_state.read_text())
        state["date"] = "2020-01-01"
        isolate_state.write_text(json.dumps(state))
        assert get_trailing_state() == {}


# ─── Cenário real 23/07 ────────────────────────────────────────────────────

class TestRealScenario2307:
    """Simula o dia 23/07: pico ~R$183, target R$250, mercado devolveu tudo."""

    def test_scenario_would_have_protected(self):
        target = 250.0
        # Sobe gradualmente
        for pnl in [50, 80, 100, 125, 150, 170, 183]:
            d = update_trailing(float(pnl), target, BASE_CONFIG)

        # No pico R$183: progress=0.732, factor≈0.732, floor≈134
        assert d.peak == 183.0
        assert d.floor > 130.0  # floor protege ~R$134

        # Mercado devolve: cai para R$130 (abaixo do floor ~134)
        d = update_trailing(130.0, target, BASE_CONFIG)
        assert d.action == TrailingAction.BREACH
        # Teria fechado com ~R$130 de lucro em vez de -R$27

    def test_scenario_continues_up_to_target(self):
        target = 250.0
        for pnl in [125, 150, 180, 200, 230, 250]:
            d = update_trailing(float(pnl), target, BASE_CONFIG)

        assert d.action == TrailingAction.TARGET
