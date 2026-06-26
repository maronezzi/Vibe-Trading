"""
test_wave_4_3_trailing_1_0.py
=============================
TDD: garante que trail_activate = 1.0 (era 1.2) em todos os pares
ativos. Causa raiz do 76% SL_SERVIDOR.

Wave 4.3 (2026-06-26):
  - Trail atual só ativa com lucro >= 1.2×ATR — preço raramente chega.
  - 76% dos exits são SL estopado em <5min (paralisia de trailing).
  - trail_activate=1.0×ATR ativa mais cedo, captura ANTES do SL.

Por que importa: trailing 1.2 é ATR-DEMASIADO-ALTO pra WIN. WIN
ATR típico M15 = 100-200pts, e 1.2× = 120-240pts de lucro
exigido antes do trail ativar. Raramente o trade vai tão longe
sem estopar antes.
"""
import json
import os
import sys
import unittest

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)

CONFIG_PATH = os.path.join(PROJECT_ROOT, "vt_config.json")


class TestTrailActivateAt1(unittest.TestCase):
    """trail_activate deve ser 1.0 (não 1.2) em todos os pares ativos."""

    def setUp(self):
        with open(CONFIG_PATH) as f:
            self.config = json.load(f)

    def test_no_trail_activate_above_1_0_in_params_by_tf(self):
        """Nenhum par em params_by_tf deve ter trail_activate > 1.0."""
        pbt = self.config.get("params_by_tf", {})
        violations = []
        for pair_key, params in pbt.items():
            ta = params.get("trail_activate")
            if ta is not None and ta > 1.0:
                violations.append(f"{pair_key}={ta}")
        self.assertEqual(
            violations, [],
            f"trail_activate > 1.0 ainda existe em: {violations}. "
            f"Wave 4.3 deveria ter mudado para 1.0."
        )

    def test_audit_trail_wave_4_3_in_notes(self):
        """_notes deve documentar a mudança Wave 4.3."""
        notes = self.config.get("_notes", "")
        self.assertIn(
            "Wave 4.3", notes,
            "Mudança trail_activate 1.2→1.0 deve estar documentada em _notes"
        )

    def test_updated_by_is_wave_4_3(self):
        """_updated_by deve indicar Wave 4.3 (ou wave_8_6+ posterior)."""
        updated_by = self.config.get("_updated_by", "")
        self.assertTrue(
            "wave_4" in updated_by.lower() or "wave_8" in updated_by.lower(),
            f"_updated_by deve indicar 'wave_4' ou 'wave_8', está '{updated_by}'"
        )


if __name__ == "__main__":
    unittest.main()
