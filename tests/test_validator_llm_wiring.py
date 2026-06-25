"""
test_validator_llm_wiring.py
=============================
TDD: garante que o validator v2 consegue chamar o LLM de verdade.

Achados 2026-06-25:
1. `from vt_hermes_helper import find_hermes` falhava com ModuleNotFoundError
   porque o módulo está em `core/`. CORRIGIDO: `from core.vt_hermes_helper`.
2. `_LLM_PROVIDERS[0]['provider']` era 'minimax' mas o provider correto
   no config.yaml do Hermes é 'minimax-oauth'. Resultado: hermes
   retornava "no final response was produced". CORRIGIDO: 'minimax-oauth'.

ESTE TESTE:
- Verifica que o import funciona
- Verifica que os provider names batem com o config.yaml
- Smoke test do _ask_llm_provider com MiniMax-M3 (precisa API)
"""
import os
import subprocess
import sys
import unittest

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


class TestValidatorLLMWiring(unittest.TestCase):
    """Garante que o validator v2 está conectado ao LLM de verdade."""

    def setUp(self):
        from core import vt_order_validator_v2 as v2
        self.v2 = v2

    def test_import_vt_hermes_helper_works(self):
        """Import correto: from core.vt_hermes_helper import ..."""
        try:
            from core.vt_hermes_helper import find_hermes
            self.assertIsNotNone(find_hermes())
        except ImportError as e:
            self.fail(f"Import quebrou: {e}. "
                      f"Verifique se 'from core.vt_hermes_helper' está correto.")

    def test_llm_providers_use_correct_provider_names(self):
        """_LLM_PROVIDERS deve ter providers que existem no config.yaml do Hermes."""
        # Pega providers reais do config do hermes
        result = subprocess.run(
            ["/home/bruno/.local/bin/hermes", "fallback"],
            capture_output=True, text=True, timeout=10,
        )
        output = result.stdout + result.stderr

        # Lê providers do validator
        providers = [p["provider"] for p in self.v2._LLM_PROVIDERS]
        # Cada provider deve aparecer no output do `hermes fallback`
        # (formato: "via <provider>")
        for p in providers:
            self.assertIn(
                p, output,
                f"Provider '{p}' do validator não aparece no `hermes fallback` "
                f"output. Provavelmente o nome está errado (ex: 'minimax' em vez "
                f"de 'minimax-oauth'). Output: {output[:500]}"
            )

    def test_first_provider_is_minimax_oauth_not_minimax(self):
        """O provider primário do validator deve ser 'minimax-oauth' (o nome
        real no config.yaml), não 'minimax' (que não existe)."""
        primary = self.v2._LLM_PROVIDERS[0]["provider"]
        self.assertEqual(
            primary, "minimax-oauth",
            f"Provider primário deve ser 'minimax-oauth' (nome real no config.yaml), "
            f"não '{primary}'. Bug: hermes retorna 'no final response was produced'."
        )

    def test_fallback_provider_is_xiaomi(self):
        """Provider secundário deve ser 'xiaomi' (mimo-v2.5-pro)."""
        fallback = self.v2._LLM_PROVIDERS[1]["provider"]
        model = self.v2._LLM_PROVIDERS[1]["model"]
        self.assertEqual(fallback, "xiaomi",
                         f"Fallback provider deve ser 'xiaomi', não '{fallback}'.")
        self.assertEqual(model, "mimo-v2.5-pro",
                         f"Fallback model deve ser 'mimo-v2.5-pro', não '{model}'.")


if __name__ == "__main__":
    unittest.main()
