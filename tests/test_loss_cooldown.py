"""
test_loss_cooldown.py — Wave N+4B (2026-07-08)

Valida _is_loss_cooldown_active do autotrader (refator em torno de
state.last_loss_direction_per_symbol + state.consecutive_loss_direction_count).

Casos cobertos:
  1. enabled=False: nunca bloqueia.
  2. enabled=True mas count < max_consecutive: não bloqueia.
  3. enabled=True + count >= max + elapsed < window: BLOQUEIA.
  4. elapsed >= window: limpa contador, libera.
  5. direction diferente: contadores independentes.
  6. Escopo symbol: contadores per-symbol, não global.
  7. Config inválido (enabled ausente) → default True.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "core"),
          str(PROJECT_ROOT / "mt5")):
    if p not in sys.path:
        sys.path.insert(0, p)


def _fake_state():
    class S:
        last_loss_direction_per_symbol = {}
        consecutive_loss_direction_count = {}
        halt_until = {}
    return S()


def test_disabled_never_blocks(monkeypatch):
    from core import vt_autotrader
    monkeypatch.setattr(vt_autotrader, "CONFIG", {
        "loss_cooldown": {"enabled": False}
    })
    monkeypatch.setattr(vt_autotrader, "state", _fake_state())
    assert vt_autotrader._is_loss_cooldown_active("WINQ26", "BUY") is False


def test_below_threshold_does_not_block(monkeypatch):
    from core import vt_autotrader
    monkeypatch.setattr(vt_autotrader, "CONFIG", {
        "loss_cooldown": {"enabled": True, "max_consecutive": 3}
    })
    fake_s = _fake_state()
    fake_s.consecutive_loss_direction_count["WINQ26_BUY"] = 1
    monkeypatch.setattr(vt_autotrader, "state", fake_s)
    assert vt_autotrader._is_loss_cooldown_active("WINQ26", "BUY") is False


def test_active_block_when_within_window(monkeypatch):
    from core import vt_autotrader
    monkeypatch.setattr(vt_autotrader, "CONFIG", {
        "loss_cooldown": {"enabled": True, "max_consecutive": 2,
                          "cooldown_minutes": 30}
    })
    fake_s = _fake_state()
    fake_s.consecutive_loss_direction_count["WINQ26_BUY"] = 2
    fake_s.last_loss_direction_per_symbol["WINQ26_BUY"] = (
        datetime.now() - timedelta(minutes=5)
    )
    monkeypatch.setattr(vt_autotrader, "state", fake_s)
    assert vt_autotrader._is_loss_cooldown_active("WINQ26", "BUY") is True


def test_block_releases_after_window(monkeypatch):
    from core import vt_autotrader
    monkeypatch.setattr(vt_autotrader, "CONFIG", {
        "loss_cooldown": {"enabled": True, "max_consecutive": 2,
                          "cooldown_minutes": 30}
    })
    fake_s = _fake_state()
    fake_s.consecutive_loss_direction_count["WINQ26_BUY"] = 2
    fake_s.last_loss_direction_per_symbol["WINQ26_BUY"] = (
        datetime.now() - timedelta(minutes=60)
    )
    monkeypatch.setattr(vt_autotrader, "state", fake_s)
    # elapsed 60min > window 30min → libera
    assert vt_autotrader._is_loss_cooldown_active("WINQ26", "BUY") is False
    # E limpou o contador (próxima call também libera sem setar nada).
    assert fake_s.consecutive_loss_direction_count["WINQ26_BUY"] == 0


def test_different_directions_independent(monkeypatch):
    from core import vt_autotrader
    monkeypatch.setattr(vt_autotrader, "CONFIG", {
        "loss_cooldown": {"enabled": True, "max_consecutive": 2,
                          "cooldown_minutes": 30}
    })
    fake_s = _fake_state()
    fake_s.consecutive_loss_direction_count["WINQ26_BUY"] = 5
    fake_s.last_loss_direction_per_symbol["WINQ26_BUY"] = (
        datetime.now() - timedelta(minutes=5)
    )
    monkeypatch.setattr(vt_autotrader, "state", fake_s)
    # BUY bloqueado, SELL livre.
    assert vt_autotrader._is_loss_cooldown_active("WINQ26", "BUY") is True
    assert vt_autotrader._is_loss_cooldown_active("WINQ26", "SELL") is False


def test_different_symbols_independent(monkeypatch):
    from core import vt_autotrader
    monkeypatch.setattr(vt_autotrader, "CONFIG", {
        "loss_cooldown": {"enabled": True, "max_consecutive": 2,
                          "cooldown_minutes": 30}
    })
    fake_s = _fake_state()
    fake_s.consecutive_loss_direction_count["WINQ26_BUY"] = 2
    fake_s.last_loss_direction_per_symbol["WINQ26_BUY"] = (
        datetime.now() - timedelta(minutes=5)
    )
    monkeypatch.setattr(vt_autotrader, "state", fake_s)
    assert vt_autotrader._is_loss_cooldown_active("WINQ26", "BUY") is True
    assert vt_autotrader._is_loss_cooldown_active("WDON26", "BUY") is False


def test_no_config_defaults_to_enabled(monkeypatch):
    """Sem loss_cooldown block no CONFIG → default enabled=True."""
    from core import vt_autotrader
    monkeypatch.setattr(vt_autotrader, "CONFIG", {})
    fake_s = _fake_state()
    fake_s.consecutive_loss_direction_count["WINQ26_BUY"] = 2
    fake_s.last_loss_direction_per_symbol["WINQ26_BUY"] = (
        datetime.now() - timedelta(minutes=5)
    )
    monkeypatch.setattr(vt_autotrader, "state", fake_s)
    assert vt_autotrader._is_loss_cooldown_active("WINQ26", "BUY") is True


def test_zero_recent_losses_does_not_block(monkeypatch):
    """Sem losses registrados (count=0) → nunca bloqueia."""
    from core import vt_autotrader
    monkeypatch.setattr(vt_autotrader, "CONFIG", {
        "loss_cooldown": {"enabled": True}
    })
    fake_s = _fake_state()
    monkeypatch.setattr(vt_autotrader, "state", fake_s)
    assert vt_autotrader._is_loss_cooldown_active("WINQ26", "BUY") is False
