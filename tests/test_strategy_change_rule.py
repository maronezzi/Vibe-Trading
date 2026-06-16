"""Test #3c — Buffett rule: NÃO trocar com dados fracos.

A versão original de #3 (trocar WIN pra RSI_REVERSION) era baseada em
7 dias de dados. Warren Buffett nunca compraria uma empresa baseado em
1 semana de cotação. Mesma lógica aqui: o backtest de 7 dias mostrou
que RSI é marginalmente melhor em M5 (+R$ 8) e MUITO pior em M15
(-R$ 393). Conclusion: dados insuficientes pra mudar produção.

Estes tests validam que o AGI NÃO troca estratégia WIN automaticamente
com base em dados < 30 dias. Para trocar, precisa de:
- 30+ dias de dados
- Melhoria consistente > R$ 100 (não R$ 8)
- WR estável > 50%
"""
import sys
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))


class TestBuffettNoChangeRule(unittest.TestCase):
    """AGI não deve trocar estratégia WIN com evidência fraca."""

    def test_win_strategy_unchanged_after_short_window(self):
        """Com 7 dias de dados mostrando melhoria < R$ 100, WIN deve manter BOLLINGER."""
        # Este test valida a regra de decisão, não a config atual
        # (a config pode ser mudada manualmente após análise humana)
        from agi_tuning_17h import should_change_strategy

        # Dados: 7 dias, RSI melhor que BOLLINGER por R$ 8 (M5), pior por R$ 393 (M15)
        decision = should_change_strategy(
            symbol="WIN",
            current_strategy="BOLLINGER",
            proposed_strategy="RSI_REVERSION",
            window_days=7,
            improvement_brl=8,        # M5: marginal
            worst_case_loss_brl=393,  # M15: catastrophico
        )
        # Buffett: improvement < R$ 100 E worst case > R$ 100 → NÃO troca
        self.assertFalse(
            decision["change"],
            f"AGI propôs trocar WIN de BOLLINGER pra RSI_REVERSION com "
            f"improvement=R$ 8 e worst case=-R$ 393. Buffett não faria essa troca."
        )
        self.assertEqual(decision["reason"], "INSUFFICIENT_EVIDENCE")
        self.assertGreater(decision["recommended_window_days"], 7)

    def test_win_strategy_changes_with_strong_evidence(self):
        """Com 30+ dias E improvement > R$ 100, AGI pode propor troca."""
        from agi_tuning_17h import should_change_strategy

        decision = should_change_strategy(
            symbol="WIN",
            current_strategy="BOLLINGER",
            proposed_strategy="RSI_REVERSION",
            window_days=30,
            improvement_brl=350,       # improvement consistente
            worst_case_loss_brl=50,    # sem regressão catastrófica
        )
        # Buffett: improvement > R$ 100 E worst case pequeno → considera trocar
        self.assertTrue(
            decision["change"],
            f"AGI deveria propor troca com improvement=R$ 350 sobre 30 dias. "
            f"Got: {decision}"
        )

    def test_worst_case_loss_blocks_change_even_with_high_average(self):
        """Se alguma condição (ex: outro TF) é catastrophicamente pior, NÃO troca."""
        from agi_tuning_17h import should_change_strategy

        decision = should_change_strategy(
            symbol="WIN",
            current_strategy="BOLLINGER",
            proposed_strategy="RSI_REVERSION",
            window_days=30,
            improvement_brl=500,        # média boa
            worst_case_loss_brl=400,    # MAS um caso isolado é -R$ 400
        )
        # Buffett Rule #1: don't lose money. Worst case > R$ 100 = blocker
        self.assertFalse(
            decision["change"],
            "AGI não deve propor troca quando há um caso isolado com loss > R$ 100. "
            "Buffett Rule #1: don't lose money."
        )
        self.assertEqual(decision["reason"], "WORST_CASE_RISK")


if __name__ == "__main__":
    unittest.main()
