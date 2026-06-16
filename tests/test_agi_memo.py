#!/usr/bin/env python3
"""TDD — AGI memo: injeção de memorando do Bruno no prompt + reativação de símbolos desativados.

Cobre:
  1. build_llm_prompt() inclui seção "📋 MEMO DO BRUNO" quando /tmp/vt_agi_memo.json existe
  2. build_llm_prompt() NÃO quebra quando o memo não existe
  3. apply_changes() HONRA reenable_symbols: remove símbolo de disabled_symbols
  4. apply_changes() NÃO remove se símbolo não está em disabled_symbols (idempotente)
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

MEMO_PATH = "/tmp/vt_agi_memo.json"


class TestMemoInjection(unittest.TestCase):
    """build_llm_prompt() deve injetar memorando do Bruno se /tmp/vt_agi_memo.json existir."""

    def setUp(self):
        # Limpar memo entre testes
        if os.path.exists(MEMO_PATH):
            os.remove(MEMO_PATH)

    def tearDown(self):
        if os.path.exists(MEMO_PATH):
            os.remove(MEMO_PATH)

    def _minimal_perf(self):
        return {
            "by_symbol": {"WDO": {"n_trades": 10, "wins": 5, "losses": 5, "win_rate": 50.0,
                                   "total_pnl": 100.0, "avg_pnl": 10.0, "worst": -50.0, "best": 80.0,
                                   "total_fees": 5.0}},
            "by_symbol_tf": {},
            "exit_reasons": {},
            "today": {},
            "streaks": {},
            "period_days": 7,
            "cutoff_date": "2026-06-09",
        }

    def test_memo_is_included_when_file_exists(self):
        """Se o memo existe, prompt deve ter a seção 'MEMO DO BRUNO'."""
        from agi_tuning_17h import build_llm_prompt

        memo = {
            "text": "Analisar WDO completo. Se edge positivo, reativar para amanha.",
            "reenable_symbols": ["WDO"],
            "issued_at": "2026-06-16T09:00:00Z",
            "issued_by": "bruno_telegram",
        }
        with open(MEMO_PATH, "w") as f:
            json.dump(memo, f)

        cfg = {"win": {"sl_atr_mult": 0.8}, "wdo": {"sl_atr_mult": 0.8}}
        prompt = build_llm_prompt(self._minimal_perf(), [], cfg)

        self.assertIn("MEMO DO BRUNO", prompt)
        self.assertIn("WDO", prompt)
        self.assertIn("reativar", prompt.lower())

    def test_memo_absent_does_not_break_prompt(self):
        """Sem memo, prompt não deve quebrar e não deve ter seção MEMO DO BRUNO."""
        from agi_tuning_17h import build_llm_prompt

        cfg = {"win": {"sl_atr_mult": 0.8}}
        prompt = build_llm_prompt(self._minimal_perf(), [], cfg)

        # Não pode quebrar
        self.assertIsInstance(prompt, str)
        self.assertGreater(len(prompt), 100)
        # Não deve ter memo section
        self.assertNotIn("MEMO DO BRUNO", prompt)


class TestReenableSymbols(unittest.TestCase):
    """apply_changes() deve HONRAR reenable_symbols e remover de disabled_symbols."""

    def setUp(self):
        # Import lazy para não quebrar se outros testes rodam
        pass

    def _call_apply_changes(self, llm_result, config):
        """Wrapper: importa apply_changes do AGI."""
        from agi_tuning_17h import apply_changes
        return apply_changes(llm_result, config, dry_run=False)

    def test_reenable_removes_from_disabled_symbols(self):
        """Se LLM pede reenable de WDO, deve sair de disabled_symbols."""
        cfg = {
            "disabled_symbols": ["WDO", "BIT"],
            "disabled_timeframes": ["WDO_M5"],
            "win": {"sl_atr_mult": 0.8},
        }
        llm = {
            "analysis": "ok",
            "changes": [],
            "reenable_symbols": ["WDO"],
        }
        self._call_apply_changes(llm, cfg)
        # WDO deve ter saído, BIT deve continuar
        self.assertNotIn("WDO", cfg["disabled_symbols"])
        self.assertIn("BIT", cfg["disabled_symbols"])

    def test_reenable_idempotent_when_symbol_not_disabled(self):
        """Se símbolo pedido não está em disabled_symbols, não deve quebrar."""
        cfg = {
            "disabled_symbols": ["BIT"],
            "win": {"sl_atr_mult": 0.8},
        }
        llm = {
            "analysis": "ok",
            "changes": [],
            "reenable_symbols": ["WIN"],  # WIN não está desativado
        }
        # Não pode quebrar
        result = self._call_apply_changes(llm, cfg)
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
