"""
test_calendar_blackout.py — Wave N+4A (2026-07-08)

Valida core/vt_calendar.aggregate_blackout(): refator que unifica
``is_trading_day``, ``_is_blocked_day_direction``, ``_is_blocked_time``,
e news events num único gate (Wave §7.1).

Casos cobertos:
  1. Dia de feriado (B3_HOLIDAYS) bloqueia BUY+SELL.
  2. blocked_day_directions bloqueia BUY em quarta.
  3. time_blocks bloqueia WIN às 10h.
  4. Events (news) — sem evento: sem efeito; com evento: bloqueia
     symbol+side.
  5. Compound reason: combina várias restrições.
  6. Sem schedule definitions → retorna False (não bloqueia).
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "core"),
          str(PROJECT_ROOT / "mt5")):
    if p not in sys.path:
        sys.path.insert(0, p)

from core import vt_calendar  # noqa: E402


# ═══════════════════════════════════════════════════════════
# aggregate_blackout
# ═══════════════════════════════════════════════════════════

def test_aggregate_returns_false_when_no_constraints(monkeypatch):
    """Sem CONFIG, sem time_blocks, sem day_dir, sem events → não bloqueia."""
    # Default config sem nenhuma chave restritiva
    cfg = {"time_blocks": {}, "blocked_day_directions": [],
           "events": []}
    is_blocked, reason = vt_calendar.aggregate_blackout(
        "WDO", "BUY", config=cfg, ts=None,
    )
    assert is_blocked is False
    assert reason == ""


def test_time_block_blocks_when_in_window(monkeypatch):
    """time_blocks WINQ26 9-11h bloqueia BUY dentro do range."""
    cfg = {
        "time_blocks": {
            "WIN": [
                {"start": 9, "end": 11, "strategy": None, "reason": "morning volatility"},
            ],
        },
        "blocked_day_directions": [],
        "events": [],
    }
    # ts = 2026-07-08 10:30:00 (quarta-feira, hora 10)
    ts = datetime(2026, 7, 8, 10, 30)
    is_blocked, reason = vt_calendar.aggregate_blackout(
        "WINQ26", "BUY", config=cfg, ts=ts,
    )
    assert is_blocked is True
    assert "time_block" in reason.lower() or "morning" in reason.lower()


def test_time_block_allows_when_outside_window(monkeypatch):
    cfg = {
        "time_blocks": {
            "WIN": [{"start": 9, "end": 11}],
        },
        "blocked_day_directions": [],
        "events": [],
    }
    ts = datetime(2026, 7, 8, 14, 30)  # 14h
    is_blocked, _ = vt_calendar.aggregate_blackout(
        "WINQ26", "BUY", config=cfg, ts=ts,
    )
    assert is_blocked is False


def test_blocked_day_direction_blocks_one_side(monkeypatch):
    """blocked_day_directions: [2, 'BUY'] bloqueia BUY em quarta."""
    cfg = {
        "blocked_day_directions": [[2, "BUY"]],  # 2 = quarta
        "time_blocks": {},
        "events": [],
    }
    # 2026-07-08 é quarta-feira (weekday=2 — yes Python convention is 0=Mon)
    ts = datetime(2026, 7, 8, 10, 0)
    is_blocked, reason = vt_calendar.aggregate_blackout(
        "WDO", "BUY", config=cfg, ts=ts,
    )
    assert is_blocked is True
    assert "day_dir" in reason.lower()


def test_blocked_day_direction_does_not_block_opposite_side(monkeypatch):
    cfg = {
        "blocked_day_directions": [[2, "BUY"]],
        "time_blocks": {},
        "events": [],
    }
    ts = datetime(2026, 7, 8, 10, 0)  # quarta
    # SELL em quarta: liberado
    is_blocked, _ = vt_calendar.aggregate_blackout(
        "WDO", "SELL", config=cfg, ts=ts,
    )
    assert is_blocked is False


def test_events_block_symbol_in_window(monkeypatch):
    """Evento (news) ±30min bloqueia symbol+side_match."""
    cfg = {
        "events": [
            {
                "ts": "2026-07-08T10:30:00-03:00",
                "symbol": "WDO",
                "side": "BUY",
                "window_min": 30,
                "severity": "HIGH",
                "source": "IPCA",
            },
        ],
        "time_blocks": {},
        "blocked_day_directions": [],
    }
    # ts 10:30: exatamente no evento → bloqueia.
    ts = datetime.fromisoformat("2026-07-08T10:30:00-03:00")
    is_blocked, reason = vt_calendar.aggregate_blackout(
        "WDON26", "BUY", config=cfg, ts=ts,
    )
    assert is_blocked is True
    assert "event" in reason.lower() or "ipca" in reason.lower()


def test_events_do_not_block_opposite_side(monkeypatch):
    cfg = {
        "events": [
            {
                "ts": "2026-07-08T10:30:00-03:00",
                "symbol": "WDO",
                "side": "BUY",
                "window_min": 30,
                "severity": "HIGH",
            },
        ],
        "time_blocks": {},
        "blocked_day_directions": [],
    }
    ts = datetime.fromisoformat("2026-07-08T10:30:00-03:00")
    # SELL em horário de evento BUY-only: liberado
    is_blocked, _ = vt_calendar.aggregate_blackout(
        "WDON26", "SELL", config=cfg, ts=ts,
    )
    assert is_blocked is False


def test_events_do_not_block_outside_window(monkeypatch):
    cfg = {
        "events": [
            {
                "ts": "2026-07-08T10:30:00-03:00",
                "symbol": "WDO",
                "window_min": 30,
            },
        ],
        "time_blocks": {},
        "blocked_day_directions": [],
    }
    # ts 12:00: 90min depois do evento → fora da janela
    ts = datetime(2026, 7, 8, 12, 0)
    is_blocked, _ = vt_calendar.aggregate_blackout(
        "WDON26", "BUY", config=cfg, ts=ts,
    )
    assert is_blocked is False


def test_compound_reason_lists_all_constraints(monkeypatch):
    """Múltiplas restrições ativas → reason contém todas (joined por ;)."""
    cfg = {
        "time_blocks": {"WDO": [{"start": 9, "end": 11, "reason": "spike"}]},
        "blocked_day_directions": [[2, "BUY"]],
        "events": [],
    }
    ts = datetime(2026, 7, 8, 10, 0)  # quarta 10h — ambos os gates ativos
    is_blocked, reason = vt_calendar.aggregate_blackout(
        "WDON26", "BUY", config=cfg, ts=ts,
    )
    assert is_blocked is True
    assert ";" in reason or "," in reason  # compound delimiter


def test_trading_day_holiday_blocks_all(monkeypatch):
    """B3_HOLIDAYS date → is_trading_day() = False, aggregate bloqueia BUY+SELL."""
    # 2026-09-07 é independência (não está em B3_HOLIDAYS — vou usar Christmas)
    cfg = {"time_blocks": {}, "blocked_day_directions": [], "events": []}
    ts = datetime(2026, 12, 25, 11, 0)  # Natal
    is_blocked, reason = vt_calendar.aggregate_blackout(
        "WIN", "BUY", config=cfg, ts=ts,
    )
    assert is_blocked is True
    assert "holiday" in reason.lower() or "trading_day" in reason.lower()


def test_aggregate_blackout_exposes_root_only(monkeypatch):
    """Aggregate respeita symbol_root (WIN, WDO) — não match em string cheia."""
    cfg = {
        "time_blocks": {"WIN": [{"start": 10, "end": 11}]},
        "blocked_day_directions": [],
        "events": [],
    }
    ts = datetime(2026, 7, 8, 10, 30)
    # symbol completo "WINQ26" → root "WIN" → casa com time_blocks["WIN"]
    is_blocked, _ = vt_calendar.aggregate_blackout(
        "WINQ26", "BUY", config=cfg, ts=ts,
    )
    assert is_blocked is True
