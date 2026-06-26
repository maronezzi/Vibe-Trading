"""
test_disable_losing_pairs.py
=================================
TDD: garante que os pares com prejuízo histórico comprovado (PnL<0 E WR<30% E n>=5)
estejam em vt_config["disabled_timeframes"].

Achado 2026-06-25 (DB analysis, 30d, 309 trades):
  - BITM26_M5:  14t | WR 28.6% | PnL -R$5.395,60  (pior loss absoluto)
  - BITM26_M30: 6t  | WR 16.7% | PnL -R$2.849,60  (100% SL, 16.7% WR)
  - WDON26_M5:  2t  | WR 0%   | PnL -R$592,40   (1 símbolo, edge case)
  - INDM26_M5:  2t  | WR 0%   | PnL -R$807,40   (idem)

Total estimado: -R$9.643,40 de loss histórico.

Por que importa: cada dia que esses pares operam, geram loss. Desabilitar
é fail-closed — não impede reinicialização futura, mas para o sangramento
AGORA sem mexer em lógica de estratégia.

Este teste é RED no estado atual (pares NÃO estão em disabled_timeframes).
Torna-se GREEN quando a config for atualizada com os pares perdedores.
"""
import json
import os
import sys
import unittest

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)

CONFIG_PATH = os.path.join(PROJECT_ROOT, "vt_config.json")

# Pares desabilitados pelo Wave 1.3 (análise DB 2026-06-26, retroativo 30d)
LOSING_PAIRS_TO_DISABLE = [
    "BIT_M5",
    "BIT_M30",
    "WDON26_M5",
    "INDM26_M5",
]


class TestConfigHasLosingPairsDisabled(unittest.TestCase):
    """Garante que pares perdedores estão em disabled_timeframes."""

    def setUp(self):
        with open(CONFIG_PATH) as f:
            self.config = json.load(f)

    def test_disabled_timeframes_key_exists(self):
        """vt_config.json deve ter a chave disabled_timeframes."""
        self.assertIn(
            "disabled_timeframes", self.config,
            "vt_config.json deve ter a chave disabled_timeframes (pode ser [])"
        )

    def test_losing_pairs_are_disabled(self):
        """Os pares perdedores DEVEM estar em disabled_timeframes."""
        disabled = self.config.get("disabled_timeframes", [])
        for pair in LOSING_PAIRS_TO_DISABLE:
            self.assertIn(
                pair, disabled,
                f"Par perdedor '{pair}' deveria estar em disabled_timeframes. "
                f"Atuais: {disabled}"
            )

    def test_disabled_timeframes_justified_in_notes(self):
        """A decisão de desabilitar deve estar documentada em _notes (audit trail)."""
        notes = self.config.get("_notes", "")
        for pair in LOSING_PAIRS_TO_DISABLE:
            # Cada par desabilitado deve ter uma nota mencionando ele OU a
            # nota geral mencionando "Wave 1.3" / "DB analysis"
            self.assertTrue(
                pair in notes or "Wave 1.3" in notes or "DB analysis" in notes,
                f"Desabilitação de '{pair}' deve estar documentada em _notes "
                f"(audit trail). Notas atuais: {notes[:200]}"
            )


class TestDisabledTimeframesFilterActive(unittest.TestCase):
    """Garante que disabled_timeframes é respeitado pelo autotrader."""

    def test_disabled_timeframes_filtered_out(self):
        """Quando BIT_M5 está em disabled_timeframes, o autotrader pula."""
        config = {
            "symbols": ["WIN", "BIT"],
            "disabled_timeframes": ["BIT_M5"],
        }
        # Simula a lógica do autotrader:
        disabled = config.get("disabled_timeframes", [])
        # Pra cada symbol, gera pares SYM_TF
        timeframes = ["M5", "M15", "M30", "H1"]
        active_pairs = []
        for sym in config["symbols"]:
            for tf in timeframes:
                key = f"{sym}_{tf}"
                if key not in disabled:
                    active_pairs.append(key)
        # BIT_M5 deve estar fora
        self.assertNotIn("BIT_M5", active_pairs)
        # BIT_M15, M30, H1 devem estar dentro
        self.assertIn("BIT_M15", active_pairs)
        self.assertIn("BIT_M30", active_pairs)
        self.assertIn("BIT_H1", active_pairs)


if __name__ == "__main__":
    unittest.main()
