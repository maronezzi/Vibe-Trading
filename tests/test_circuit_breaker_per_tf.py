"""
test_circuit_breaker_per_tf.py
==============================
Testa o circuit breaker per-(symbol_root, tf) introduzido na Wave Melhoria 1.

Cobre a função ``_check_consecutive_losses(symbol, tf)`` no caminho per-TF:
- Após N losses consecutivas no slot → bloqueia (retorna False).
- Halt expira após halt_duration_minutes.
- Win reseta o contador do slot.
- Slots diferentes (mesmo symbol, TF diferente) são independentes.
- Default 999 (config ausente ou desligada) = nunca bloqueia (fail-open).

NÃO testa o caminho legado per-symbol (tf=None) — esse fica com max=999.
"""
import sys
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


class TestCircuitBreakerPerTf(unittest.TestCase):
    """Garante que o circuit breaker per-(symbol,tf) funciona como esperado."""

    def setUp(self):
        # SessionState mock — só os attrs que _check_consecutive_losses lê.
        self.state = MagicMock()
        self.state.consecutive_losses_by_tf = {}
        self.state.halt_until_by_tf = {}
        self.state.consecutive_losses = {}      # legado, não deve ser tocado no path tf
        self.state.halt_until = {}
        self.state.max_consecutive_losses = 999
        self.state.daily_pnl = -150.0

        # Config mock com max_consecutive_losses_by_tf e halt_duration_minutes_by_tf.
        self.config = {
            "max_consecutive_losses_by_tf": {
                "WIN_M5": 3,
                "WIN_M15": 4,
                "WDO_M5": 999,  # desligado
            },
            "halt_duration_minutes_by_tf": {
                "WIN_M5": 45,
                "WIN_M15": 60,
            },
        }

    def _check(self, symbol, tf):
        """Chama _check_consecutive_losses com state+CONFIG mockados."""
        with patch("core.vt_autotrader.state", self.state), \
             patch("core.vt_autotrader.CONFIG", self.config), \
             patch("core.vt_autotrader.notify_telegram"):
            from core.vt_autotrader import _check_consecutive_losses
            return _check_consecutive_losses(symbol, tf)

    def test_no_losses_allows_trading(self):
        """Slot sem losses consecutivos deve permitir trading."""
        self.assertTrue(self._check("WINQ26", "M5"))

    def test_below_threshold_allows_trading(self):
        """2 losses (< 3) ainda permite trading no WIN_M5."""
        self.state.consecutive_losses_by_tf["WIN_M5"] = 2
        self.assertTrue(self._check("WINQ26", "M5"))

    def test_at_threshold_blocks_and_sets_halt(self):
        """3 losses consecutivas no WIN_M5 (max=3) → bloqueia e seta halt 45min."""
        self.state.consecutive_losses_by_tf["WIN_M5"] = 3
        result = self._check("WINQ26", "M5")
        self.assertFalse(result, "3 losses no WIN_M5 (max=3) deve bloquear")
        # Halt deve ter sido setado ~45min no futuro.
        halt = self.state.halt_until_by_tf.get("WIN_M5")
        self.assertIsNotNone(halt, "halt_until_by_tf[WIN_M5] deve ser setado")
        delta = (halt - datetime.now()).total_seconds() / 60
        self.assertGreater(delta, 40, f"halt deve ser >40min no futuro (got {delta:.0f}min)")
        self.assertLess(delta, 50, f"halt deve ser <50min no futuro (got {delta:.0f}min)")

    def test_halt_active_blocks_entry(self):
        """Halt ativo (no futuro) bloqueia mesmo sem novas losses."""
        self.state.halt_until_by_tf["WIN_M5"] = datetime.now() + timedelta(minutes=30)
        result = self._check("WINQ26", "M5")
        self.assertFalse(result, "Halt ativo deve bloquear entrada")

    def test_halt_expired_clears_counter(self):
        """Halt expirado limpa o contador e permite trading (state hygiene)."""
        self.state.halt_until_by_tf["WIN_M5"] = datetime.now() - timedelta(minutes=5)
        self.state.consecutive_losses_by_tf["WIN_M5"] = 3
        result = self._check("WINQ26", "M5")
        self.assertTrue(result, "Halt expirado deve permitir trading")
        # Counter deve ter sido zerado e halt removido.
        self.assertEqual(self.state.consecutive_losses_by_tf.get("WIN_M5", 0), 0)
        self.assertNotIn("WIN_M5", self.state.halt_until_by_tf)

    def test_different_tfs_independent(self):
        """WIN_M5 e WIN_M15 têm thresholds diferentes e são independentes."""
        # WIN_M5 com 3 losses bloqueia
        self.state.consecutive_losses_by_tf["WIN_M5"] = 3
        self.assertFalse(self._check("WINQ26", "M5"))
        # WIN_M15 com 3 losses (< 4) ainda permite
        self.state.consecutive_losses_by_tf["WIN_M15"] = 3
        self.assertTrue(self._check("WINQ26", "M15"))

    def test_disabled_slot_never_blocks(self):
        """Slot com 999 (desligado) nunca bloqueia, mesmo com muitas losses."""
        self.state.consecutive_losses_by_tf["WDO_M5"] = 50
        result = self._check("WDON26", "M5")
        self.assertTrue(result, "WDO_M5 (max=999) nunca deve bloquear")

    def test_contract_resolution_uses_root(self):
        """WINQ26 (contract) resolve para WIN root — chave do slot é WIN_M5."""
        self.state.consecutive_losses_by_tf["WIN_M5"] = 3
        result = self._check("WINQ26", "M5")
        self.assertFalse(result, "WINQ26 deve mapear para WIN_M5 no circuit breaker")

    def test_win_resets_counter(self):
        """Após um win, o contador do slot deve resetar (simula bookkeeping)."""
        # Simula 3 losses, depois um win zera o contador
        self.state.consecutive_losses_by_tf["WIN_M5"] = 3
        self.assertFalse(self._check("WINQ26", "M5"))
        # Win zera (bookkeeping feito no close detection, não aqui)
        self.state.consecutive_losses_by_tf["WIN_M5"] = 0
        self.state.halt_until_by_tf.pop("WIN_M5", None)
        self.assertTrue(self._check("WINQ26", "M5"))


if __name__ == "__main__":
    unittest.main()
