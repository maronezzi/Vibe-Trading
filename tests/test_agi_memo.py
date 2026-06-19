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
    """apply_changes() deve HONRAR reenable_symbols e remover de disabled_symbols.

    2026-06-19 — Refatorado: o backup do config de produção agora fica
    em tmp_path (isolado por teste, não compartilhado em /tmp). Antes
    deste fix, múltiplos testes compartilhavam /tmp/_vt_config_real_backup.json
    e o último writer vencia — causando corrupção quando rodava junto
    com test_agi_strategy_change (que modificava apply_changes em monkey-patch).
    """

    def setUp(self):
        # 2026-06-19: backup isolado por teste via tmp_path (era /tmp compartilhado)
        import shutil
        import tempfile
        from vt_config_loader import load_config, save_full_config
        self._load = load_config
        self._save = save_full_config
        # Snapshot do config real em arquivo tmp desta instância
        self._backup_dir = tempfile.mkdtemp(prefix="vt_test_")
        self._backup_path = f"{self._backup_dir}/vt_config_real.json"
        self._real_config = load_config(force=True)
        with open(self._backup_path, "w") as f:
            json.dump(self._real_config, f)

    def tearDown(self):
        # 2026-06-19: restaura config real e limpa tmp_dir (era /tmp compartilhado)
        import shutil
        try:
            with open(self._backup_path) as f:
                real = json.load(f)
            self._save(real, updated_by="test_agi_memo_teardown")
        finally:
            shutil.rmtree(self._backup_dir, ignore_errors=True)

    def _call_apply_changes(self, llm_result, config_overrides):
        """Wrapper: importa apply_changes e usa config real + overrides.

        IMPORTANTE: testes NÃO podem passar um dict minúsculo para apply_changes,
        senão save_full_config() sobrescreve vt_config.json real com só os
        campos do test → CORRUPÇÃO DE PRODUÇÃO. Sempre passar a config real
        com overrides pontuais.
        """
        from agi_tuning_17h import apply_changes
        # Copiar config real e aplicar overrides do teste
        cfg = json.loads(json.dumps(self._real_config))
        for k, v in config_overrides.items():
            cfg[k] = v
        return apply_changes(llm_result, cfg, dry_run=True)  # DRY-RUN: não toca disco

    def test_reenable_removes_from_disabled_symbols(self):
        """Se LLM pede reenable de WDO, dry-run deve remover WDO de disabled_symbols (em memória).

        NOTA: dry_run=True não persiste no disco, mas o `config` in-memory
        deve refletir o estado pós-aplicação para o chamador poder inspecionar.
        """
        cfg_in = self._real_config.copy()
        cfg_in["disabled_symbols"] = ["WDO", "BIT"]  # override pontual
        llm = {
            "analysis": "ok",
            "changes": [],
            "reenable_symbols": ["WDO"],
        }
        from agi_tuning_17h import apply_changes
        apply_changes(llm, cfg_in, dry_run=True)
        # WDO deve ter saído, BIT deve continuar
        self.assertNotIn("WDO", cfg_in["disabled_symbols"])
        self.assertIn("BIT", cfg_in["disabled_symbols"])

    def test_reenable_idempotent_when_symbol_not_disabled(self):
        """Se símbolo pedido não está em disabled_symbols, não deve quebrar."""
        llm = {
            "analysis": "ok",
            "changes": [],
            "reenable_symbols": ["WIN"],  # WIN não está desativado (no real)
        }
        cfg_in = self._real_config.copy()
        # Não precisa adicionar nada em disabled_symbols
        from agi_tuning_17h import apply_changes
        # Não pode quebrar
        result = apply_changes(llm, cfg_in, dry_run=True)
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
