"""
Testes do VtLogger (Fase 4.1).

Cobertura:
  1. 4 níveis formatam corretamente [LEVEL] [SUBSYSTEM] [EVENT] detalhe
  2. Agregação de WARN conta ocorrências na janela
  3. ERROR envia Telegram (mockado)
  4. CRITICAL chama auto-heal hook antes de Telegram
  5. Rate-limit Telegram (1/min ERROR)
  6. Retro-compat: detalhes vazios não quebram formato
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from monitoring.vt_logger import VtLogger, _format_details


class TestFormatting:
    def test_info_format(self, caplog):
        log = VtLogger("test1", telegram_enabled=False)
        with caplog.at_level("INFO"):
            line = log.info("TRADE", "entry", symbol="WINQ26", ticket=12345)
        assert "[INFO" in line
        assert "[TRADE]" in line
        assert "[entry]" in line
        assert "symbol=WINQ26" in line
        assert "ticket=12345" in line

    def test_all_four_levels(self):
        log = VtLogger("test2", telegram_enabled=False)
        for method, level in [("info", "INFO"), ("warn", "WARN"),
                              ("error", "ERROR"), ("critical", "CRITICAL")]:
            line = getattr(log, method)("SYS", "evt", x=1)
            assert f"[{level}" in line

    def test_empty_details_ok(self):
        """Detalhes vazios não quebram o formato."""
        log = VtLogger("test3", telegram_enabled=False)
        line = log.info("STATE", "rebuild")
        assert "[STATE]" in line
        assert "[rebuild]" in line

    def test_float_formatted_2dp(self):
        assert "drift=263.00" in _format_details({"drift": 263.0})


class TestWarnAggregation:
    def test_aggregation_counts_repeats(self):
        """Múltiplos WARNs iguais em 1min viram linhas com _aggregated_count."""
        log = VtLogger("agg", telegram_enabled=False)
        lines = []
        for _ in range(5):
            lines.append(log.warn("DRIFT", "high", value=10))
        # A partir da 2ª, deve ter _aggregated_count
        assert any("_aggregated_count=5" in l for l in lines) or \
               any("_aggregated_count" in l for l in lines[1:])


class TestTelegram:
    def test_error_sends_telegram(self):
        log = VtLogger("tel1", telegram_enabled=True)
        with patch("core.vt_hermes_helper.hermes_send", return_value=True) as hs:
            log.error("MT5", "offline", ping=5000)
        hs.assert_called_once()

    def test_info_never_sends_telegram(self):
        log = VtLogger("tel2", telegram_enabled=True)
        with patch("core.vt_hermes_helper.hermes_send") as hs:
            log.info("TRADE", "entry", ticket=1)
        hs.assert_not_called()

    def test_rate_limit_error(self):
        """2 ERRORs iguais em <1min → só 1 Telegram (rate-limit)."""
        log = VtLogger("tel3", telegram_enabled=True)
        with patch("core.vt_hermes_helper.hermes_send", return_value=True) as hs:
            log.error("X", "y", v=1)
            log.error("X", "y", v=2)  # mesmo key, dentro do cooldown
        hs.assert_called_once()  # rate-limited


class TestCriticalAutoHeal:
    def test_critical_calls_auto_heal_before_telegram(self):
        """CRITICAL tenta auto-heal. Se heal OK, NÃO envia Telegram (reduz spam).

        Princípio handoff: 'auto-heal primeiro, Telegram depois — só se falhar'.
        """
        heal_fn = MagicMock(return_value=True)  # heal bem-sucedido
        log = VtLogger("crit", telegram_enabled=True, auto_heal_fn=heal_fn)
        with patch("core.vt_hermes_helper.hermes_send") as hs:
            log.critical("AUTOTRADER", "crashed", pid=123)
        heal_fn.assert_called_once()
        # heal OK → Bruno NÃO é notificado (só se falhar)
        hs.assert_not_called()

    def test_critical_sends_telegram_when_heal_fails(self):
        """auto-heal falha → Telegram enviado (Bruno precisa intervir)."""
        heal_fn = MagicMock(return_value=False)  # heal falhou
        log = VtLogger("crit_fail", telegram_enabled=True, auto_heal_fn=heal_fn)
        with patch("core.vt_hermes_helper.hermes_send", return_value=True) as hs:
            log.critical("MT5", "down", ping=9999)
        hs.assert_called_once()

    def test_auto_heal_exception_does_not_crash(self):
        """auto-heal que levanta não derruba o logger."""
        def bad_heal(s, e, d):
            raise RuntimeError("heal broken")
        log = VtLogger("crit2", telegram_enabled=False, auto_heal_fn=bad_heal)
        # não deve levantar
        line = log.critical("X", "y", v=1)
        assert "[CRITICAL" in line
