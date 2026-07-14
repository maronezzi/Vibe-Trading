"""
test_self_heal_trading_guard.py
===============================
Testa que o gate de dia útil + horário em ``_check_autotrader_alive`` funciona.

O self-heal NÃO deve reportar ``autotrader_dead`` quando:
- Não é dia útil (fim de semana / feriado B3).
- É dia útil mas está fora do horário 09:05-16:50.

Deve continuar reportando ``autotrader_dead`` quando dentro de horário útil
E o pgrep retorna vazio (processo realmente morto).
"""
import sys
import os
import unittest
from datetime import datetime
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


class TestSelfHealTradingGuard(unittest.TestCase):
    """Garante que o self-heal respeita dia útil e horário de trading."""

    def _check(self, is_trading_day_ok, now_hour, now_minute):
        """Chama _check_autotrader_alive com datetime e is_trading_day mockados.

        Args:
            is_trading_day_ok: valor que is_trading_day() retorna (bool).
            now_hour, now_minute: hora/minuto simulados para datetime.now().
        """
        mock_now = datetime(2026, 7, 13, now_hour, now_minute)  # segunda-feira

        # Mock datetime.now dentro do modulo self_heal
        # Mock is_trading_day para retornar o valor desejado
        # Mock subprocess.run para simular pgrep vazio (processo morto)
        with patch("monitoring.vt_self_heal.datetime") as mock_dt, \
             patch("core.vt_calendar.is_trading_day",
                   return_value=(is_trading_day_ok, "motivo_mock")), \
             patch("monitoring.vt_self_heal.subprocess.run") as mock_run:
            mock_dt.now.return_value = mock_now
            # pgrep retorna vazio = processo morto
            mock_run.return_value = MagicMock(stdout="", stderr="")

            from monitoring.vt_self_heal import _check_autotrader_alive
            return _check_autotrader_alive()

    def test_weekend_returns_none(self):
        """Fim de semana: autotrader ausente é normal (retorna None)."""
        # is_trading_day=False simula sábado/domingo/feriado
        result = self._check(is_trading_day_ok=False, now_hour=10, now_minute=0)
        self.assertIsNone(result, "Fim de semana não deve reportar autotrader_dead")

    def test_before_trading_hours_returns_none(self):
        """Dia útil, 08:00 (antes das 09:05): autotrader ausente é normal."""
        result = self._check(is_trading_day_ok=True, now_hour=8, now_minute=0)
        self.assertIsNone(result, "Antes das 09:05 não deve reportar autotrader_dead")

    def test_after_trading_hours_returns_none(self):
        """Dia útil, 17:00 (depois das 16:50): autotrader ausente é normal."""
        result = self._check(is_trading_day_ok=True, now_hour=17, now_minute=0)
        self.assertIsNone(result, "Depois das 16:50 não deve reportar autotrader_dead")

    def test_during_eod_reconcile_window_reports_dead(self):
        """Dia útil, 16:48 (dentro da janela ativa 09:05-16:50): reporta dead.
        A janela de reconcile do GATE vai até 16:50 (5min pós-close), então
        às 16:48 o autotrader ainda deveria estar rodando."""
        result = self._check(is_trading_day_ok=True, now_hour=16, now_minute=48)
        self.assertIsNotNone(result, "16:48 ainda na janela ativa — deve reportar dead")

    def test_during_trading_hours_reports_dead(self):
        """Dia útil, 10:00 (dentro de 09:05-16:50) + pgrep vazio: reporta dead."""
        result = self._check(is_trading_day_ok=True, now_hour=10, now_minute=0)
        self.assertIsNotNone(result, "Dentro de horário útil deve reportar autotrader_dead")
        self.assertEqual(result.type, "autotrader_dead")
        # severity é lowercase no HealthIssue
        self.assertEqual(result.severity.lower(), "critical")

    def test_just_at_start_time_reports_dead(self):
        """Dia útil, exatamente 09:05 (início): dentro da janela, reporta dead."""
        result = self._check(is_trading_day_ok=True, now_hour=9, now_minute=5)
        self.assertIsNotNone(result, "09:05 é início de trading — deve reportar dead")

    def test_just_before_end_reports_dead(self):
        """Dia útil, 16:49 (antes das 16:50): dentro da janela, reporta dead."""
        result = self._check(is_trading_day_ok=True, now_hour=16, now_minute=49)
        self.assertIsNotNone(result, "16:49 ainda está na janela — deve reportar dead")


if __name__ == "__main__":
    unittest.main()
