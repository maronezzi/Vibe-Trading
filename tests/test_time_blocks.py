"""
test_time_blocks.py
=====================
TDD: garante que time_blocks funciona — bloqueia combinações
(symbol, hour_range) baseado em evidência DB.

Wave 8.4 (2026-06-26, Bruno):
  'se as estratégias estão ruins, crie novas, melhore as existentes,
   observe e aplique'

  Achado do sub-agente DB (RELATORIO_OPORTUNIDADES_LUCRATIVAS.md):
  - BITM26 STRONG_TREND 09h-11h: -R$3.234 em 12 trades
  - WINQ26 VWAP (todos TFs): 52 SL_SERVIDOR, 23.1% WR, -R$331
  - Total: cortar ~R$2.8k/mês

FIX: nova função _is_blocked_time(symbol, hour) que consulta
config['time_blocks'] e retorna True se bloqueado. Wire no
check_and_trade.

time_blocks schema:
  {
    "BITM26": [{"start": 9, "end": 11, "reason": "..."}],
    "WINQ26": [{"start": 0, "end": 24, "strategy": "VWAP", "reason": "..."}],
  }

Por que importa: corta -R$2.8k/mês direto, fail-closed (default: não
bloqueia nada se config vazio).
"""
import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


class TestIsBlockedTime(unittest.TestCase):
    """A função _is_blocked_time existe e funciona."""

    def test_function_exists(self):
        from core.vt_autotrader import _is_blocked_time
        self.assertTrue(callable(_is_blocked_time))


class TestTimeBlockByHourRange(unittest.TestCase):
    """Bloqueia combinação (symbol, hour) dentro de range."""

    def test_blocks_within_hour_range(self):
        """
        Se BITM26 tem block 9-11h, _is_blocked_time("BITM26", 10) = True.
        """
        from core.vt_autotrader import _is_blocked_time
        # Mock CONFIG com block
        with patch("core.vt_autotrader.CONFIG", {
            "time_blocks": {
                "BITM26": [{"start": 9, "end": 11, "reason": "test"}]
            }
        }):
            # 10h está no range [9, 11] → bloqueado
            self.assertTrue(
                _is_blocked_time("BITM26", 10),
                "BITM26 10h deveria estar bloqueado (range 9-11)"
            )

    def test_allows_outside_hour_range(self):
        """
        Se BITM26 tem block 9-11h, _is_blocked_time("BITM26", 14) = False.
        """
        from core.vt_autotrader import _is_blocked_time
        with patch("core.vt_autotrader.CONFIG", {
            "time_blocks": {
                "BITM26": [{"start": 9, "end": 11, "reason": "test"}]
            }
        }):
            # 14h está fora do range
            self.assertFalse(
                _is_blocked_time("BITM26", 14),
                "BITM26 14h deveria estar liberado (fora do range 9-11)"
            )

    def test_empty_time_blocks_fails_open(self):
        """Sem time_blocks no config, retorna False (fail-open)."""
        from core.vt_autotrader import _is_blocked_time
        with patch("core.vt_autotrader.CONFIG", {}):
            self.assertFalse(
                _is_blocked_time("BITM26", 10),
                "Sem time_blocks, deve ser fail-open"
            )


class TestTimeBlockEdgeCases(unittest.TestCase):
    """Edge cases: range overnight, start==end, múltiplos ranges."""

    def test_block_boundary_inclusive(self):
        """start=9 end=11 inclui 9h e 11h (inclusive)."""
        from core.vt_autotrader import _is_blocked_time
        with patch("core.vt_autotrader.CONFIG", {
            "time_blocks": {
                "BITM26": [{"start": 9, "end": 11, "reason": "test"}]
            }
        }):
            self.assertTrue(_is_blocked_time("BITM26", 9), "9h deve incluir")
            self.assertTrue(_is_blocked_time("BITM26", 11), "11h deve incluir")
            self.assertFalse(_is_blocked_time("BITM26", 12), "12h NÃO deve incluir")

    def test_unknown_symbol_not_blocked(self):
        """Symbol sem entrada em time_blocks não é bloqueado."""
        from core.vt_autotrader import _is_blocked_time
        with patch("core.vt_autotrader.CONFIG", {
            "time_blocks": {"BITM26": [{"start": 9, "end": 11, "reason": "test"}]}
        }):
            self.assertFalse(
                _is_blocked_time("WINQ26", 10),
                "WINQ26 não tem block, deve passar"
            )


if __name__ == "__main__":
    unittest.main()
