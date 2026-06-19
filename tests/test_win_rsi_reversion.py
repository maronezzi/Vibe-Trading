"""Test: WIN strategy consolidation.

Histórico:
- 2026-06-15: WIN M5/M15 com BOLLINGER perdia (WR 29%/18%, PnL -R$ 240/-R$ 147).
  WIN M30 com RSI_REVERSION era o único lucrativo (WR 33%, +R$ 35).
- 2026-06-15: AGI testou trocar WIN para RSI_REVERSION (regra #3c Buffett).
  Bloqueado: improvement marginal (+R$ 8), worst case -R$ 393, < 30 dias.
- 2026-06-19: AGI consolida WIN com per-TF strategies (root=DONCHIAN_BREAKOUT,
  M5=MACD_MOMENTUM, M15=RSI_REVERSION, M30=MACD_MOMENTUM, H1=RSI_REVERSION).
  Esta é a configuração operacional final pós-remover IND/DOL.

Este test valida o estado atual E impede regressão futura: WIN root
permanece em DONCHIAN_BREAKOUT (estratégia robusta), per-TF conforme
acima, sl_atr_mult fica no nível validado pelo AGI (1.1 — Explorer
sugeriu 0.6 mas AGI rejeitou por amostra insuficiente).
"""
import sys
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))


class TestWinStrategyConsolidation(unittest.TestCase):
    """WIN consolidado em 2026-06-19 — root DONCHIAN_BREAKOUT + per-TF."""

    def test_win_root_strategy_is_donchian_breakout(self):
        """strategy['WIN'] (root) deve ser DONCHIAN_BREAKOUT (decisão AGI 19/06)."""
        from vt_config_loader import load_config
        config = load_config()
        self.assertEqual(
            config.get("strategy", {}).get("WIN"), "DONCHIAN_BREAKOUT",
            f"WIN root deveria ser DONCHIAN_BREAKOUT (AGI 19/06), "
            f"achou {config.get('strategy', {}).get('WIN')!r}. "
            f"Troca pra RSI_REVERSION foi BLOQUEADA pela regra Buffett."
        )

    def test_win_per_tf_strategies(self):
        """strategy_by_tf por TF (consolidação AGI 19/06)."""
        from vt_config_loader import load_config
        config = load_config()
        sb = config.get("strategy_by_tf", {})
        expected = {
            "WIN_M5": "MACD_MOMENTUM",
            "WIN_M15": "RSI_REVERSION",
            "WIN_M30": "MACD_MOMENTUM",
            "WIN_H1": "RSI_REVERSION",
        }
        for pair, expected_strat in expected.items():
            self.assertEqual(
                sb.get(pair), expected_strat,
                f"{pair} deveria ser {expected_strat}, achou {sb.get(pair)!r}"
            )

    def test_agi_blocks_win_strategy_change_with_short_window(self):
        """should_change_strategy() deve bloquear troca com window_days=7.

        Pino histórico: AGI rejeita WIN→RSI_REVERSION com 7d dados
        (improvement marginal + worst case ruim). Regra Buffett
        ("10 years, not 7 days").
        """
        from agi_tuning_17h import should_change_strategy
        decision = should_change_strategy(
            symbol="WIN", current_strategy="DONCHIAN_BREAKOUT",
            proposed_strategy="RSI_REVERSION",
            window_days=7, improvement_brl=8, worst_case_loss_brl=393,
        )
        self.assertFalse(decision["change"],
                        "AGI não deveria propor troca com 7 dias de dados")
        self.assertEqual(decision["reason"], "INSUFFICIENT_EVIDENCE")

    def test_win_has_rsi_params_for_per_tf_use(self):
        """WIN root deve ter rsi_period/overbought/oversold (per-TF RSI_REVERSION)."""
        from vt_config_loader import load_config
        config = load_config()
        win_params = config.get("win", {})
        self.assertIn("rsi_period", win_params,
                      "WIN sem rsi_period (per-TF RSI_REVERSION precisa)")
        self.assertIn("rsi_overbought", win_params,
                      "WIN sem rsi_overbought (per-TF RSI_REVERSION precisa)")
        self.assertIn("rsi_oversold", win_params,
                      "WIN sem rsi_oversold (per-TF RSI_REVERSION precisa)")

    def test_win_rsi_thresholds_realistic(self):
        """Thresholds RSI devem ser realistas (range 60-80 ob, 20-40 os)."""
        from vt_config_loader import load_config
        config = load_config()
        win_params = config.get("win", {})
        rsi_ob = win_params.get("rsi_overbought", 70)
        rsi_os = win_params.get("rsi_oversold", 30)
        self.assertGreaterEqual(rsi_ob, 60, f"rsi_overbought={rsi_ob} muito baixo")
        self.assertLessEqual(rsi_ob, 80, f"rsi_overbought={rsi_ob} muito alto")
        self.assertGreaterEqual(rsi_os, 20, f"rsi_oversold={rsi_os} muito baixo")
        self.assertLessEqual(rsi_os, 40, f"rsi_oversold={rsi_os} muito alto")

    def test_win_sl_atr_mult_at_current_validated_level(self):
        """sl_atr_mult WIN no nível 1.1 (AGI validou; Explorer sugeriu 0.6 mas
        improvement marginal não justificou troca — regra Buffett).

        Este teste documenta a decisão vigente: NÃO é 0.6 (Explorer) nem
        0.7 (limite antigo), e sim 1.1 (validado pelo AGI 17h com base
        em dados de 7d pós-experimento).
        """
        from vt_config_loader import load_config
        config = load_config()
        win_params = config.get("win", {})
        sl = win_params.get("sl_atr_mult")
        self.assertEqual(
            sl, 1.1,
            f"WIN sl_atr_mult deveria estar em 1.1 (decisão AGI vigente). "
            f"Achou {sl}. Para mudar, AGI precisa validar com ≥ 30d de "
            f"evidência e improvement material."
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
