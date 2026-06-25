"""
test_cooldown_respects_params.py
TDD: garante que _check_cooldown() respeita params_by_tf, não só CONFIG[symbol].

Achado 2026-06-25: _check_cooldown() em core/vt_autotrader.py:1066 le
    _params = CONFIG.get(_root.lower(), CONFIG.get('win', {}))
em vez de aceitar os params do caller. Resultado: params_by_tf.WIN_M5
.cooldown_seconds=1200 e IGNORADO e o bot usa CONFIG[win].cooldown_seconds=270.

Isso causou martelada de 5 losses WINQ26 M5 SELL entre 11:18-11:40 hoje
(cooldown real ~1.3min entre entradas em vez dos 20min configurados).

FIX: _check_cooldown() deve usar os params do caller (que vem de
_get_params_for_tf), com fallback apenas se vier None/vazio.

Este teste mocka state.last_trade_time e valida que:
1. Cooldown por (symbol, tf, direction) usa params_by_tf quando fornecido
2. Cooldown por symbol (sem tf/direction) usa CONFIG[symbol]
3. Fallback para 300s default se nem params nem CONFIG tem cooldown_seconds
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


def _mock_state_with_last_trade(dir_key, seconds_ago):
    state = type("MockState", (), {})()
    state.last_trade_time = {dir_key: datetime.now() - timedelta(seconds=seconds_ago)}
    state.current_day = datetime.now().date().isoformat()
    state.daily_trade_count = 0
    state.consecutive_losses = {}
    return state


class TestCooldownRespectsParams(unittest.TestCase):
    """_check_cooldown deve usar params_by_tf do caller, nao CONFIG global."""

    def setUp(self):
        import core.vt_autotrader as vt
        self.vt = vt

    def test_check_cooldown_uses_params_from_caller(self):
        """Se params tem cooldown_seconds=1200, _check_cooldown deve usar 1200.

        Estado: last_trade_time[WINQ26_M5_SELL] = 60s atras.
        Com cd=1200, deve BLOQUEAR (retornar False).
        """
        state = _mock_state_with_last_trade("WINQ26_M5_SELL", seconds_ago=60)
        params_by_tf = {"cooldown_seconds": 1200}

        with patch.object(self.vt, "state", state):
            result = self.vt._check_cooldown(
                "WINQ26", params_by_tf, tf="M5", direction="SELL"
            )
        self.assertFalse(
            result,
            "_check_cooldown deveria BLOQUEAR (60s < cooldown 1200s). "
            "Mas retornou True - bug do cooldown nao usar params_by_tf.",
        )

    def test_check_cooldown_respects_120s_when_cd_is_1200(self):
        """Com cd=1200, passado 119s, deve BLOQUEAR. Passado 1201s, deve LIBERAR."""
        state_fresh = _mock_state_with_last_trade("WINQ26_M5_SELL", seconds_ago=119)
        params_by_tf = {"cooldown_seconds": 1200}
        with patch.object(self.vt, "state", state_fresh):
            result_fresh = self.vt._check_cooldown(
                "WINQ26", params_by_tf, tf="M5", direction="SELL"
            )
        self.assertFalse(result_fresh, "119s < 1200s deveria BLOQUEAR")

        state_old = _mock_state_with_last_trade("WINQ26_M5_SELL", seconds_ago=1201)
        with patch.object(self.vt, "state", state_old):
            result_old = self.vt._check_cooldown(
                "WINQ26", params_by_tf, tf="M5", direction="SELL"
            )
        self.assertTrue(result_old, "1201s > 1200s deveria LIBERAR")

    def test_check_cooldown_uses_win_config_when_no_params(self):
        """Fallback: sem params, usa CONFIG[win].cooldown_seconds=270."""
        state = _mock_state_with_last_trade("WINQ26_M5_SELL", seconds_ago=60)
        with patch.object(self.vt, "state", state):
            result = self.vt._check_cooldown("WINQ26", None, tf="M5", direction="SELL")
        self.assertFalse(result, "60s < 270s (fallback) deveria BLOQUEAR")

    def test_check_cooldown_default_when_nothing_configured(self):
        """Default: 300s quando nem params nem CONFIG tem cooldown_seconds."""
        state = _mock_state_with_last_trade("WINQ26_M5_SELL", seconds_ago=60)
        with patch.object(self.vt, "state", state):
            with patch.object(self.vt, "CONFIG", {}):
                result = self.vt._check_cooldown(
                    "WINQ26", None, tf="M5", direction="SELL"
                )
        self.assertFalse(result, "60s < 300s (default) deveria BLOQUEAR")

    def test_check_cooldown_different_symbol_independent(self):
        """Cooldown por (symbol, tf, direction). WIN M5 SELL nao bloqueia WDO M5 BUY."""
        state = _mock_state_with_last_trade("WINQ26_M5_SELL", seconds_ago=60)
        params_by_tf = {"cooldown_seconds": 1200}
        with patch.object(self.vt, "state", state):
            win = self.vt._check_cooldown(
                "WINQ26", params_by_tf, tf="M5", direction="SELL"
            )
            wdo = self.vt._check_cooldown(
                "WDOQ26", params_by_tf, tf="M5", direction="BUY"
            )
        self.assertFalse(win, "WINQ26 M5 SELL com 60s < 1200s deveria BLOQUEAR")
        self.assertTrue(wdo, "WDOQ26 M5 BUY sem last trade deveria LIBERAR")


if __name__ == "__main__":
    unittest.main()
