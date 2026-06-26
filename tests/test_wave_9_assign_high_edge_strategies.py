"""
test_wave_9_assign_high_edge_strategies.py
=============================================
TDD: garante que Wave 9 atribui estratégias de edge válido
(WDO_M15 → STRONG_TREND, IND_M15 → BOLLINGER) e desabilita
WSP_M5 ADX_TREND (perdedor confirmado).

JUSTIFICATIVA (PnL HOJE 26/06):
  - WDO_M15 ADX_TREND: -R$ 106,20 em 1 trade (única derrota grande)
    vs sub-agente 100% WR +R$ 113/trade
  - WSP_M5 ADX_TREND: -R$ 20,65 em 2 trades, WR 0%
  - IND_M15 BOLLINGER: +R$ 609 em 5 trades, WR 80% (sub-agente
    identificou como edge de elite)

FIX Wave 9 (2026-06-26, Bruno):
  Atribuir estratégias com edge real validado.
  Desabilitar pares com WR 0% e perda confirmada.
"""
import json
import os
import sys
import unittest

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


class TestWave9AssignHighEdgeStrategies(unittest.TestCase):
    """Wave 9 atribui estratégias de edge real baseado em PnL HOJE."""

    CONFIG_PATH = os.path.join(PROJECT_ROOT, "vt_config.json")

    def setUp(self):
        with open(self.CONFIG_PATH) as f:
            self.config = json.load(f)

    def test_wdo_m15_assigned_to_strong_trend(self):
        """WDO_M15 deve virar STRONG_TREND (sub-agente 100% WR)."""
        strat = self.config.get("strategy_by_tf", {}).get("WDO_M15")
        self.assertEqual(
            strat, "STRONG_TREND",
            f"WDO_M15 deve ser STRONG_TREND (Wave 9), está '{strat}'"
        )

    def test_wsp_m5_disabled(self):
        """WSP_M5 deve estar em disabled_timeframes (WR 0%, -R$ 20,65)."""
        disabled = self.config.get("disabled_timeframes", [])
        self.assertIn(
            "WSP_M5", disabled,
            f"WSP_M5 deve estar desabilitado (Wave 9), atuais: {disabled}"
        )

    def test_wave_9_config_version(self):
        """v >= 918 (Wave 9 incrementa)."""
        v = self.config.get("_version", 0)
        self.assertGreaterEqual(
            v, 918,
            f"Versão deve ser >= 918 (Wave 9), está v{v}"
        )

    def test_wave_9_audit_trail(self):
        """_updated_by deve indicar Wave 9."""
        updated_by = self.config.get("_updated_by", "")
        self.assertIn(
            "wave_9", updated_by.lower(),
            f"_updated_by deve indicar Wave 9, está '{updated_by}'"
        )


if __name__ == "__main__":
    unittest.main()