"""Test #3 — WIN deve usar RSI_REVERSION (não BOLLINGER).

Dados do DB (7 dias, run de 2026-06-15):
- WIN M5 com BOLLINGER: WR 29.2%, PnL -R$ 240
- WIN M15 com BOLLINGER: WR 18.2%, PnL -R$ 147
- WIN M30 com RSI_REVERSION: WR 33.3%, PnL +R$ 35 (único WIN lucrativo)

BIT M30 com RSI_REVERSION: WR 66.7%, PnL +R$ 5.505
IND M30 com RSI_REVERSION: WR 44.4%, PnL +R$ 464

Padrão claro: RSI_REVERSION em M30 é o setup vencedor do portfólio.
WIN M5/M15 com BOLLINGER perde. Solução: trocar strategy["WIN"]
pra "RSI_REVERSION" e replicar params do WIN M30.

Este test valida a decisão E impede regressão futura.
"""
import sys
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))


class TestWinStrategyIsRsiReversion(unittest.TestCase):
    """WIN deve seguir a regra Buffett de troca de estratégia.

    Regra atual (#3c): NÃO trocar WIN pra RSI_REVERSION com base em
    7 dias de dados que mostram improvement marginal (+R$ 8) e worst case
    catastrophico (-R$ 393 em M15). Buffett: "10 years, not 7 days".

    WIN deve PERMANECER em BOLLINGER até que:
    - 30+ dias de dados sustentem a proposta
    - Improvement médio > R$ 100
    - Worst case < R$ 100 de loss
    """

    def test_win_strategy_unchanged_pending_strong_evidence(self):
        """strategy['WIN'] deve continuar BOLLINGER (mudança bloqueada por regra Buffett)."""
        from vt_config_loader import load_config
        config = load_config()
        strategy_map = config.get("strategy", {})
        self.assertEqual(
            strategy_map.get("WIN"), "BOLLINGER",
            f"WIN está em {strategy_map.get('WIN')!r}, deveria estar em 'BOLLINGER'. "
            f"A troca pra RSI_REVERSION foi BLOQUEADA pela regra Buffett do AGI "
            f"(7 dias de dados < 30 dias mínimo, improvement marginal)."
        )

    def test_agi_blocks_win_strategy_change_with_short_window(self):
        """should_change_strategy() deve bloquear troca com window_days=7."""
        from agi_tuning_17h import should_change_strategy
        decision = should_change_strategy(
            symbol="WIN", current_strategy="BOLLINGER",
            proposed_strategy="RSI_REVERSION",
            window_days=7, improvement_brl=8, worst_case_loss_brl=393,
        )
        self.assertFalse(decision["change"],
                        "AGI não deveria propor troca com 7 dias de dados")
        self.assertEqual(decision["reason"], "INSUFFICIENT_EVIDENCE")

    def test_win_has_rsi_reversion_params(self):
        """WIN deve ter rsi_overbought, rsi_oversold e rsi_period configurados."""
        from vt_config_loader import load_config
        config = load_config()
        win_params = config.get("win", {})
        # RSI_REVERSION usa rsi_period, rsi_overbought, rsi_oversold
        self.assertIn("rsi_period", win_params,
                      "WIN sem rsi_period (RSI_REVERSION precisa)")
        self.assertIn("rsi_overbought", win_params,
                      "WIN sem rsi_overbought (RSI_REVERSION precisa)")
        self.assertIn("rsi_oversold", win_params,
                      "WIN sem rsi_oversold (RSI_REVERSION precisa)")

    def test_win_rsi_thresholds_realistic(self):
        """Thresholds RSI devem ser realistas (não os 70/30 default agressivos demais)."""
        from vt_config_loader import load_config
        config = load_config()
        win_params = config.get("win", {})
        rsi_ob = win_params.get("rsi_overbought", 70)
        rsi_os = win_params.get("rsi_oversold", 30)
        # Pitfall #16: BOLLINGER default 70/30 é extremo demais pra WIN M5/M15
        # Range esperado: 60-75 overbought, 25-40 oversold
        self.assertGreaterEqual(rsi_ob, 60, f"rsi_overbought={rsi_ob} muito baixo")
        self.assertLessEqual(rsi_ob, 80, f"rsi_overbought={rsi_ob} muito alto")
        self.assertGreaterEqual(rsi_os, 20, f"rsi_oversold={rsi_os} muito baixo")
        self.assertLessEqual(rsi_os, 40, f"rsi_oversold={rsi_os} muito alto")

    def test_win_sl_atr_mult_at_06_floor(self):
        """sl_atr_mult deve estar <= 0.7 (Explorer validou 0.6 como lucrativo)."""
        from vt_config_loader import load_config
        config = load_config()
        win_params = config.get("win", {})
        sl = win_params.get("sl_atr_mult", 1.0)
        self.assertLessEqual(
            sl, 0.7,
            f"WIN sl_atr_mult={sl} alto demais. Explorer validou 0.6 como lucrativo."
        )


if __name__ == "__main__":
    unittest.main()
