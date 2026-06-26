"""
test_block_loss_day_direction_wired.py
========================================
TDD: garante que _is_blocked_day_direction() está WIREADA no fluxo
de execução do autotrader — antes de enviar ordem pro broker.

Wave 3.2 (2026-06-26): a função existe (Wave 3.1) mas não é chamada
em lugar nenhum. Sem este wirear, o impacto de -R$9.722/30d dos
padrões quarta-BUY / terça-SELL não é bloqueado de fato.

FIX: integrar _is_blocked_day_direction() no caminho de execução
(check_and_trade ou execute_signal) — antes de safe_buy/safe_sell.

Por que importa: sem este wirear, todo o trabalho de Wave 3.1
(RED+GREEN de _is_blocked_day_direction) é inerte. A função
passa no teste mas não tem efeito no live.
"""
import ast
import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


class TestIsBlockedDayDirectionWired(unittest.TestCase):
    """Garante via AST que _is_blocked_day_direction é chamado no fluxo."""

    def test_function_is_called_in_vt_autotrader(self):
        """vt_autotrader.py deve chamar _is_blocked_day_direction() em algum lugar."""
        src_path = Path(PROJECT_ROOT, "core", "vt_autotrader.py")
        src = src_path.read_text()

        # Conta quantas vezes a função é REFERENCIADA (definição + uso)
        # Excluindo a definição (def _is_blocked_day_direction)
        lines = src.splitlines()
        call_count = 0
        in_def = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("def _is_blocked_day_direction"):
                in_def = True
                continue
            if in_def:
                # Sai do def quando indent volta a 0
                if line and not line.startswith(" ") and not line.startswith("\t"):
                    in_def = False
            # Conta chamadas (não definição)
            if not in_def and "_is_blocked_day_direction(" in line:
                # Exclui a definição que pode estar numa linha só
                if "def " not in line and "DEFAULT" not in line:
                    call_count += 1

        self.assertGreater(
            call_count, 0,
            f"_is_blocked_day_direction() não é chamada em vt_autotrader.py. "
            f"Wave 3.2 precisa wirear antes de enviar ordem. "
            f"Local: def na linha que começa com 'def _is_blocked_day_direction'"
        )

    def test_function_checked_before_executing_order(self):
        """A checagem deve estar ANTES de safe_buy/safe_sell (executar ordem)."""
        src_path = Path(PROJECT_ROOT, "core", "vt_autotrader.py")
        src = src_path.read_text()
        # Encontra a primeira ocorrência de uso de _is_blocked_day_direction
        # e verifica que está ANTES de qualquer safe_buy/safe_sell
        first_block_idx = src.find("_is_blocked_day_direction(")
        # Procura DEPOIS da definição (pula a linha def)
        def_idx = src.find("def _is_blocked_day_direction")
        if first_block_idx > 0 and first_block_idx < def_idx:
            first_block_idx = src.find("_is_blocked_day_direction(", def_idx)

        # Procura primeira chamada safe_buy
        first_safe_buy = src.find("safe_buy(")
        first_safe_sell = src.find("safe_sell(")
        first_order = min(
            x for x in [first_safe_buy, first_safe_sell] if x > 0
        ) if (first_safe_buy > 0 or first_safe_sell > 0) else 99999

        # Aceita que block_check esteja próximo (antes ou na mesma função)
        # mas não depois
        self.assertLess(
            first_block_idx, first_order,
            f"_is_blocked_day_direction está em L{first_block_idx} mas "
            f"safe_buy/safe_sell está em L{first_order} — check deve ser ANTES"
        )


if __name__ == "__main__":
    unittest.main()
