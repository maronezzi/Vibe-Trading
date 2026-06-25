"""
test_autotrader_emergency_wiring.py
====================================
TDD RED phase: garante que core/vt_autotrader.py chama
safe_modify_sl_with_emergency_close() em TODOS os call sites de
modify_sl, não safe_modify_sl() diretamente.

CONTEXTO (2026-06-24 safety audit):
- core/vt_emergency.py:251 implementa safe_modify_sl_with_emergency_close()
  com a regra WSPU26: "se modify_sl falhou e PnL contra, fecha a mercado".
- 12 testes em test_emergency_close.py validam o wrapper isolado.
- BUG REAL: o autotrader chama safe_modify_sl() direto em 5 sites
  (L1158, L1217, L1454, L1465, L1534), nunca passa pelo wrapper.
- Resultado: a regra WSPU26 não está protegendo produção.

ESTE TESTE:
- Faz source-level AST scan de core/vt_autotrader.py
- Verifica que cada chamada a safe_modify_sl( é roteada via wrapper
- Verifica que o import do wrapper existe no topo do arquivo
- Falha CLARAMENTE listando os call sites não-wrapped

NOTA: este é um teste de "fio" (wiring) — checa a estrutura do código
para garantir que mudanças futuras não voltem a usar safe_modify_sl()
direto. NÃO executa o autotrader.
"""
import ast
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTOTRADER_PATH = os.path.join(PROJECT_ROOT, "core", "vt_autotrader.py")


def _get_source():
    """Lê o source de core/vt_autotrader.py como string UTF-8."""
    with open(AUTOTRADER_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _get_tree():
    """Faz parse AST do source do autotrader."""
    return ast.parse(_get_source())


def _find_all_calls(tree):
    """Retorna lista de (linha, nome_chamada, parent_func) para todas as Call no módulo."""
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # nome da função chamada
            name = None
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name:
                calls.append((node.lineno, name))
    return calls


def _find_imported_names(tree):
    """Retorna set de nomes importados no módulo (import X, from Y import X)."""
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.add(alias.asname or alias.name)
    return imported


class TestAutotraderEmergencyWiring(unittest.TestCase):
    """Garante que toda modify_sl no autotrader passa pelo wrapper de emergency close."""

    def setUp(self):
        self.tree = _get_tree()
        self.source = _get_source()
        self.imports = _find_imported_names(self.tree)
        self.calls = _find_all_calls(self.tree)
        self.safe_modify_sl_calls = [
            (lineno, name) for lineno, name in self.calls
            if name == "safe_modify_sl"
        ]

    def test_autotrader_imports_emergency_wrapper(self):
        """O módulo precisa importar safe_modify_sl_with_emergency_close."""
        self.assertIn(
            "safe_modify_sl_with_emergency_close",
            self.imports,
            "core/vt_autotrader.py NÃO importa safe_modify_sl_with_emergency_close. "
            "Adicione `from core.vt_emergency import safe_modify_sl_with_emergency_close` "
            "no bloco de imports."
        )

    def test_autotrader_does_not_call_safe_modify_sl_directly(self):
        """
        Cada chamada a safe_modify_sl() no autotrader deveria ser
        safe_modify_sl_with_emergency_close(). Reporta todos os sites.
        """
        if not self.safe_modify_sl_calls:
            self.skipTest(
                "Sem call sites safe_modify_sl() no autotrader — wrap completo, "
                "validação feita por test_wrapper_calls_have_no_remaining_safe_modify_sl. "
                "Este teste documenta o estado esperado (zero ocorrências)."
            )
        lines = "\n".join(f"  L{ln}: {n}()" for ln, n in self.safe_modify_sl_calls)
        self.fail(
            f"Autotrader chama safe_modify_sl() direto (NÃO passa pelo wrapper de emergency):\n"
            f"{lines}\n\n"
            f"Ação: substituir cada `safe_modify_sl(...)` por "
            f"`safe_modify_sl_with_emergency_close(...)` em core/vt_autotrader.py."
        )

    def test_wrapper_calls_count_matches_expected(self):
        """Deve haver EXATAMENTE 5 call sites wrapped (mesmo número que tinha safe_modify_sl)."""
        wrapper_calls = [
            (ln, name) for ln, name in self.calls
            if name == "safe_modify_sl_with_emergency_close"
        ]
        self.assertEqual(
            len(wrapper_calls), 5,
            f"Esperado 5 call sites de safe_modify_sl_with_emergency_close "
            f"(correspondente aos 5 sites safe_modify_sl originais), "
            f"encontrado {len(wrapper_calls)}.\n"
            f"Sites: {wrapper_calls}"
        )

    def test_wrapper_calls_have_no_remaining_safe_modify_sl(self):
        """Sanity: depois do wrap, não pode haver safe_modify_sl() solto."""
        self.assertEqual(
            len(self.safe_modify_sl_calls), 0,
            f"Após o wrap, safe_modify_sl() não deve aparecer mais no autotrader. "
            f"Sites restantes: {self.safe_modify_sl_calls}"
        )


if __name__ == "__main__":
    unittest.main()
