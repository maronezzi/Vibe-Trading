"""
Testes do dashboard CLI (Fase 4.3 — opcional).

Valida:
  1. render() produz string formatada com bordas + seções
  2. render() não crasha com MT5 indisponível (fallback '--')
  3. _next_crons() parseia scripts do crontab.txt
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from dashboard import render, _next_crons, _mt5_snapshot, _autotrader_status


class TestRender:
    def test_render_produces_box(self):
        """render() retorna string com bordas e título."""
        out = render()
        assert "VIBE-TRADING DASHBOARD" in out
        assert "┌" in out and "└" in out
        assert "MT5" in out
        assert "Autotrader" in out

    def test_render_does_not_crash_without_mt5(self):
        """MT5 indisponível → render mostra '--' / 'unavailable', não crasha."""
        with patch("dashboard._mt5_snapshot", return_value={}):
            out = render()
        # não levanta, mostra algo
        assert "MT5" in out

    def test_render_shows_pnl(self):
        """Com MT5 disponível, render mostra PnL."""
        snap = {"positions": 1, "balance": 1000000.0, "equity": 1000005.0,
                "pnl_today": 263.08, "trade_allowed": True}
        with patch("dashboard._mt5_snapshot", return_value=snap):
            out = render()
        assert "263.08" in out or "+263" in out


class TestNextCrons:
    def test_parse_crontab_scripts(self):
        """_next_crons extrai nomes de .py/.sh do crontab.txt."""
        crons = _next_crons()
        # crontab tem pelo menos alguns scripts conhecidos
        assert isinstance(crons, list)
        # vt_self_heal.py e check_symbols_active.py (adicionados Fase 2)
        if Path(PROJECT_ROOT / "crontab.txt").exists():
            assert len(crons) > 0


class TestResilience:
    def test_autotrader_status_handles_no_pid(self):
        """Sem autotrader rodando → pid=None, não crasha."""
        with patch("subprocess.run",
                   return_value=type("R", (), {"stdout": ""})()):
            st = _autotrader_status()
        assert st.get("pid") in (None, "")
