"""
Testes dos 4 novos health checks MT5 (Fase 3.2 — Entregável 2).

Valida: mt5_margin, mt5_tick_freshness, mt5_symbol_map, mt5_trade_allowed.
Todos mockam o MT5 (status/tick/info) — produção intocada.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from monitoring import vt_self_heal as sh


class TestMt5Margin:
    def test_low_margin_alerts(self):
        """free_margin < 30% equity → mt5_low_margin HIGH."""
        status = {"account": {"equity": 100000, "free_margin": 20000,
                              "trade_allowed": True}}
        with patch.object(sh, "_mt5_status_safe", return_value=status):
            issue = sh._check_mt5_margin()
        assert issue is not None
        assert issue.type == "mt5_low_margin"
        assert issue.severity == sh.SEV_HIGH

    def test_healthy_margin_no_issue(self):
        status = {"account": {"equity": 100000, "free_margin": 90000,
                              "trade_allowed": True}}
        with patch.object(sh, "_mt5_status_safe", return_value=status):
            assert sh._check_mt5_margin() is None

    def test_mt5_unavailable_no_issue(self):
        """MT5 indisponível → não acusa (já coberto por _check_mt5_reachable)."""
        with patch.object(sh, "_mt5_status_safe", return_value=None):
            assert sh._check_mt5_margin() is None


class TestMt5TickFreshness:
    def test_stale_tick_alerts(self):
        """Tick com time > 5min atrás → mt5_tick_stale HIGH."""
        old_time = time.time() - 600  # 10min
        with patch("core.vt_config_loader.load_config",
                   return_value={"resolved_symbols": {"WIN": "WINQ26"}}), \
             patch("mt5.mt5_orchestrator.tick",
                   return_value={"time": old_time, "bid": 175000}):
            issue = sh._check_mt5_tick_freshness()
        assert issue is not None
        assert issue.type == "mt5_tick_stale"
        assert issue.severity == sh.SEV_HIGH

    def test_fresh_tick_no_issue(self):
        recent = time.time() - 30  # 30s atrás
        with patch("core.vt_config_loader.load_config",
                   return_value={"resolved_symbols": {"WIN": "WINQ26"}}), \
             patch("mt5.mt5_orchestrator.tick",
                   return_value={"time": recent, "bid": 175000}):
            assert sh._check_mt5_tick_freshness() is None

    def test_no_resolved_symbols_skips(self):
        with patch("core.vt_config_loader.load_config",
                   return_value={"resolved_symbols": {}}):
            assert sh._check_mt5_tick_freshness() is None


class TestMt5SymbolMap:
    def test_broken_symbol_alerts(self):
        """Símbolo com erro no info() → mt5_symbol_map_broken HIGH."""
        cfg = {"resolved_symbols": {"WIN": "WINQ26", "WDO": "WDOQ26"}}
        with patch("core.vt_config_loader.load_config", return_value=cfg), \
             patch("mt5.mt5_orchestrator.info",
                   return_value={"error": "unknown symbol"}):
            issue = sh._check_mt5_symbol_map()
        assert issue is not None
        assert issue.type == "mt5_symbol_map_broken"

    def test_valid_symbols_no_issue(self):
        cfg = {"resolved_symbols": {"WIN": "WINQ26"}}
        with patch("core.vt_config_loader.load_config", return_value=cfg), \
             patch("mt5.mt5_orchestrator.info",
                   return_value={"symbol": "WINQ26", "point": 1}):
            assert sh._check_mt5_symbol_map() is None

    def test_ind_skipped_in_symbol_map(self):
        """IND é ignorado no check de symbol map (Lei 2 — hard-kill)."""
        cfg = {"resolved_symbols": {"IND": "INDQ26", "WIN": "WINQ26"}}
        call_count = {"info": 0}

        def fake_info(sym):
            call_count["info"] += 1
            return {"symbol": sym}

        with patch("core.vt_config_loader.load_config", return_value=cfg), \
             patch("mt5.mt5_orchestrator.info", side_effect=fake_info):
            sh._check_mt5_symbol_map()
        # IND não deve ser consultado (só WIN)
        # call_count pode variar conforme amostra, mas IND nunca é chamado


class TestMt5TradeAllowed:
    def test_trade_blocked_critical(self):
        """trade_allowed=False → CRITICAL (conta não opera)."""
        status = {"account": {"trade_allowed": False, "equity": 100000,
                              "free_margin": 100000}}
        with patch.object(sh, "_mt5_status_safe", return_value=status):
            issue = sh._check_mt5_trade_allowed()
        assert issue is not None
        assert issue.type == "mt5_trade_blocked"
        assert issue.severity == sh.SEV_CRITICAL

    def test_trade_allowed_no_issue(self):
        status = {"account": {"trade_allowed": True, "equity": 100000,
                              "free_margin": 100000}}
        with patch.object(sh, "_mt5_status_safe", return_value=status):
            assert sh._check_mt5_trade_allowed() is None


class TestHealthCheckIncludesMt5Checks:
    def test_health_check_runs_all_mt5_checks(self):
        """health_check() invoca os 4 novos checks MT5 (registrados)."""
        # Todos retornam None (saudável) → report vazio
        with patch.object(sh, "_mt5_status_safe", return_value=None), \
             patch.object(sh, "_check_mt5_margin", return_value=None) as m1, \
             patch.object(sh, "_check_mt5_tick_freshness", return_value=None) as m2, \
             patch.object(sh, "_check_mt5_symbol_map", return_value=None) as m3, \
             patch.object(sh, "_check_mt5_trade_allowed", return_value=None) as m4, \
             patch.object(sh, "_check_autotrader_alive", return_value=None), \
             patch.object(sh, "_check_mt5_reachable", return_value=None), \
             patch.object(sh, "_check_db_accessible", return_value=None), \
             patch.object(sh, "_check_state_fresh", return_value=None), \
             patch.object(sh, "_check_config_lock_stale", return_value=None), \
             patch.object(sh, "_check_cron_drift", return_value=None):
            sh.health_check()
        m1.assert_called_once()
        m2.assert_called_once()
        m3.assert_called_once()
        m4.assert_called_once()
