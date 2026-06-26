"""
test_squeeze_breakout_strategy.py
==================================
TDD: garante que a estratégia SQUEEZE_BREAKOUT funciona corretamente.

Wave 5 (2026-06-26): nova estratégia baseada em TTM Squeeze.
Filtra chop (BB dentro de KC) + entrada em release de volatilidade
com momentum direcional (MACD hist).

Por que importa: 76% dos exits são SL_SERVIDOR. Mercado em chop gera
sinais falsos. Squeeze Breakout só opera em vol compression +
expansion — espera o mercado "escolher direção" antes de entrar.
"""
import sys
import unittest
from unittest.mock import MagicMock
from pathlib import Path

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


def _make_bars(n=100, base_price=100, atr=2.0, trend="up"):
    """Gera n candles sintéticos para teste."""
    import random
    random.seed(42)
    bars = []
    price = base_price
    for i in range(n):
        if trend == "up":
            change = random.uniform(-0.5, 1.0)
        elif trend == "down":
            change = random.uniform(-1.0, 0.5)
        else:
            change = random.uniform(-0.5, 0.5)
        new_price = price + change
        bar = {
            "time": 1000 + i * 60,
            "open": price,
            "high": new_price + atr / 2,
            "low": new_price - atr / 2,
            "close": new_price,
            "tick_volume": 1000,
        }
        bars.append(bar)
        price = new_price
    return bars


class TestSqueezeBreakoutImport(unittest.TestCase):
    """Garante que o módulo é importável e tem os símbolos esperados."""

    def test_module_imports(self):
        from strategies import squeeze_breakout
        self.assertEqual(squeeze_breakout.STRATEGY_NAME, "SQUEEZE_BREAKOUT")

    def test_has_check_entry(self):
        from strategies import squeeze_breakout
        self.assertTrue(hasattr(squeeze_breakout, "check_entry"))


class TestSqueezeBreakoutRequiresSqueezeRelease(unittest.TestCase):
    """Squeeze Breakout SÓ opera em release de squeeze."""

    def setUp(self):
        from strategies import squeeze_breakout
        self.strategy = squeeze_breakout

    def test_no_entry_when_still_in_squeeze(self):
        """Se mercado ainda em squeeze, retorna None."""
        bars = _make_bars(100, 100, 2.0, "up")
        utils = {
            "calc_sl": MagicMock(return_value=200),
            "calculate_bollinger": MagicMock(return_value=(98, 100, 102)),  # BB tight
            "calculate_ema": MagicMock(return_value=100),
            "calculate_atr": MagicMock(return_value=2.0),
            "calculate_macd": MagicMock(return_value=(0.5, 0.3, 0.2)),  # hist > 0
            "calculate_adx": MagicMock(return_value=30.0),
        }
        # squeeze_now=True (BB 98-102 dentro de KC 97-103)
        result = self.strategy.check_entry(
            "WINQ26", "M15", 100, 200, 1000, bars, {}, utils
        )
        # Se squeeze_now e squeeze_prev são True, squeeze_release=False
        # Depende do mock — vamos só verificar que retorna None
        # (o test de release está abaixo)
        self.assertIsNone(result)

    def test_entry_on_squeeze_release_with_momentum(self):
        """Squeeze release + MACD hist > 0 = BUY entry."""
        bars = _make_bars(100, 100, 2.0, "up")
        # Mock _is_squeeze_on: prev=True (in squeeze), now=False (released)
        # A função é chamada 2x: 1x com bars completo, 1x com bars[:-1]
        from unittest.mock import patch
        with patch.object(self.strategy, "_is_squeeze_on", side_effect=[False, True]) as mock_sq:
            # 1ª chamada (bars completo = now), 2ª (bars[:-1] = prev)
            # Mas a lógica do código: squeeze_release = squeeze_prev AND not squeeze_now
            # Logo: prev=True, now=False → release=True
            result = self.strategy.check_entry(
                "WINQ26", "M15", 100, 200, 1000, bars, {}, {
                    "calc_sl": MagicMock(return_value=200),
                    "calculate_macd": MagicMock(return_value=(0.5, 0.3, 0.5)),
                    "calculate_adx": MagicMock(return_value=30.0),
                }
            )

        self.assertIsNotNone(result, f"Deveria retornar entrada, got None (mock_called: {mock_sq.call_count})")
        if result:
            self.assertEqual(result["direction"], "BUY")


if __name__ == "__main__":
    unittest.main()
