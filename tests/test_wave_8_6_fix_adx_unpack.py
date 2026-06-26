"""
test_wave_8_6_fix_adx_unpack.py
================================
TDD: garante que pivot_points.py desempacota a tupla de calculate_adx
corretamente. BUG introduzido pelo Wave 2.4.

O bug: calculate_adx() retorna tupla (adx_val, plus_di, minus_di),
mas pivot_points.py usa o retorno como int. Resultado:
  TypeError: '>=' not supported between instances of 'tuple' and 'int'

Detectado em produção: 2026-06-26 09:21 (autotrader crashou após
hot-reload da config v903).

FIX: desempacotar tupla com (adx, _, _) = calculate_adx(...)
"""
import sys
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


class TestPivotPointsADXUnpacking(unittest.TestCase):
    """calculate_adx retorna tupla (adx, +DI, -DI) — pivot_points deve desempacotar."""

    def test_calculate_adx_returns_tuple(self):
        """calculate_adx retorna (adx, plus_di, minus_di) — 3 valores."""
        from core.vt_autotrader import calculate_adx
        # Mock de bars (não testamos a função em si, só o contrato)
        bars = [
            {"high": 110, "low": 90, "close": 100},
            {"high": 112, "low": 92, "close": 102},
        ] * 30  # 60 bars para satisfazer period*2
        result = calculate_adx(bars, period=14)
        self.assertIsInstance(
            result, tuple,
            f"calculate_adx deve retornar tuple, got {type(result)}"
        )
        self.assertEqual(
            len(result), 3,
            f"calculate_adx deve retornar 3 valores, got {len(result)}"
        )

    def test_pivot_points_does_not_crash_with_adx(self):
        """
        pivot_points.check_entry() com ADX válido não pode dar
        TypeError. Wave 2.4 introduziu bug que crashava o
        autotrader.
        """
        from strategies import pivot_points
        from unittest.mock import MagicMock
        utils = {
            "calc_sl": MagicMock(return_value=200),
            "calculate_rsi": MagicMock(return_value=25.0),  # oversold
            # calculate_adx retorna tupla (adx, +DI, -DI) = (30, 20, 10)
            # adx=30 é >= threshold=25, deve rejeitar
            "calculate_adx": MagicMock(return_value=(30, 20, 10)),
        }
        bars = [
            {"time": 100, "open": 100, "high": 110, "low": 90, "close": 105},
            {"time": 99, "open": 100, "high": 110, "low": 90, "close": 105},
        ] * 50  # bastante bars

        params = {
            "rsi_period": 14,
            "rsi_overbought": 70,
            "rsi_oversold": 30,
            "touch_pct": 0.002,
            "sl_atr_mult": 1.5,
            "adx_threshold": 25,
            "adx_period": 14,
        }

        # Não pode dar TypeError
        try:
            result = pivot_points.check_entry(
                "WINQ26", "M15", 93.33, 200, 1000, bars, params, utils
            )
            # Se adx=30 >= 25, deve rejeitar (return None)
            self.assertIsNone(
                result,
                f"ADX=30 (trending) com threshold=25 deve rejeitar. Got {result}"
            )
        except TypeError as e:
            self.fail(
                f"pivot_points crasha com TypeError — Wave 2.4 bug: {e}"
            )


if __name__ == "__main__":
    unittest.main()
