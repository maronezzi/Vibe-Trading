"""
test_watchdog_hermes_helper_import.py
=========================================
TDD: garante que monitoring/vt_trade_watchdog.py importa
vt_hermes_helper corretamente (via core.).

BUG IDENTIFICADO 2026-06-26 14:47:
  O cron vt-trade-watchdog falhou com:
    'No module named vt_hermes_helper'
  Causa: import 'from vt_hermes_helper' (sem prefixo core.)
  + sys.path sem /core/, enquanto vt_daily_report/vt_pre_flight
  tinham o fix sys.path.insert(0, ../core).

FIX (Wave 8.7):
  - sys.path.insert(0, .../core) ANTES dos imports
  - from core.vt_hermes_helper import hermes_send (qualificado)

VALIDAÇÃO:
- RED: watchdog falha ao importar vt_hermes_helper
- GREEN: watchdog importa via core.vt_hermes_helper
"""
import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


class TestWatchdogHermesHelperImport(unittest.TestCase):
    """monitoring/vt_trade_watchdog.py deve importar vt_hermes_helper corretamente."""

    def test_watchdog_imports_vt_hermes_helper(self):
        """
        Importa o módulo como o cron faz (sem sys.path com core/).
        Deve funcionar porque o watchdog agora tem o sys.path.insert core.
        """
        import importlib.util
        watchdog_path = Path(PROJECT_ROOT) / "monitoring" / "vt_trade_watchdog.py"

        # Cria módulo isolado
        spec = importlib.util.spec_from_file_location("vt_trade_watchdog_test", watchdog_path)
        module = importlib.util.module_from_spec(spec)

        # Tenta executar — se falhar, vai levantar exceção
        try:
            spec.loader.exec_module(module)
            self.assertTrue(
                hasattr(module, 'log') and hasattr(module, 'notify_telegram'),
                "Módulo deve definir log e notify_telegram"
            )
        except ModuleNotFoundError as e:
            if "vt_hermes_helper" in str(e):
                self.fail(f"watchdog falha ao importar vt_hermes_helper: {e}")
            else:
                # Outros ModuleNotFoundError são esperados (MT5, config, etc.)
                self.skipTest(f"Outros imports faltando (esperado): {e}")

    def test_watchdog_uses_qualified_import(self):
        """O código-fonte deve usar 'from core.vt_hermes_helper' (não 'from vt_hermes_helper')."""
        watchdog_src = Path(PROJECT_ROOT, "monitoring", "vt_trade_watchdog.py").read_text()
        self.assertIn(
            "from core.vt_hermes_helper", watchdog_src,
            "monitoring/vt_trade_watchdog.py DEVE usar 'from core.vt_hermes_helper'"
        )
        # E NÃO deve ter import sem prefixo
        bad_lines = [line for line in watchdog_src.splitlines()
                     if line.strip().startswith("from vt_hermes_helper")
                     and "core." not in line]
        self.assertEqual(
            len(bad_lines), 0,
            f"Não deve ter 'from vt_hermes_helper' sem prefixo. Encontrado: {bad_lines}"
        )


if __name__ == "__main__":
    unittest.main()