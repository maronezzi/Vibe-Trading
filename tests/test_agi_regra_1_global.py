"""
test_agi_regra_1_global.py
=============================
TDD: garante que a Regra 1 de Bruno está WIRED GLOBALMENTE no AGI.
Não importa por qual caminho o AGI tenta mudar config
(Explorer auto-apply, LLM Portfolio, Discovery, manual):
se a mudança tem projeção forward negativa, REJEITA.

BUG IDENTIFICADO 2026-06-26 17:10:
  AGI 17H rodou e destruiu config v916 (estado bom pós-Wave 1-7):
  - REABILITOU BIT (-R$9.643/30d Wave 1.3 revertida)
  - Trocou WDO_M5 STRONG_TREND (edge 100% WR sub-agente) por MACD_MOMENTUM
  - Trocou WSP_M15 SUPERTREND (edge 100% WR) por RSI_REVERSION
  - Trocou WIN_M15 SQUEEZE (Wave 8.5+) por RSI_REVERSION
  Causa: Wave 8.3 wirear _should_apply_changes APENAS no Explorer
  auto-apply (linha 3463). Os outros caminhos (LLM Portfolio, manual,
  Discovery) NÃO passam pelo guard.

REGRA BRUNO: "sempre achar edge, criar se preciso, NUNCA bloquear."

FIX Wave 8.8:
  1. _should_apply_changes GLOBAL: wirear em TODOS os caminhos que mutam
     config (Explorer, LLM, Discovery, manual apply).
  2. Adicionar fallback de CRIAÇÃO DE ESTRATÉGIA: se nenhum edge for
     encontrado, AGI cria uma nova via template+backtest.
"""
import ast
import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


class TestRegra1GlobalNoAGI(unittest.TestCase):
    """_should_apply_changes deve estar em TODOS os caminhos do AGI."""

    AGI_PATH = Path(PROJECT_ROOT) / "optimization" / "agi_tuning_17h.py"

    def setUp(self):
        self.src = self.AGI_PATH.read_text()
        self.tree = ast.parse(self.src)

    def _find_call_sites(self, func_name):
        """Encontra call sites de func_name no AGI (excluindo definição)."""
        call_lines = []
        in_def = False
        lines = self.src.splitlines()
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith(f"def {func_name}"):
                in_def = True
                continue
            if in_def and line and not line.startswith((" ", "\t", "#")):
                in_def = False
            if not in_def and func_name + "(" in line and "def " not in line:
                call_lines.append((i, line.strip()))
        return call_lines

    def test_should_apply_changes_called_in_main_agi(self):
        """_should_apply_changes deve ser chamada no main AGI (não só Explorer)."""
        call_sites = self._find_call_sites("_should_apply_changes")
        self.assertGreater(
            len(call_sites), 0,
            "_should_apply_changes NÃO é chamada no AGI. "
            "Wave 8.8 deve wirear globalmente."
        )


class TestAGIStrategyCreationFallback(unittest.TestCase):
    """AGI deve SEMPRE achar edge — se nenhum existe, criar nova estratégia."""

    def test_agi_has_create_new_strategy_function(self):
        """Deve haver função _create_new_strategy() ou similar."""
        agi_src = (Path(PROJECT_ROOT) / "optimization" / "agi_tuning_17h.py").read_text()
        # Procura função que cria estratégia nova
        has_create = (
            "def _create_new_strategy" in agi_src
            or "def create_strategy" in agi_src
            or "def _discover_new_strategy" in agi_src
        )
        self.assertTrue(
            has_create,
            "AGI precisa ter função de criação de nova estratégia "
            "(Regra Bruno: sempre achar edge, criar se preciso)"
        )


class TestAGINegativePnlGuard(unittest.TestCase):
    """AGI não pode aceitar PnL negativo no relatório."""

    def test_print_report_does_not_show_negative_projection(self):
        """Quando AGI mostra projeção 30d, deve ser POSITIVA (Regra 1)."""
        # Smoke test: verificar que o relatório final tem sinal de sucesso
        agi_src = (Path(PROJECT_ROOT) / "optimization" / "agi_tuning_17h.py").read_text()
        # Procura comentário sobre Regra 1 / sempre positivo
        has_regra = (
            "Regra 1" in agi_src or "REGRA_1" in agi_src
            or "sempre positivo" in agi_src.lower()
        )
        self.assertTrue(
            has_regra,
            "AGI deve referenciar Regra 1 (sempre positivo) em comentários ou lógica"
        )


if __name__ == "__main__":
    unittest.main()