"""
test_day_trade_intent.py — Wave N+5A (2026-07-08)

Valida _is_day_trade_flatten_window do autotrader.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "core")):
    if p not in sys.path:
        sys.path.insert(0, p)


def test_within_window_returns_true(monkeypatch):
    """Faltam 10min pro EOD, buffer=15min → dentro da janela → flatten."""
    from core import vt_autotrader
    monkeypatch.setattr(vt_autotrader, "CONFIG", {
        "close_hour": 16,
        "close_minute": 45,
        "day_trade_intent": {"WIN_M5": True},
    })
    # now = 16:35 (10min antes do EOD)
    now = datetime(2026, 7, 8, 16, 35)
    assert vt_autotrader._is_day_trade_flatten_window(
        "WINQ26", "M5", pos_minutes=10, buffer_minutes=15, now=now,
    ) is True


def test_outside_window_returns_false(monkeypatch):
    """Faltam 30min pro EOD, buffer=15min → fora da janela → não flatten."""
    from core import vt_autotrader
    monkeypatch.setattr(vt_autotrader, "CONFIG", {
        "close_hour": 16,
        "close_minute": 45,
        "day_trade_intent": {"WIN_M5": True},
    })
    now = datetime(2026, 7, 8, 16, 15)  # 30min antes
    assert vt_autotrader._is_day_trade_flatten_window(
        "WINQ26", "M5", pos_minutes=5, buffer_minutes=15, now=now,
    ) is False


def test_swing_intent_returns_false(monkeypatch):
    """day_trade_intent[WIN_H1] = False → swing permite ficar overnight."""
    from core import vt_autotrader
    monkeypatch.setattr(vt_autotrader, "CONFIG", {
        "close_hour": 16,
        "close_minute": 45,
        "day_trade_intent": {"WIN_H1": False},  # swing
    })
    now = datetime(2026, 7, 8, 16, 35)  # 10min antes
    assert vt_autotrader._is_day_trade_flatten_window(
        "WINQ26", "H1", pos_minutes=120, buffer_minutes=15, now=now,
    ) is False


def test_default_intent_is_day_trade(monkeypatch):
    """Sem day_trade_intent no config → default True (day-trade)."""
    from core import vt_autotrader
    monkeypatch.setattr(vt_autotrader, "CONFIG", {
        "close_hour": 16,
        "close_minute": 45,
        # sem day_trade_intent
    })
    now = datetime(2026, 7, 8, 16, 35)
    assert vt_autotrader._is_day_trade_flatten_window(
        "WINQ26", "M5", pos_minutes=2, buffer_minutes=15, now=now,
    ) is True


def test_buffer_size_is_configurable(monkeypatch):
    """Buffer menor = janela mais apertada."""
    from core import vt_autotrader
    monkeypatch.setattr(vt_autotrader, "CONFIG", {
        "close_hour": 16,
        "close_minute": 45,
        "day_trade_intent": {"WIN_M5": True},
    })
    now = datetime(2026, 7, 8, 16, 35)  # 10min antes
    # buffer=5: faltam 10min > 5min → NÃO flatten
    assert vt_autotrader._is_day_trade_flatten_window(
        "WINQ26", "M5", pos_minutes=2, buffer_minutes=5, now=now,
    ) is False
    # buffer=20: faltam 10min < 20min → SIM flatten
    assert vt_autotrader._is_day_trade_flatten_window(
        "WINQ26", "M5", pos_minutes=2, buffer_minutes=20, now=now,
    ) is True
