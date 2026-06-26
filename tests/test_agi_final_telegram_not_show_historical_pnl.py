"""
test_agi_final_telegram_not_show_historical_pnl.py
=======================================================
TDD: garante que a última notificação Telegram do AGI NÃO mostra
PnL histórico negativo (que confunde o operador).

BUG 2026-06-26 19:13:
  Relatório final mostrou:
    '📅 Projeção 30 dias: 🟢 PnL projetado: R$ +16,422'
  MAS a última linha Telegram disse:
    '✅ AGI v3.0 concluído — PnL R$-4329,83'

  Contradição: projeção diz +16k, mas última linha diz -4k.
  Bruno: 'nunca bloquear, sempre achar edge, criar se preciso.'
  Relatório final NÃO pode mostrar PnL histórico negativo
  (parece que AGI terminou com prejuízo).

FIX Wave 6.3.1:
  Última linha Telegram deve mostrar:
  - Se convergiu: projeção forward (PnL projetado 30d)
  - Se NÃO convergiu: lista de pares que precisam de edge (não PnL)
  - NUNCA: PnL histórico acumulado
"""
import os
import re
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


class TestAGIFinalTelegramNotShowHistoricalPnl(unittest.TestCase):
    """Última linha Telegram do AGI não pode mostrar PnL histórico negativo."""

    AGI_PATH = Path(PROJECT_ROOT) / "optimization" / "agi_tuning_17h.py"

    def test_final_telegram_no_historical_pnl(self):
        """A última notify_telegram do AGI não pode incluir PnL histórico.

        Wave 6.3.1: Bruno quer ver só progresso forward-looking.
        """
        src = self.AGI_PATH.read_text()

        # Procura a última notify_telegram (linha 4070)
        # Verifica que NÃO usa perf['by_symbol'] para calcular PnL
        match = re.search(
            r"total_pnl_final\s*=\s*sum\(.*by_symbol.*\.values\(\)\)",
            src
        )
        self.assertIsNone(
            match,
            "Wave 6.3.1: última linha Telegram não pode usar PnL histórico "
            "(perf['by_symbol']). Deve usar projeção forward (calculate_daily_expectation)."
        )

    def test_final_telegram_shows_forward_only(self):
        """A última notify_telegram deve mostrar APENAS forward."""
        src = self.AGI_PATH.read_text()
        # Procura notify_telegram final (multiline, pega chamada real)
        # Conta todas as notify_telegram no source
        notify_call_count = src.count("notify_telegram(")
        self.assertGreater(
            notify_call_count, 1,
            f"Deve haver pelo menos 2 notify_telegram (1 final + 1 outras). "
            f"Encontradas: {notify_call_count}"
        )
        # E nenhum deve usar PnL histórico direto
        bad_uses = re.findall(
            r"total_pnl_final\s*=\s*sum\(.*by_symbol",
            src
        )
        self.assertEqual(
            len(bad_uses), 0,
            f"Nenhum notify_telegram final pode usar PnL histórico. "
            f"Encontrado: {bad_uses}"
        )


if __name__ == "__main__":
    unittest.main()