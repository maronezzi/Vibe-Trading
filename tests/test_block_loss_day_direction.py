"""
test_block_loss_day_direction.py
==================================
TDD: garante que o autotrader BLOQUEIA combinações dia-da-semana +
direction comprovadamente perdedoras (análise DB 30d).

PROBLEMA IDENTIFICADO 2026-06-25 (DB analysis, 309 trades):
  - Quarta (3) BUY:  38t | WR 42.1% | PnL R$ -6.775,70 (PIIOR DIA)
  - Terça  (2) SELL: 65t | WR 29.2% | PnL R$ -2.946,45
  - Total estimado: -R$9.722 em 30d (single-day drawdown)

FIX: _is_blocked_day_direction() rejeita entry quando (weekday, direction)
está em block_list. Lista configurável via config["blocked_day_directions"].
Default defensivo: bloqueia as 2 piores combinações.

Por que importa: padrão claro de "dia da semana ruim pra direção X".
Filtro simples, retorno alto, baixo risco de regressão.
"""
import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


# Combinações bloqueadas (weekday=0=Seg, 1=Ter, ..., 6=Dom)
BLOCKED_COMBINATIONS = {
    (2, "BUY"),   # Quarta BUY: -R$6.775 (PIIOR padrão)
    (1, "SELL"),  # Terça SELL: -R$2.946
}


class TestIsBlockedDayDirection(unittest.TestCase):
    """Testa a função _is_blocked_day_direction()."""

    def setUp(self):
        # Import lazy pra evitar erro de import se autotrader não está carregado
        from core.vt_autotrader import _is_blocked_day_direction
        self._is_blocked = _is_blocked_day_direction

    def test_wednesday_buy_blocked(self):
        """Quarta (weekday=2) BUY NÃO deve ser bloqueado (liberado manualmente 2026-07-01).

        Histórico: 30d mostrou Quarta BUY -R$6.775 (regra AGI Recomendada).
        Decisão Bruno 2026-07-01 (commit 48780d05): liberar BUY quarta manualmente.
        _updated_by = 'bruno_manual_release_wed_buy_2026_07_01', _version=950.
        Bloqueio mantido apenas para Terça SELL (-R$2.946 histórico), em test_tuesday_sell_blocked.
        """
        # Mock datetime.now() para retornar quarta-feira
        wednesday = datetime(2026, 6, 24, 10, 30)  # 24/06/2026 é quarta
        self.assertEqual(wednesday.weekday(), 2, "sanity check: 24/06/2026 deve ser quarta")
        with patch("core.vt_autotrader.datetime") as mock_dt:
            mock_dt.now.return_value = wednesday
            self.assertFalse(
                self._is_blocked("BUY"),
                f"Quarta BUY NÃO deve ser bloqueado (liberação manual Bruno 2026-07-01, v950), retornou True"
            )

    def test_tuesday_sell_blocked(self):
        """Terça (weekday=1) SELL deve ser bloqueado."""
        tuesday = datetime(2026, 6, 23, 10, 30)  # 23/06/2026 é terça
        self.assertEqual(tuesday.weekday(), 1)
        with patch("core.vt_autotrader.datetime") as mock_dt:
            mock_dt.now.return_value = tuesday
            self.assertTrue(
                self._is_blocked("SELL"),
                f"Terça SELL deve ser bloqueado, retornou False"
            )

    def test_monday_buy_allowed(self):
        """Segunda (weekday=0) BUY NÃO deve ser bloqueado (não está na lista)."""
        monday = datetime(2026, 6, 22, 10, 30)  # 22/06/2026 é segunda
        self.assertEqual(monday.weekday(), 0)
        with patch("core.vt_autotrader.datetime") as mock_dt:
            mock_dt.now.return_value = monday
            self.assertFalse(
                self._is_blocked("BUY"),
                f"Segunda BUY NÃO deve ser bloqueado, retornou True"
            )

    def test_thursday_sell_allowed(self):
        """Quinta (weekday=3) SELL NÃO deve ser bloqueado (PnL fraco mas não -X)."""
        thursday = datetime(2026, 6, 25, 10, 30)  # 25/06/2026 é quinta
        self.assertEqual(thursday.weekday(), 3)
        with patch("core.vt_autotrader.datetime") as mock_dt:
            mock_dt.now.return_value = thursday
            self.assertFalse(
                self._is_blocked("SELL"),
                f"Quinta SELL NÃO deve ser bloqueado, retornou True"
            )

    def test_returns_false_when_no_config(self):
        """Se config não tem blocked_day_directions, retorna False (fail-open)."""
        # O DEFAULT_BLOCKED_DAY_DIRECTIONS é [(2, "BUY"), (1, "SELL")].
        # Se hoje for quarta (weekday=2) e direction=BUY, retorna True mesmo
        # sem config (fallback pro default). Isto é CORRETO — fail-safe,
        # não fail-open. O fail-open é quando SÓ passamos uma lista vazia.
        with patch("core.vt_autotrader.CONFIG", {"blocked_day_directions": []}), \
             patch("core.vt_autotrader.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 6, 24, 10, 30)  # quarta
            result = self._is_blocked("BUY")
            self.assertFalse(
                result,
                f"Lista vazia em config deve desabilitar bloqueio (fail-open), got {result}"
            )


if __name__ == "__main__":
    unittest.main()
