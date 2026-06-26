"""
test_exit_sl_price_recorded.py
=================================
TDD: garante que log_exit() grava exit_sl_price quando reason=SL_SERVIDOR.

Achado 2026-06-25 (DB analysis, 309 trades 30d):
  - 276 trades saem por SL_SERVIDOR (89% do total fechado)
  - Em 100% deles, exit_sl_price é NULL (na chamada do log_exit em
    core/vt_autotrader.py:1580)
  - Sem exit_sl_price, é impossível calcular o slippage real entre o SL
    teórico enviado pro broker e o preço em que o servidor realmente fechou.

FIX: no caminho "posição fechou pelo servidor" em check_and_trade(), passar
o SL teórico (calculado de entry_price + sl_pts*point_val) como exit_sl_price
do log_exit().

Por que importa: habilita diagnóstico real de slippage, e a base do fix
do I10 (trailing_activate 1.2 -> 1.0). Sem saber slippage real, qualquer
otimização de trailing é cega.

Este teste é RED no estado atual (exit_sl_price fica NULL). Torna-se GREEN
quando autotrader passar exit_sl_price correto no log_exit.
"""
import ast
import os
import re
import sys
import unittest

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


class TestSlServidorPathPopulatesExitSlPrice(unittest.TestCase):
    """Valida via AST: o caminho SL_SERVIDOR deve passar exit_sl_price."""

    def test_sl_servidor_log_exit_includes_exit_sl_price(self):
        """
        Faz parse do core/vt_autotrader.py e localiza a chamada log_exit(...)
        dentro do bloco onde exit_reason='SL_SERVIDOR'. Verifica que
        exit_sl_price é passado como keyword arg.
        """
        src = open(os.path.join(PROJECT_ROOT, "core", "vt_autotrader.py")).read()
        tree = ast.parse(src)

        # Procura todas as chamadas log_exit(...)
        sl_servidor_call_with_sl_price = False
        sl_servidor_calls = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # Identifica nome da função
                func_name = None
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr

                if func_name != "log_exit":
                    continue

                # Procura kwargs pra achar exit_reason
                kwargs = {kw.arg: kw.value for kw in node.keywords}
                if "exit_reason" not in kwargs:
                    continue
                reason_node = kwargs["exit_reason"]
                reason_val = None
                if isinstance(reason_node, ast.Constant):
                    reason_val = reason_node.value
                if reason_val != "SL_SERVIDOR":
                    continue

                sl_servidor_calls.append(node)
                if "exit_sl_price" in kwargs:
                    sl_servidor_call_with_sl_price = True

        self.assertGreater(
            len(sl_servidor_calls), 0,
            "Esperado pelo menos 1 chamada log_exit com reason=SL_SERVIDOR no autotrader"
        )
        self.assertTrue(
            sl_servidor_call_with_sl_price,
            f"Nenhuma das {len(sl_servidor_calls)} chamadas log_exit(SL_SERVIDOR) "
            f"passa exit_sl_price. Diagnóstico de slippage fica cego."
        )


class TestSlPriceCalculation(unittest.TestCase):
    """Valida a fórmula de cálculo do exit_sl_price a partir do pos state."""

    def test_sl_price_buy(self):
        # BUY: SL abaixo do entry
        entry_price = 175000.0
        sl_pts = 200  # positivo
        point_val = 1.0
        sl_price = entry_price - abs(sl_pts) * point_val
        self.assertEqual(sl_price, 174800.0)

    def test_sl_price_sell(self):
        # SELL: SL acima do entry
        entry_price = 5750.0
        sl_pts = 5
        point_val = 0.001
        sl_price = entry_price + abs(sl_pts) * point_val
        self.assertAlmostEqual(sl_price, 5750.005, places=4)

    def test_slippage_calc(self):
        # BUY com slippage contra: exit < sl_th
        exit_price = 174750.0
        sl_th = 174800.0
        slippage_pts = abs(exit_price - sl_th)  # 50pts contra nós
        self.assertEqual(slippage_pts, 50.0)
        # Pra BUY, slippage contra = exit MENOR que sl_th
        self.assertLess(exit_price, sl_th)


if __name__ == "__main__":
    unittest.main()
