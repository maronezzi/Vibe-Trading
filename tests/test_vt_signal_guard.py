# -*- coding: utf-8 -*-
"""Tests Wave 883.B4 (29/08/2026) — guarda de sinal expirado (M30/H1).

Hermético: módulo puro core/vt_signal_guard.py — nenhum MT5/DB/config real.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import vt_signal_guard as sg  # noqa: E402


def _bars(bar0_time: float) -> list[dict]:
    """bars[0] = barra em formação com `time` de abertura (epoch servidor)."""
    return [{"time": bar0_time, "open": 100.0, "high": 101.0, "low": 99.0,
             "close": 100.5, "volume": 10}]


def test_h1_fresco_passa():
    # agora 10min após a abertura da barra H1 → 600s <= 0.25×3600=900s
    now = 1_800_000_000.0
    assert sg.signal_age_ok("H1", _bars(now - 600), now=now) is True


def test_h1_tarde_bloqueia():
    # 22min dentro da barra (p50 medido na auditoria) → 1320s > 900s
    now = 1_800_000_000.0
    assert sg.signal_age_ok("H1", _bars(now - 1320), now=now) is False


def test_m30_limite_quarto_de_hora():
    now = 1_800_000_000.0
    assert sg.signal_age_ok("M30", _bars(now - 400), now=now) is True    # <450s
    assert sg.signal_age_ok("M30", _bars(now - 500), now=now) is False   # >450s


def test_m15_m5_fora_do_escopo():
    now = 1_800_000_000.0
    # M15 com 10min decorridos não é bloqueado (latência intra-loop normal)
    assert sg.signal_age_ok("M15", _bars(now - 600), now=now) is True
    assert sg.signal_age_ok("M5", _bars(now - 240), now=now) is True


def test_modulo_3600_elimina_fuso():
    # servidor B3 adiantado 3h: bar0.time carrega offset +10800 — o módulo
    # devolve o decorrido real (600s), não 600+10800
    now = 1_800_000_000.0
    offset = 3 * 3600
    assert sg.elapsed_in_current_bar(_bars(now - 600 + offset), now=now) == 600.0


def test_sem_barra_ou_sem_time_nao_bloqueia():
    assert sg.signal_age_ok("H1", [], now=1.0) is True
    assert sg.signal_age_ok("H1", [{"close": 1.0}], now=1.0) is True


def test_kill_switch_por_env(monkeypatch):
    monkeypatch.setenv("VT_SIGNAL_AGE_GUARD", "0")
    now = 1_800_000_000.0
    assert sg.signal_age_ok("H1", _bars(now - 3000), now=now) is True
