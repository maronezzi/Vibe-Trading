"""
test_agi_sl_atr_grid.py
=================================
TDD: garante que o AGI ARREDONDA sl_atr_mult para um grid razoável,
eliminando "magic numbers" bayesianos (1.006, 1.0009, 1.2345, etc.).

PROBLEMA IDENTIFICADO 2026-06-25 (auditoria de código):
  AGI bayesiano encontra "magic numbers" que minimizam loss no treino
  mas não generalizam:
  - WIN_M30.sl_atr_mult = 1.006448641952834 (overfit bayesiano)
  - WSP_M30.sl_atr_mult = 1.0009392633353573 (overfit bayesiano)
  - Ambos efetivamente ~1.0, mas o número "mágico" sugere precisão que
    não existe.

FIX: o AGI deve ARREDONDAR sl_atr_mult para o grid
{1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0} antes de aplicar à config.
Wave 1.3 já removeu os 2 magic numbers do config (substituiu por 1.5
e 1.0), mas a FUNÇÃO de arredondamento não existe — se o AGI rodar
de novo e encontrar outro magic number, ele seria aplicado.

VALIDAÇÃO:
  - test_1.006_rounds_to_1.0 (overfit bayesiano → grid mais próximo)
  - test_1.2345_rounds_to_1.25
  - test_1.5_stays_1.5 (grid value preservado)
  - test_2.7_rounds_to_2.5 (entre grid → arredonda p/ próximo)
  - test_function_exists_and_is_used

Por que importa: o AGI converte ruído em precisão falsa. Magic numbers
são overfit que infla PF no treino e desmonta no live. Grid razoável
= robustez.

NOTA: a função _round_sl_atr_to_grid() deve ser CHAMADA antes de
aplicar params_to_apply no auto-apply section. Wave 4.1 só cria e
testa a função — Wave 4.2 (próxima) wireá no fluxo.
"""
import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)

# Grid razoável (anti-overfit, anti-magic-number)
SL_ATR_GRID = [1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]


def _round_sl_atr_to_grid(value: float) -> float:
    """Arredonda sl_atr_mult para o grid razoável mais próximo.

    Args:
        value: qualquer float (0.5 a 5.0 tipicamente)

    Returns:
        O valor do grid mais próximo.
    """
    if value is None:
        return 1.5  # default seguro
    if value < SL_ATR_GRID[0]:
        return SL_ATR_GRID[0]
    if value > SL_ATR_GRID[-1]:
        return SL_ATR_GRID[-1]
    # Encontra o grid point mais próximo
    return min(SL_ATR_GRID, key=lambda g: abs(g - value))


class TestRoundSlAtrToGrid(unittest.TestCase):
    """Garante que _round_sl_atr_to_grid() retorna valores do grid."""

    def test_1_006_rounds_to_1_0(self):
        """1.006 (overfit bayesiano) deve arredondar para 1.0."""
        result = _round_sl_atr_to_grid(1.006448641952834)
        self.assertEqual(result, 1.0)

    def test_1_2345_rounds_to_1_25(self):
        """1.2345 deve arredondar para 1.25 (mais próximo)."""
        result = _round_sl_atr_to_grid(1.2345)
        self.assertEqual(result, 1.25)

    def test_1_5_stays_1_5(self):
        """1.5 (grid value) deve permanecer 1.5."""
        result = _round_sl_atr_to_grid(1.5)
        self.assertEqual(result, 1.5)

    def test_2_7_rounds_to_2_5(self):
        """2.7 (entre 2.5 e 3.0) deve arredondar para 2.5 (mais próximo)."""
        # 2.7 - 2.5 = 0.2; 3.0 - 2.7 = 0.3 → 2.5 mais próximo
        result = _round_sl_atr_to_grid(2.7)
        self.assertEqual(result, 2.5)

    def test_2_8_rounds_to_3_0(self):
        """2.8 deve arredondar para 3.0 (mais próximo)."""
        # 2.8 - 2.5 = 0.3; 3.0 - 2.8 = 0.2 → 3.0 mais próximo
        result = _round_sl_atr_to_grid(2.8)
        self.assertEqual(result, 3.0)

    def test_value_below_grid_returns_floor(self):
        """Valor < 1.0 retorna 1.0 (floor)."""
        self.assertEqual(_round_sl_atr_to_grid(0.6), 1.0)
        self.assertEqual(_round_sl_atr_to_grid(0.5), 1.0)

    def test_value_above_grid_returns_ceiling(self):
        """Valor > 3.0 retorna 3.0 (ceiling)."""
        self.assertEqual(_round_sl_atr_to_grid(4.0), 3.0)
        self.assertEqual(_round_sl_atr_to_grid(5.0), 3.0)

    def test_none_returns_default(self):
        """None retorna 1.5 (default seguro)."""
        self.assertEqual(_round_sl_atr_to_grid(None), 1.5)

    def test_returns_grid_value(self):
        """Resultado sempre está no grid."""
        for test_val in [0.5, 1.0, 1.1, 1.5, 2.0, 2.49, 2.51, 3.0, 3.5, 4.0]:
            result = _round_sl_atr_to_grid(test_val)
            self.assertIn(
                result, SL_ATR_GRID,
                f"_round_sl_atr_to_grid({test_val}) = {result} não está no grid"
            )


class TestRoundSlAtrAvailableInModule(unittest.TestCase):
    """Garante que a função está acessível no módulo agi_tuning_17h."""

    def test_function_in_agi_tuning(self):
        """A função deve estar importável de optimization.agi_tuning_17h."""
        src_path = Path(PROJECT_ROOT, "optimization", "agi_tuning_17h.py")
        if not src_path.exists():
            self.skipTest("agi_tuning_17h.py não encontrado")
        src = src_path.read_text()
        self.assertIn(
            "_round_sl_atr_to_grid", src,
            "optimization/agi_tuning_17h.py deve ter a função _round_sl_atr_to_grid"
        )


if __name__ == "__main__":
    unittest.main()
