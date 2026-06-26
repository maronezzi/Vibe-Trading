"""
test_regra_1_only_apply_if_positive.py
========================================
TDD: garante que o AGI v3.1 aplica Regra 1 de Bruno:
"SEMPRE lucro positivo. Indicadores bons. Resultado ruim não é viável."

O AGI atual tem um bug perigoso: pode APLICAR mudanças mesmo com
projeção forward-looking NEGATIVA. Resultado: config fica pior que
o baseline, mas o AGI 'otimizou' (teatro de otimização).

FIX: introduzir guard `_should_apply_changes` que:
  - Calcula projeção forward (PnL/dia × 30) da config candidata
  - Se projeção < 0: REJEITA todas as mudanças (regra 1)
  - Se projeção > 0 MAS pior que baseline: rejeita (regra 1)
  - Se projeção > 0 E melhor que baseline: aplica (regra 1)

Wave 8.3 (2026-06-26, Bruno):
  'vc projeta lucro negativo? regra 1: sempre positivo. resultado ruim
  não é viável. crie novas, melhore existentes.'

Por que importa: o AGI atual pode MUDAR a config para algo com
projeção -R$736/mês e dizer 'convergiu ✅'. Regra 1 garante que
o sistema só evolui se a EVOLUÇÃO for POSITIVA.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


class TestRegra1GuardExists(unittest.TestCase):
    """A função _should_apply_changes existe e é callable."""

    def test_function_exists(self):
        from optimization.agi_tuning_17h import _should_apply_changes
        self.assertTrue(callable(_should_apply_changes))


class TestRegra1RejectsNegativeProjection(unittest.TestCase):
    """Regra 1: rejeita mudanças se projeção 30d é negativa."""

    def test_rejects_when_projection_negative(self):
        """
        Se candidate_projection_30d < 0 E baseline < 0, REJEITAR
        (pior que baseline). Se candidate < 0 E baseline >= 0, REJEITAR
        (regra 1: nunca negativo).
        """
        from optimization.agi_tuning_17h import _should_apply_changes
        # Caso 1: baseline positivo, candidate negativo (regressão)
        result = _should_apply_changes(
            current_projection_30d=+500.0,
            candidate_projection_30d=-100.0,  # REGRESSÃO a negativo
            current_pnl=+10.0,
            candidate_pnl=-2.0,
        )
        self.assertFalse(
            result["should_apply"],
            f"Regra 1: candidate negativo quando baseline positivo = REJEITAR, "
            f"got {result}"
        )
        # Caso 2: ambos negativos, candidate pior que baseline
        result = _should_apply_changes(
            current_projection_30d=-100.0,  # baseline negativo
            candidate_projection_30d=-200.0,  # candidate PIOR
            current_pnl=-2.0,
            candidate_pnl=-4.0,
        )
        self.assertFalse(
            result["should_apply"],
            f"Regra 1: ambos negativos, candidate pior = REJEITAR, got {result}"
        )

    def test_accepts_baseline_negative_to_positive(self):
        """
        PRIORIDADE: se baseline é negativo e candidate é positivo,
        ACEITAR (sair do vermelho). Regra 1 cumprida — é positivo.
        """
        from optimization.agi_tuning_17h import _should_apply_changes
        result = _should_apply_changes(
            current_projection_30d=-736.0,  # baseline negativo
            candidate_projection_30d=+500.0,  # candidate positivo
            current_pnl=-10.0,
            candidate_pnl=+5.0,
        )
        self.assertTrue(
            result["should_apply"],
            f"Regra 1: baseline negativo → candidate positivo DEVE ser aceito "
            f"(sair do vermelho), got {result}"
        )

    def test_rejects_when_candidate_worse_than_baseline(self):
        """
        Se candidate_projection_30d < current_projection_30d, REJEITAR.
        Regra 1: 'sempre melhor que antes'.
        """
        from optimization.agi_tuning_17h import _should_apply_changes
        result = _should_apply_changes(
            current_projection_30d=+500.0,
            candidate_projection_30d=+100.0,  # pior que baseline
            current_pnl=+10.0,
            candidate_pnl=+2.0,
        )
        self.assertFalse(
            result["should_apply"],
            f"Regra 1: candidate pior que baseline deve ser rejeitado, got {result}"
        )


class TestRegra1AcceptsPositiveImprovement(unittest.TestCase):
    """Regra 1: aceita SÓ se candidate > baseline E > 0."""

    def test_accepts_when_candidate_better_and_positive(self):
        """
        Caso ideal: candidate_projection_30d > current_projection_30d E
        ambos > 0. ACEITAR.
        """
        from optimization.agi_tuning_17h import _should_apply_changes
        result = _should_apply_changes(
            current_projection_30d=+500.0,
            candidate_projection_30d=+1000.0,  # 2x melhor
            current_pnl=+10.0,
            candidate_pnl=+20.0,
        )
        self.assertTrue(
            result["should_apply"],
            f"Regra 1: candidate 2x melhor deve ser aceito, got {result}"
        )

    def test_accepts_when_both_positive_even_small_improvement(self):
        """
        Pequena melhoria em cenário já positivo: ACEITAR.
        (qualquer melhoria sobre baseline positivo é OK)
        """
        from optimization.agi_tuning_17h import _should_apply_changes
        result = _should_apply_changes(
            current_projection_30d=+500.0,
            candidate_projection_30d=+550.0,  # +10% melhoria
            current_pnl=+10.0,
            candidate_pnl=+11.0,
        )
        self.assertTrue(
            result["should_apply"],
            f"Pequena melhoria em cenário positivo: ACEITAR, got {result}"
        )


class TestRegra1ReasonProvided(unittest.TestCase):
    """A função retorna motivo da decisão (audit trail)."""

    def test_rejection_includes_reason(self):
        """Rejeição vem com motivo textual."""
        from optimization.agi_tuning_17h import _should_apply_changes
        result = _should_apply_changes(
            current_projection_30d=+500.0,
            candidate_projection_30d=-100.0,
            current_pnl=+10.0,
            candidate_pnl=-2.0,
        )
        self.assertIn("reason", result)
        self.assertIn("Regra 1", result["reason"])

    def test_acceptance_includes_improvement_pct(self):
        """Aceitação inclui % de melhoria."""
        from optimization.agi_tuning_17h import _should_apply_changes
        result = _should_apply_changes(
            current_projection_30d=+500.0,
            candidate_projection_30d=+1000.0,
            current_pnl=+10.0,
            candidate_pnl=+20.0,
        )
        self.assertIn("improvement_pct", result)
        self.assertEqual(result["improvement_pct"], 100.0)  # +100%


if __name__ == "__main__":
    unittest.main()
