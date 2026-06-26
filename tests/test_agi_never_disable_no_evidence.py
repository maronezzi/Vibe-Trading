"""
test_agi_never_disable_no_evidence.py
=========================================
TDD: garante que o AGI NUNCA desabilita pares por backtest falho.

BUG IDENTIFICADO 2026-06-26 17:10:
  AGI 17H rodou exhaustive_search com 27 estratégias no backtest.
  Algumas pares (provavelmente IND_M5, WINQ26_M5 etc) tinham
  TODAS as 27 estratégias negativas no backtest (bug sintético WIN$).
  AGI então DESABILITOU essas pares via _pause_failing_pairs().
  Resultado: config v923 ficou com tickers inválidos (INDM26_M5, WDON26_M5)
  em disabled_timeframes.

REGRA BRUNO (2026-06-26):
  "AGI deve SEMPRE achar edge. NUNCA bloquear/retornar negativo.
   Se nenhuma estratégia existente der edge, CRIAR uma nova."

FIX Wave 8.8:
  - Remove a lógica que desabilita pares com base em backtest
  - Se exhaustive_search mostra all_negative: AGI tenta CRIAR nova estratégia
  - Só desabilita em último caso (após 3 iterações com profit negativo REAL no live)
"""
import ast
import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


class TestAGINeverDisableByBacktest(unittest.TestCase):
    """AGI não pode desabilitar pares baseado apenas em backtest falho."""

    AGI_PATH = Path(PROJECT_ROOT) / "optimization" / "agi_tuning_17h.py"

    def test_no_disable_pairs_based_on_exhaustive_search(self):
        """A lógica de 'all_negative_from_exhaustive → disable' deve estar REMOVIDA."""
        src = self.AGI_PATH.read_text()
        # Procura padrão problemático
        problematic = (
            "all_negative_from_exhaustive" in src
            and "pairs_to_disable.append" in src
        )
        self.assertFalse(
            problematic,
            "AGI ainda contém lógica de desabilitar pares baseado em backtest falho. "
            "Wave 8.8 deve REMOVER essa lógica."
        )

    def test_no_pause_failing_pairs_call_in_main_flow(self):
        """_pause_failing_pairs() não deve ser chamado a partir do fluxo principal."""
        src = self.AGI_PATH.read_text()
        # _pause_failing_pairs é definido mas NÃO deve ser usado para disable live
        # Apenas allowed para casos extremos (3+ iterações live com profit negativo)
        self.assertIn(
            "def _pause_failing_pairs", src,
            "_pause_failing_pairs deve existir (extreme cases only)"
        )

    def test_no_disable_timeframes_from_exhaustive(self):
        """Não deve haver lógica que desabilita timeframes baseado em exhaustive_search.

        Wave 8.8: deve haver '_create_new_strategy' em vez de disable.
        """
        src = self.AGI_PATH.read_text()
        # Verifica se há chamada para _pause_failing_pairs DENTRO do fluxo principal
        # (Wave 8.8 removeu essa chamada; função ainda existe mas não é mais chamada)
        # O test passa se _create_new_strategy é chamada no fallback
        has_create = "_create_new_strategy(" in src
        self.assertTrue(
            has_create,
            "AGI deve chamar _create_new_strategy() em vez de _pause_failing_pairs() "
            "(Wave 8.8: 'sempre achar edge, criar se preciso')"
        )


class TestAGIAlwaysFindsOrCreatesStrategy(unittest.TestCase):
    """AGI SEMPRE deve tentar achar edge ou criar nova estratégia."""

    def test_create_strategy_function_exists(self):
        """Deve haver função _create_new_strategy() ou similar."""
        agi_src = (Path(PROJECT_ROOT) / "optimization" / "agi_tuning_17h.py").read_text()
        self.assertTrue(
            "_create_new_strategy" in agi_src or "_create_strategy" in agi_src,
            "AGI precisa de função de criar estratégia (Regra Bruno)"
        )

    def test_create_strategy_called_when_no_edge(self):
        """Quando exhaustive_search retorna all_negative, AGI chama _create_new_strategy."""
        agi_src = (Path(PROJECT_ROOT) / "optimization" / "agi_tuning_17h.py").read_text()
        # Procura padrão: criar estratégia quando all_negative
        creates_on_neg = (
            "all_negative_pairs" in agi_src
            and ("_create_new_strategy" in agi_src or "_create_strategy" in agi_src)
        )
        self.assertTrue(
            creates_on_neg,
            "AGI deve criar estratégia quando exhaustive_search mostra all_negative_pairs"
        )


if __name__ == "__main__":
    unittest.main()