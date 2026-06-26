"""
test_wave_8_5_assign_strategies.py
====================================
TDD: garante que Wave 8.5 atribui estratégias validadas por
sub-agente de otimização (pair_optimizer).

Wave 8.5 (2026-06-26, sub-agente pair_optimizer):
  Achados com run real (MT5 online) + Bayesian refine:
  - WDO_M5 STRONG_TREND: WR 100% | PnL +R$3.300 | n=29 | avg=R$113
  - WSP_M15 SUPERTREND: WR 100% | PnL +R$174 | n=6 | avg=R$29
  - WIN_M5 STRONG_TREND (já ativo): WR 60% | PnL +R$1.184 | n=40
  - BIT_M5 ENHANCED_MACD_MOMENTUM (já ativo): WR 89% | n=36 (sub-agente)

FIX: trocar WDO_M5 e WSP_M15 para as estratégias com edge.

Cuidado:
- WSP_M15 n=6 é amostra PEQUENA. 100% WR pode ser sorte. Wave 8.5
  aceita n>=5 mas com warning.
- WDO_M5 n=29 é estatisticamente significativo.
"""
import json
import os
import sys
import unittest

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)

CONFIG_PATH = os.path.join(PROJECT_ROOT, "vt_config.json")


class TestWDO_M5_StrongTrend(unittest.TestCase):
    """WDO_M5 deve usar STRONG_TREND (sub-agente: 100% WR +R$3.300)."""

    def setUp(self):
        with open(CONFIG_PATH) as f:
            self.config = json.load(f)

    def test_wdo_m5_uses_strong_trend(self):
        strat = self.config.get("strategy_by_tf", {}).get("WDO_M5")
        self.assertEqual(
            strat, "STRONG_TREND",
            f"WDO_M5 deve ser STRONG_TREND (sub-agente: 100% WR +R$3.300), "
            f"está '{strat}'"
        )

    def test_audit_trail_wave_8_5(self):
        notes = self.config.get("_notes", "")
        self.assertIn("Wave 8.5", notes, "Wave 8.5 deve estar em _notes")


class TestWSP_M15_Supertrend(unittest.TestCase):
    """WSP_M15 deve usar SUPERTREND (sub-agente: 100% WR +R$174)."""

    def setUp(self):
        with open(CONFIG_PATH) as f:
            self.config = json.load(f)

    def test_wsp_m15_uses_supertrend(self):
        strat = self.config.get("strategy_by_tf", {}).get("WSP_M15")
        self.assertEqual(
            strat, "SUPERTREND",
            f"WSP_M15 deve ser SUPERTREND (sub-agente: 100% WR), "
            f"está '{strat}'"
        )


class TestWave85ConfigVersion(unittest.TestCase):
    """v >= 903 (Wave 8.5 incrementou)."""

    def test_version_after_wave_8_5(self):
        with open(CONFIG_PATH) as f:
            c = json.load(f)
        v = c.get("_version", 0)
        self.assertGreaterEqual(
            v, 903,
            f"Versão deve ser >= 903 (Wave 8.5), está v{v}"
        )


if __name__ == "__main__":
    unittest.main()
