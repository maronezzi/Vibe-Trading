"""
test_agi_best_params_pass_sl_atr_grid.py
===========================================
TDD: garante que AGI best_sl_atr_mult passa pelo SL_ATR_GRID snap
antes de virar "applied" ou virar sugestão ao LLM.

BUG IDENTIFICADO 2026-06-26 19:08 (dry-run AGI):
  optimization.best_sl_atr_mult = 0.6 (em WIN, BIT, WSP, WDO)
  Mas SL_ATR_GRID floor é 1.0 (Wave 4.1 anti-overfit).
  AGI SUGERE 0.6 ao LLM SEM passar pelo snap.
  Resultado: LLM poderia aplicar 0.6 se autorizasse.

FIX Wave 8.8.2:
  Quando optimization.best_sl_atr_mult < SL_ATR_GRID[0],
  snapear para SL_ATR_GRID[0] (1.0) ANTES de:
  - Adicionar a changes_applied
  - Montar prompt para LLM
  - Auto-apply Explorer
"""
import ast
import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


class TestBestParamsSlAtrGrid(unittest.TestCase):
    """best_sl_atr_mult deve ser snapped pro SL_ATR_GRID antes de aplicar."""

    AGI_PATH = Path(PROJECT_ROOT) / "optimization" / "agi_tuning_17h.py"

    def test_snap_sl_atr_to_grid_function_exists(self):
        """Função _round_sl_atr_to_grid deve existir (Wave 4.1)."""
        src = self.AGI_PATH.read_text()
        self.assertIn(
            "def _round_sl_atr_to_grid", src,
            "_round_sl_atr_to_grid deve existir (Wave 4.1)"
        )

    def test_best_sl_atr_mult_snapped_before_prompt(self):
        """best_sl_atr_mult deve ser snapped antes de virar prompt."""
        src = self.AGI_PATH.read_text()
        # Wave 8.8.2: linha 1262 deve usar _round_sl_atr_to_grid()
        snap_used = (
            "_round_sl_atr_to_grid(opt[\"best_sl_atr_mult\"])" in src
            or "_round_sl_atr_to_grid(opt['best_sl_atr_mult'])" in src
        )
        self.assertTrue(
            snap_used,
            "best_sl_atr_mult deve passar por _round_sl_atr_to_grid() "
            "antes de virar applied/sugestão. Wave 8.8.2 fix."
        )

    def test_apply_changes_uses_snapped_sl_atr(self):
        """save_params deve usar snap_sl_atr_to_grid."""
        src = self.AGI_PATH.read_text()
        # Quando save_params é chamado com sl_atr_mult, deve ter snap antes
        # Procura chamada próxima de "save_params" e "sl_atr_mult"
        lines = src.splitlines()
        for i, line in enumerate(lines):
            if "save_params" in line and "clamped_params" in lines[i:i+5]:
                # Achou caminho de save — verifica se snap está próximo
                nearby_snap = any(
                    "snap_sl_atr" in lines[j]
                    for j in range(max(0, i-10), min(len(lines), i+10))
                )
                self.assertTrue(
                    nearby_snap,
                    f"save_params perto da linha {i+1} deve usar snap_sl_atr_to_grid. "
                    f"Contexto: {lines[max(0,i-3):i+5]}"
                )
                break


if __name__ == "__main__":
    unittest.main()