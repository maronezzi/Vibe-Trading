"""
test_strategy_changes_v899.py
=================================
TDD: garante que as trocas de estratégia validadas pelo AGI v899 dry-run
(com contratos REAIS) estão aplicadas em vt_config.json.

VALIDAÇÃO NO DB (30d, 2026-06-25):
  - WIN_M30 MACD_MOMENTUM: 5 trades | WR 40% | PnL R$ +352
  - WIN_M30 PIVOT_POINTS:  3 trades | WR 33% | PnL R$ -6,60
  → MACD_MOMENTUM é +R$358 melhor (n=5, evidência forte)

VALIDAÇÃO BACKTEST (dry-run v899, contratos reais):
  - WIN_M30 PIVOT_POINTS → MACD_MOMENTUM: PnL R$+352 | WR 40% | PF 3.8
  - WIN_M15 PIVOT_POINTS → BOLLINGER:  PnL R$+86  | WR 66.7% | PF 4.72
    (mas DB não tem BOLLINGER no WIN_M15 — só backtest, sem evidência)
  - WSP_M5 ADX_TREND → RSI_REVERSION: PnL R$+0.35 (marginal, n=13 → +R$12)

DECISÃO (defesa > sinal uniforme):
  - Aplica WIN_M30 → MACD_MENTUM (DB + backtest confirmam)
  - NÃO aplica WIN_M15 → BOLLINGER (DB não confirma, n=0 histórico)
  - NÃO aplica WSP_M5 → RSI_REVERSION (margem marginal, n pequeno)

NOTA: o config atual (v899) tem strategy_by_tf.WIN_M30 = "PIVOT_POINTS".
Após Wave 2.1, deve ser "MACD_MOMENTUM" com nota de justificativa.

Por que importa: WIN_M30 é um par com 8 trades 30d, +R$345 PnL. Trocar
a estratégia sem evidência DB+backtest é overfit. Esta troca tem as duas.
"""
import json
import os
import sys
import unittest

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)

CONFIG_PATH = os.path.join(PROJECT_ROOT, "vt_config.json")


class TestStrategyChangesV899(unittest.TestCase):
    """Garante que strategy_by_tf reflete trocas validadas DB+backtest."""

    def setUp(self):
        with open(CONFIG_PATH) as f:
            self.config = json.load(f)

    def test_win_m30_uses_macd_momentum(self):
        """WIN_M30 deve usar MACD_MOMENTUM (validado por DB+backtest)."""
        strat = self.config.get("strategy_by_tf", {}).get("WIN_M30")
        self.assertEqual(
            strat, "MACD_MOMENTUM",
            f"WIN_M30 deve ser MACD_MOMENTUM (DB+backtest confirmam), "
            f"está '{strat}'"
        )

    def test_win_m30_change_documented_in_notes(self):
        """Troca deve estar em _notes (audit trail)."""
        notes = self.config.get("_notes", "")
        self.assertIn(
            "WIN_M30", notes,
            "Troca WIN_M30 → MACD_MOMENTUM deve estar documentada em _notes"
        )
        self.assertIn(
            "MACD_MOMENTUM", notes,
            "Estratégia nova deve aparecer no _notes"
        )

    def test_win_m15_unchanged_pivot_points(self):
        """WIN_M15 PIVOT_POINTS é mantida (DB não tem BOLLINGER histórico)."""
        strat = self.config.get("strategy_by_tf", {}).get("WIN_M15")
        # Decisão defensiva: NÃO trocar WIN_M15 (DB não confirma BOLLINGER)
        self.assertEqual(
            strat, "PIVOT_POINTS",
            f"WIN_M15 deve ser PIVOT_POINTS (DB não confirma BOLLINGER), "
            f"está '{strat}'"
        )


class TestStrategyChangeIncreasesVersion(unittest.TestCase):
    """Garante que a mudança bumpa a versão."""

    def setUp(self):
        with open(CONFIG_PATH) as f:
            self.config = json.load(f)

    def test_config_version_increased(self):
        """A versão deve ser > v899 (Wave 1.3) — Wave 2.1 bumpa."""
        v = self.config.get("_version", 0)
        self.assertGreaterEqual(
            v, 900,
            f"Versão deve ser >= 900 (Wave 2.1 bumpa de v899), está v{v}"
        )

    def test_updated_by_is_wave_2(self):
        """_updated_by deve indicar Wave 2.1 (ou posterior)."""
        updated_by = self.config.get("_updated_by", "")
        # Aceita qualquer wave_2+ (mudou várias vezes desde então)
        self.assertTrue(
            "wave_2" in updated_by.lower() or "wave_4" in updated_by.lower() or "wave_8" in updated_by.lower(),
            f"_updated_by deve indicar wave_2/4/8, está '{updated_by}'"
        )


if __name__ == "__main__":
    unittest.main()
