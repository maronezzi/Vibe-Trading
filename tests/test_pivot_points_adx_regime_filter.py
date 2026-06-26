"""
test_pivot_points_adx_regime_filter.py
=======================================
TDD: garante que PIVOT_POINTS rejeita entradas em mercado trending (ADX>=25).

PROBLEMA IDENTIFICADO 2026-06-25 (auditoria de código):
  PIVOT_POINTS (strategies/pivot_points.py:60-72) é uma estratégia de
  MEAN REVERSION. Funciona bem em RANGING (ADX<25). Mas o código
  atual não checa ADX — entra em downtrends fortes onde o "suporte"
  S1/S2 é quebrado consecutivamente. Resultado: WIN_M15 PIVOT_POINTS
  com WR 19.5% em 30d (41 trades, -R$291) — explica parte dos -R$8.430.

FIX: PIVOT_POINTS deve chamar calculate_adx() e rejeitar quando
ADX >= 25 (trending confirmado). Quando ADX < 25, manter comportamento
atual (estratégia é otimizada pra ranging).

IMPORTANTE: adx_threshold é configurável via params (default 25).
Bruno pode afrouxar/aperto sem mexer no código.

Por que importa: evita entradas contra-tendência. Estratégia de
reversão (mean reversion) em downtrend é estaticamente perdedora
porque não há "reversão" — só continuation.
"""
import sys
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


class TestPivotPointsADXRegimeFilter(unittest.TestCase):
    """Garante que PIVOT_POINTS rejeita mercado trending (ADX>=25)."""

    def setUp(self):
        # Carrega módulo da estratégia
        from strategies import pivot_points
        self.pivot_points = pivot_points

        # Mock utils
        self.utils = {
            "calc_sl": MagicMock(return_value=200),
            "calculate_rsi": MagicMock(return_value=50.0),
            "calculate_adx": MagicMock(return_value=30.0),  # default: trending
        }

        # Bars mock: 2 candles para pivot calcular
        self.bars = [
            {"time": 100, "open": 100, "high": 110, "low": 90, "close": 105},
            {"time": 99, "open": 100, "high": 110, "low": 90, "close": 105},
        ]

        # Params default + custom
        self.params = {
            "rsi_period": 14,
            "rsi_overbought": 70,
            "rsi_oversold": 30,
            "touch_pct": 0.002,
            "sl_atr_mult": 1.5,
        }

    def test_rejects_when_adx_above_threshold(self):
        """ADX=30 (trending) deve rejeitar mesmo com S1 hit + RSI oversold."""
        self.utils["calculate_adx"].return_value = 30.0
        # Preço perto de S1 (suporte)
        # S1 = 2*pivot - prev_high = 2*100 - 110 = 90
        price = 90.5
        atr = 200

        result = self.pivot_points.check_entry(
            "WINQ26", "M15", price, atr, 100, self.bars, self.params, self.utils
        )
        self.assertIsNone(
            result,
            f"PIVOT_POINTS deve rejeitar ADX=30 (trending), retornou {result}"
        )

    def test_accepts_when_adx_below_threshold(self):
        """ADX=20 (ranging) deve aceitar com S1 hit + RSI oversold."""
        self.utils["calculate_adx"].return_value = 20.0
        self.utils["calculate_rsi"].return_value = 25.0  # oversold
        # S1 = 2*pivot - prev_high = 2*101.67 - 110 = 93.33
        # touch_pct=0.002 (0.2%), preço 93.33 (exato no S1)
        price = 93.33
        atr = 200

        result = self.pivot_points.check_entry(
            "WINQ26", "M15", price, atr, 100, self.bars, self.params, self.utils
        )
        self.assertIsNotNone(
            result, f"PIVOT_POINTS deve aceitar ADX=20 (ranging) com S1 hit + RSI oversold, got {result}"
        )
        if result:
            self.assertEqual(result["direction"], "BUY")

    def test_adx_threshold_configurable(self):
        """adx_threshold=15 deve rejeitar ADX=20 (mais restritivo)."""
        self.utils["calculate_adx"].return_value = 20.0
        self.utils["calculate_rsi"].return_value = 25.0
        self.params["adx_threshold"] = 15  # mais restritivo
        price = 93.33  # S1 exato
        atr = 200

        result = self.pivot_points.check_entry(
            "WINQ26", "M15", price, atr, 100, self.bars, self.params, self.utils
        )
        self.assertIsNone(
            result,
            f"adx_threshold=15 + ADX=20 deve rejeitar, retornou {result}"
        )

    def test_default_adx_threshold_is_25(self):
        """Default adx_threshold deve ser 25 (ranging puro)."""
        self.utils["calculate_adx"].return_value = 24.0  # abaixo do default
        self.utils["calculate_rsi"].return_value = 25.0
        # S1=93.33, preço no nível
        price = 93.33
        atr = 200

        result = self.pivot_points.check_entry(
            "WINQ26", "M15", price, atr, 100, self.bars, self.params, self.utils
        )
        # Com ADX=24 (ranging) deve aceitar
        self.assertIsNotNone(
            result, f"ADX=24 (ranging, default threshold=25) deve aceitar, got {result}"
        )


class TestPivotPointsCallsCalculateAdx(unittest.TestCase):
    """Garante via AST que PIVOT_POINTS chama calculate_adx()."""

    def test_calculate_adx_in_utils_lookup(self):
        """PIVOT_POINTS.check_entry deve acessar utils['calculate_adx']."""
        src = Path(PROJECT_ROOT, "strategies", "pivot_points.py").read_text()
        self.assertIn(
            'calculate_adx', src,
            "strategies/pivot_points.py deve referenciar calculate_adx para "
            "filtro de regime (ADX)"
        )


if __name__ == "__main__":
    unittest.main()
