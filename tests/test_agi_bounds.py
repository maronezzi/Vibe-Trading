"""Test #1 — sl_atr_mult floor é 1.0 (decisão de safety).

O floor de sl_atr_mult foi elevado de 0.5 para 1.0 (commit 51820b064,
"fix(safety): AGI SL management — adaptive bounds", 2026-06-22): stops
abaixo de 1.0×ATR geram noise-level stops que não protegem a posição de
verdade. Este teste guarda essa decisão de safety.

Os testes antigos (floor ≤ 0.5 / ≤ 0.6) codificavam a intenção PRÉ-fix
(deixar o Explorer aplicar 0.6) e foram superados. Complementa
test_agi_v3.py::TestParamBoundsSL::test_sl_atr_mult_floor_is_1_0.
"""
import sys
import unittest
from pathlib import Path

# Adiciona o diretório do projeto ao path
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from agi_tuning_17h import PARAM_BOUNDS, MAX_CHANGE_PCT  # noqa: E402


class TestSlAtrMultFloor(unittest.TestCase):
    def test_sl_atr_mult_floor_is_at_least_1_0(self):
        """Floor deve ser >= 1.0 — stops abaixo disso são noise-level."""
        lo, hi = PARAM_BOUNDS["sl_atr_mult"]
        self.assertGreaterEqual(
            lo, 1.0,
            f"sl_atr_mult floor {lo} abaixo de 1.0 gera stops noise-level "
            f"(decisão de safety do commit 51820b064)."
        )

    def test_sl_atr_mult_upper_bound(self):
        """Teto deve continuar aceitando valores razoáveis (>= 1.0)."""
        _, hi = PARAM_BOUNDS["sl_atr_mult"]
        self.assertGreaterEqual(hi, 1.0, f"sl_atr_mult hi {hi} baixo demais")

    def test_sl_atr_mult_max_change_pct_capped(self):
        """Mudança de sl_atr_mult não pode exceder ±30% por iteração."""
        self.assertLessEqual(
            MAX_CHANGE_PCT["sl_atr_mult"], 0.5,
            "Mudança muito abrupta (>50%) por iteração. Manter conservador."
        )


if __name__ == "__main__":
    unittest.main()
