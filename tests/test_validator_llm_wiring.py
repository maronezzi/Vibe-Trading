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

    def test_first_provider_is_zenmux_free(self):
        """Wave 880.F (Bruno 07/08): primário do validator é o zenmux free
        (deepseek/deepseek-v4-flash-free), 1º da cadeia de uso LLM definida
        pelo Bruno. NÃO é mais o modelo global do config.yaml."""
        primary = self.v2._LLM_PROVIDERS[0]
        self.assertEqual(
            primary["provider"], "zenmux",
            f"Provider primário do validator ({primary['provider']}) ≠ "
            f"zenmux. Wave 880.F: cadeia definida pelo Bruno."
        )
        self.assertEqual(
            primary["model"], "deepseek/deepseek-v4-flash-free",
            f"Model primário do validator ({primary['model']}) ≠ "
            f"deepseek/deepseek-v4-flash-free."
        )

    def test_chain_order_is_bruno_defined(self):
        """Wave 880.F (Bruno 07/08): cadeia LLM na ordem:
        zenmux-free → zenmux-flash → alibaba-flash-0731 → qwen3.8-max.
        (deepseek-v4-pro REMOVIDO — Bruno 09/08.)"""
        expected = [
            ("zenmux", "deepseek/deepseek-v4-flash-free"),
            ("zenmux", "deepseek/deepseek-v4-flash"),
            ("alibaba-token-plan", "deepseek-v4-flash-0731"),
            ("alibaba-token-plan", "qwen3.8-max"),
        ]
        got = [(p["provider"], p["model"]) for p in self.v2._LLM_PROVIDERS]
        self.assertEqual(got, expected,
                         f"Cadeia LLM ≠ ordem definida pelo Bruno. Got: {got}")


if __name__ == "__main__":
    unittest.main()
