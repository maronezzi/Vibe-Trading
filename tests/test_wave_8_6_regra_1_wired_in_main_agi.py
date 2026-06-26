"""
test_wave_8_6_regra_1_wired_in_main_agi.py
===========================================
TDD: garante que a Regra 1 de Bruno está WIRED no fluxo principal
do AGI v3.1.

Wave 8.6 (2026-06-26, Bruno):
  'SEMPRE lucro positivo. Indicadores bons. Resultado ruim não é viável.'

Wave 8.3 criou _should_apply_changes() mas NÃO está wireada no
fluxo principal do AGI. Resultado: o AGI ainda pode aplicar
mudanças com projeção forward negativa.

FIX: wirear no caminho 'auto-apply Explorer' (L3463-3471):
  - Calcular projeção forward antes de aplicar
  - Se Regra 1 falhar: REJEITAR (não salva no config)
  - Log explícito: '[REGRA 1] Rejeitado: <motivo>'

VALIDAÇÃO:
- RED: hoje o auto-apply acontece sem checar Regra 1
- GREEN: depois do fix, auto-apply só acontece se Regra 1 passa
"""
import ast
import os
import sys
import unittest

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


class TestRegra1WiredInMainAGI(unittest.TestCase):
    """_should_apply_changes deve ser chamado no fluxo do AGI principal."""

    def test_should_apply_changes_called_in_agi_tuning(self):
        """
        O arquivo optimization/agi_tuning_17h.py DEVE chamar
        _should_apply_changes em algum lugar do fluxo (não só definir).
        """
        src_path = os.path.join(
            PROJECT_ROOT, "optimization", "agi_tuning_17h.py"
        )
        src = open(src_path).read()

        # Conta referências à função (definição + uso)
        # Exclui a definição (linha que começa com 'def _should_apply_changes')
        lines = src.splitlines()
        in_def = False
        call_count = 0
        for line in lines:
            stripped = line.strip()
            # Pula linhas de definição (def _should_apply_changes(...):)
            if stripped.startswith("def _should_apply_changes"):
                in_def = True
                continue
            # Sai do def quando indent volta a 0 (próxima função/classe)
            if in_def and line and not line.startswith(" ") and not line.startswith("\t") and not line.startswith("#"):
                in_def = False
            # Conta qualquer outra referência que NÃO seja definição
            if not in_def and "_should_apply_changes" in line and "def " not in line:
                call_count += 1

        self.assertGreater(
            call_count, 0,
            f"_should_apply_changes() não é referenciada em agi_tuning_17h.py "
            f"fora da definição. Wave 8.6 precisa wirear. "
            f"Encontradas {call_count} referências no fluxo."
        )


class TestRegra1IntegrationWithAutoApply(unittest.TestCase):
    """Testa que a função pode ser chamada dentro do contexto do AGI."""

    def test_should_apply_changes_rejects_negative_in_agi_context(self):
        """
        Simula o cenário do bug: AGI sugere mudança com projeção
        -R$736/mês. _should_apply_changes DEVE rejeitar.
        """
        from optimization.agi_tuning_17h import _should_apply_changes
        # Baseline: -R$736/mês (pós-Wave atual)
        # Candidate: -R$1500/mês (Pior — o que AGI sugeriu)
        result = _should_apply_changes(
            current_projection_30d=-736.0,
            candidate_projection_30d=-1500.0,
        )
        self.assertFalse(
            result["should_apply"],
            "Regra 1: candidate -R$1500 (pior que baseline -R$736) deve ser rejeitado"
        )


if __name__ == "__main__":
    unittest.main()
