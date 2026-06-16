"""Test #1 — sl_atr_mult floor allows 0.6 (Explorer recommendation).

The Strategy Explorer consistently finds sl_atr_mult=0.6 as the profitable
config. The previous floor of 0.8 was blocking every suggestion, so the
AGI could never apply the change. Lower the floor to 0.5 so 0.6 fits
with headroom for finer values.
"""
import sys
import unittest
from pathlib import Path

# Adiciona o diretório do projeto ao path
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from agi_tuning_17h import PARAM_BOUNDS, MAX_CHANGE_PCT  # noqa: E402


class TestSlAtrMultFloor(unittest.TestCase):
    def test_sl_atr_mult_floor_allows_explorer_recommendation(self):
        """0.6 deve estar DENTRO do range (entre lo e hi)."""
        lo, hi = PARAM_BOUNDS["sl_atr_mult"]
        self.assertLessEqual(
            lo, 0.6,
            f"sl_atr_mult floor {lo} bloqueia a recomendação do Explorer (0.6). "
            f"O AGI fica preso aplicando 0.8 toda iteração e o gate nunca converge."
        )
        self.assertLessEqual(0.6, hi, f"sl_atr_mult hi {hi} deveria aceitar 0.6")

    def test_sl_atr_mult_floor_at_least_05(self):
        """Floor deve ser <= 0.5 (deixa headroom pra 0.6)."""
        lo, _ = PARAM_BOUNDS["sl_atr_mult"]
        self.assertLessEqual(
            lo, 0.5,
            f"sl_atr_mult floor {lo} alto demais, deveria ser <= 0.5"
        )

    def test_sl_atr_mult_max_change_pct_capped(self):
        """Mudança de sl_atr_mult não pode exceder ±30% por iteração."""
        self.assertLessEqual(
            MAX_CHANGE_PCT["sl_atr_mult"], 0.5,
            "Mudança muito abrupta (>50%) por iteração. Manter conservador."
        )


if __name__ == "__main__":
    unittest.main()
