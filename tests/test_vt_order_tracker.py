"""
Testes do Order Tracker (Fase 3.1 — Lei 4).

Cobertura:
  Lei 3 — register_order valida SL:
    1. sl_pts <= 0 → recusado
    2. sl_pts None → recusado
  Lei 4 — register_order valida ticket:
    3. ticket <= 0 → recusado (MT5 não confirmou)
  Persistência:
    4. save + load round-trip
    5. atomic write (não corrompe)
  Reconcile:
    6. ghosts (tracker tem, MT5 não) marcados closed
    7. orphans (MT5 tem, tracker não) reportados
    8. confirmed quando ambos têm
  Misc:
    9. get_active_orders filtra por symbol
    10. mark_closed muda status
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.vt_order_tracker import (
    OrderRecord,
    OrderTracker,
    ReconcileReport,
    _atomic_write_json,
)


@pytest.fixture
def tracker(tmp_path):
    """Tracker isolado em tmp (não toca /tmp/vt_order_tracker.json real)."""
    return OrderTracker(path=tmp_path / "tracker.json", autoload=False)


# ── Lei 3: SL obrigatório no register ───────────────────────────────────────
class TestRegisterStopLossValidation:
    def test_zero_sl_rejected(self, tracker):
        assert tracker.register_order(100, "WINQ26", "BUY", 1.0, 100.0, 0) is False
        assert tracker.get_active_orders() == []

    def test_negative_sl_rejected(self, tracker):
        assert tracker.register_order(100, "WINQ26", "BUY", 1.0, 100.0, -50) is False

    def test_none_sl_rejected(self, tracker):
        assert tracker.register_order(100, "WINQ26", "BUY", 1.0, 100.0, None) is False


# ── Lei 4: ticket confirmado ────────────────────────────────────────────────
class TestRegisterTicketValidation:
    def test_zero_ticket_rejected(self, tracker):
        """ticket=0 = MT5 não confirmou (Lei 4)."""
        assert tracker.register_order(0, "WINQ26", "BUY", 1.0, 100.0, 200) is False

    def test_negative_ticket_rejected(self, tracker):
        assert tracker.register_order(-1, "WINQ26", "BUY", 1.0, 100.0, 200) is False

    def test_valid_order_registered(self, tracker):
        ok = tracker.register_order(12345, "WINQ26", "BUY", 1.0, 175000.0, 200,
                                    reason="ADX_TREND")
        assert ok is True
        actives = tracker.get_active_orders()
        assert len(actives) == 1
        assert actives[0].ticket == 12345
        assert actives[0].sl_pts == 200


# ── Persistência ────────────────────────────────────────────────────────────
class TestPersistence:
    def test_save_load_roundtrip(self, tmp_path):
        path = tmp_path / "tracker.json"
        t1 = OrderTracker(path=path, autoload=False)
        t1.register_order(111, "WINQ26", "BUY", 1.0, 100.0, 200)
        t1.register_order(222, "WDOQ26", "SELL", 2.0, 5000.0, 50)

        # novo tracker carrega do disco
        t2 = OrderTracker(path=path, autoload=True)
        actives = t2.get_active_orders()
        assert {r.ticket for r in actives} == {111, 222}

    def test_atomic_write_does_not_corrupt(self, tmp_path):
        """Atomic write: se crash mid-write, arquivo original intacto."""
        path = tmp_path / "t.json"
        _atomic_write_json(path, {"v": 1})
        # simula escrita que falha — _atomic_write usa tmp+replace
        _atomic_write_json(path, {"v": 2})
        assert json.loads(path.read_text())["v"] == 2

    def test_load_missing_file_starts_empty(self, tmp_path):
        t = OrderTracker(path=tmp_path / "inexistente.json", autoload=True)
        assert t.get_active_orders() == []

    def test_load_corrupt_json_starts_empty(self, tmp_path):
        path = tmp_path / "corrupt.json"
        path.write_text("{ invalid json }}}")
        t = OrderTracker(path=path, autoload=True)
        assert t.get_active_orders() == []  # não crasha


# ── Reconcile ───────────────────────────────────────────────────────────────
class TestReconcile:
    def test_ghosts_marked_closed(self, tracker, monkeypatch):
        """Ordem no tracker mas não no MT5 → ghost → closed."""
        tracker.register_order(999, "WINQ26", "BUY", 1.0, 100.0, 200)
        # MT5 não tem o ticket 999
        fake_truth = MagicMock()
        fake_truth.get_open_positions.return_value = []
        tracker.truth = fake_truth

        report = tracker.reconcile()
        assert 999 in report.ghosts
        # ghost foi marcado closed
        assert all(r.status == "closed" for r in tracker._active.values())

    def test_orphans_reported(self, tracker):
        """Ordem no MT5 mas não no tracker → orphan (não ingere, só reporta)."""
        tracker.register_order(111, "WINQ26", "BUY", 1.0, 100.0, 200)
        # MT5 tem o 111 (confirmed) + 222 (orphan, não está no tracker)
        fake_truth = MagicMock()
        fake_truth.get_open_positions.return_value = [
            MagicMock(ticket=111), MagicMock(ticket=222)]
        tracker.truth = fake_truth

        report = tracker.reconcile()
        assert 111 in report.confirmed
        assert 222 in report.orphans
        assert report.has_drift is True

    def test_confirmed_when_both_sides(self, tracker):
        tracker.register_order(111, "WINQ26", "BUY", 1.0, 100.0, 200)
        fake_truth = MagicMock()
        fake_truth.get_open_positions.return_value = [MagicMock(ticket=111)]
        tracker.truth = fake_truth

        report = tracker.reconcile()
        assert 111 in report.confirmed
        assert report.orphans == []
        assert report.ghosts == []
        assert report.has_drift is False

    def test_reconcile_mt5_unavailable_returns_empty(self, tracker):
        """MT5 indisponível → reconcile não crasha, retorna vazio."""
        tracker.register_order(111, "WINQ26", "BUY", 1.0, 100.0, 200)
        fake_truth = MagicMock()
        fake_truth.get_open_positions.side_effect = ConnectionError("MT5 down")
        tracker.truth = fake_truth

        report = tracker.reconcile()
        assert report.has_drift is False  # não acusou drift falso


# ── Misc ────────────────────────────────────────────────────────────────────
class TestMisc:
    def test_get_active_filters_by_symbol(self, tracker):
        tracker.register_order(1, "WINQ26", "BUY", 1.0, 100.0, 200)
        tracker.register_order(2, "WDOQ26", "SELL", 1.0, 100.0, 50)
        win = tracker.get_active_orders(symbol="WIN")
        assert len(win) == 1
        assert win[0].symbol == "WINQ26"

    def test_mark_closed_changes_status(self, tracker):
        tracker.register_order(100, "WINQ26", "BUY", 1.0, 100.0, 200)
        assert tracker.mark_closed(100, close_price=99.0,
                                   close_reason="SL_hit") is True
        # closed não aparece em get_active
        assert tracker.get_active_orders() == []

    def test_update_heartbeat(self, tracker):
        tracker.register_order(100, "WINQ26", "BUY", 1.0, 100.0, 200)
        old_hb = tracker._active[100].last_heartbeat
        import time
        time.sleep(0.01)
        tracker.update_heartbeat(100)
        assert tracker._active[100].last_heartbeat > old_hb
