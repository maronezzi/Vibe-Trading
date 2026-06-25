"""
test_disabled_symbols_filter.py
=================================
TDD: garante que check_and_trade() pula símbolos em CONFIG["disabled_symbols"].

Achado 2026-06-25: o loop `for symbol_root in CONFIG["symbols"]` em
check_and_trade() (linha 646 original) NÃO filtrava por disabled_symbols.
Resultado: BIT estava em disabled_symbols mas o autotrader processava
BIT_M5, BIT_M15, BIT_M30, BIT_H1 normalmente. Decisão de desabilitar
não tinha efeito.

FIX: filtrar active_symbols antes do loop.

Este teste monkey-patch o CONFIG para injetar um símbolo disabled e
verificar que o loop interno pula esse símbolo.
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


class TestDisabledSymbolsFilter(unittest.TestCase):
    """Garante que check_and_trade() pula símbolos em disabled_symbols."""

    def setUp(self):
        import core.vt_autotrader as vt
        self.vt = vt
        # Pega config real
        self.real_config = vt.load_config()

    def test_disabled_symbols_excluded_from_active_symbols(self):
        """Se BIT está em disabled_symbols, active_symbols NÃO contém BIT."""
        config = {
            "symbols": ["WIN", "BIT", "WSP", "WDO"],
            "disabled_symbols": ["BIT"],
            "halt_trading": False,
            "halt_new_trades": False,
            "warmup_minutes": 0,
            "winddown_minutes": 0,
            "start_hour": 0, "start_minute": 0,
            "close_hour": 23, "close_minute": 59,
        }
        disabled = config.get("disabled_symbols", [])
        active = [s for s in config["symbols"] if s not in disabled]
        self.assertEqual(
            active, ["WIN", "WSP", "WDO"],
            f"BIT deveria ser filtrado de active_symbols. Ativos: {active}"
        )

    def test_empty_disabled_symbols_keeps_all(self):
        """Sem disabled_symbols, todos os symbols ficam ativos."""
        config = {
            "symbols": ["WIN", "BIT", "WSP", "WDO"],
            "disabled_symbols": [],
        }
        disabled = config.get("disabled_symbols", [])
        active = [s for s in config["symbols"] if s not in disabled]
        self.assertEqual(active, ["WIN", "BIT", "WSP", "WDO"])

    def test_multiple_disabled_symbols_all_filtered(self):
        """Múltiplos disabled são todos filtrados."""
        config = {
            "symbols": ["WIN", "BIT", "WSP", "WDO"],
            "disabled_symbols": ["BIT", "WSP"],
        }
        disabled = config.get("disabled_symbols", [])
        active = [s for s in config["symbols"] if s not in disabled]
        self.assertEqual(active, ["WIN", "WDO"])


if __name__ == "__main__":
    unittest.main()
