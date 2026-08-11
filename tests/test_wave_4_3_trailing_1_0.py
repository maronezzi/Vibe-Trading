"""
test_wave_4_3_trailing_1_0.py
=============================
TDD: garante que trail_activate é SANO por símbolo (Wave 4.3 + 880.F).

Wave 4.3 (2026-06-26): trail_activate não pode ser ATR-demasiado-alto
(1.2 ERA probião—WIN nunca ativava, 76% SL_SERVIDOR).

Wave 880.F (2026-08-07): o R$/ATR varia ~1000x entre símbolos
(WDO ~R$84k, WSP ~R$15, BIT ~R$1.5k, WIN ~R$123). Por isso o trailing
NÃO pode ser número fixo global — deve ser per-símbolo e AGI-tunável.
Valores calibrados pra ativar em ~R$80-150 de lucro:
  WDO: 0.001 (ATR enorme) | WSP: 3.0 (ATR pequeno) | BIT: 0.07 | WIN: 0.8
"""
import json
import os
import sys
import unittest

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)

CONFIG_PATH = os.path.join(PROJECT_ROOT, "vt_config.json")


class TestTrailActivateSane(unittest.TestCase):
    """trail_activate deve ser calibrado por símbolo (não fixo 1.0)."""

    def setUp(self):
        with open(CONFIG_PATH) as f:
            self.config = json.load(f)

    def test_no_trail_activate_absurdly_high(self):
        """Nenhum par deve ter trail_activate > 1.0 nos símbolos de ATR grande
        (WDO/BIT), senão o trailing nunca ativa. WSP usa >1.0 (ATR pequeno)."""
        pbt = self.config.get("params_by_tf", {})
        high = []
        for pair_key, params in pbt.items():
            ta = params.get("trail_activate")
            if ta is None:
                continue
            root = pair_key.split("_")[0]
            if root in ("WDO", "BIT") and ta > 1.0:
                high.append(f"{pair_key}={ta}")
        self.assertEqual(
            high, [],
            f"WDO/BIT com trail_activate > 1.0 (trailing nunca ativa): {high}"
        )

    def test_wdo_trail_activate_small(self):
        """WDO precisa de trail_activate pequeno (1 ATR ~R$84k). Default 0.001
        ativa em ~R$84 de lucro — não R$84.000."""
        pbt = self.config.get("params_by_tf", {})
        wdo_vals = [p.get("trail_activate") for k, p in pbt.items()
                    if k.startswith("WDO_") and p.get("trail_activate") is not None]
        self.assertTrue(wdo_vals, "WDO deveria ter trail_activate definido")
        for v in wdo_vals:
            self.assertLess(v, 0.01,
                            f"WDO trail_activate={v} ainda alto demais (1 ATR ~R$84k)")

    def test_win_trail_activate_reasonable(self):
        """WIN (point=1.0, 1 ATR ~R$123) deve ativar em lucro razoável (~R$100)."""
        pbt = self.config.get("params_by_tf", {})
        win_vals = [p.get("trail_activate") for k, p in pbt.items()
                    if k.startswith("WIN_") and p.get("trail_activate") is not None]
        for v in win_vals:
            self.assertGreaterEqual(v, 0.5,
                                    f"WIN trail_activate={v} muito baixo (whipsaw)")
            self.assertLessEqual(v, 1.0,
                                 f"WIN trail_activate={v} alto demais (raramente ativa)")

    def test_audit_trail_wave_4_3_in_notes(self):
        """_notes deve documentar a mudança Wave 4.3 (ou 880.F)."""
        notes = self.config.get("_notes", "")
        self.assertTrue(
            "Wave 4.3" in notes or "880.F" in notes or "trail" in notes.lower(),
            "Mudança trail deve estar documentada em _notes"
        )

    def test_updated_by_is_wave_4_3(self):
        """_updated_by deve indicar Wave 4.3 ou 880.F (autorização Bruno 07/08)."""
        updated_by = self.config.get("_updated_by", "")
        self.assertTrue(
            "wave_4" in updated_by.lower() or "wave_9" in updated_by.lower()
            or "880" in updated_by.lower() or "agi" in updated_by.lower(),
            f"_updated_by deve indicar wave_4/wave_9/880.F, está '{updated_by}'"
        )


if __name__ == "__main__":
    unittest.main()