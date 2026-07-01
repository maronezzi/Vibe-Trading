"""
Testes do Auditor de Integridade de Escopo (Fase 2.3 — Lei 2).

Regra Bruno: todos os 16 pares WIN/BIT/WSP/WDO × M5/M15/M30/H1 devem estar
ativos; IND é completamente ignorado (índice cheio, não operado).

Cobertura:
  1. config limpo (16/16) → clean=True, 0 violações
  2. detecta disabled_timeframes (BIT_M5, BIT_M30) — caso real do config atual
  3. detecta TF ausente de timeframes_by_symbol (WIN sem H1) — caso real
  4. IND é totalmente ignorado (mesmo presente, não gera violação)
  5. CLI --json retorna estrutura correta
  6. Lei 2: run() NUNCA modifica o config recebido
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import check_symbols_active as csa
from scripts.check_symbols_active import (
    ScopeReport,
    audit_scope,
    format_alert,
    main,
    run,
)


def _clean_config() -> dict:
    """Config 100% conforme Lei 2: 16 pares ativos, sem IND."""
    syms = ["WIN", "BIT", "WSP", "WDO"]
    tfs = ["M5", "M15", "M30", "H1"]
    return {
        "symbols": syms,
        "timeframes": tfs,
        "disabled_symbols": [],          # nem IND precisa estar aqui p/ auditoria
        "disabled_timeframes": [],
        "strategy_by_tf": {f"{s}_{t}": "ADX_TREND" for s in syms for t in tfs},
        "params_by_tf": {f"{s}_{t}": {"sl_atr_mult": 1.5} for s in syms for t in tfs},
        "timeframes_by_symbol": {s: list(tfs) for s in syms},
    }


# ── 1. config limpo ─────────────────────────────────────────────────────────
class TestCleanConfig:
    def test_all_16_active_when_clean(self):
        cfg = _clean_config()
        report = audit_scope(cfg)
        assert report.clean is True
        assert len(report.expected_pairs) == 16
        assert report.active_count == 16
        assert report.violations == []


# ── 2. detecta disabled_timeframes ──────────────────────────────────────────
class TestDisabledTimeframes:
    def test_disabled_tf_flagged_as_violation(self):
        """Caso real do config atual: BIT_M5 + BIT_M30 em disabled_timeframes."""
        cfg = _clean_config()
        cfg["disabled_timeframes"] = ["BIT_M5", "BIT_M30"]
        report = audit_scope(cfg)
        assert not report.clean
        disabled_tf_violations = [v for v in report.violations
                                  if v.kind == "disabled_timeframe"]
        pairs = {v.pair for v in disabled_tf_violations}
        assert pairs == {"BIT_M5", "BIT_M30"}


# ── 3. detecta TF ausente de timeframes_by_symbol ───────────────────────────
class TestMissingFromTimeframesBySymbol:
    def test_win_without_h1_flagged(self):
        """Caso real: WIN sem H1 em timeframes_by_symbol."""
        cfg = _clean_config()
        cfg["timeframes_by_symbol"]["WIN"] = ["M5", "M15", "M30"]  # sem H1
        report = audit_scope(cfg)
        win_h1 = [v for v in report.violations
                  if v.pair == "WIN_H1"
                  and v.kind == "missing_from_timeframes_by_symbol"]
        assert len(win_h1) == 1

    def test_missing_strategy_flagged(self):
        cfg = _clean_config()
        del cfg["strategy_by_tf"]["WDO_M15"]
        report = audit_scope(cfg)
        missing_strat = [v for v in report.violations
                         if v.kind == "missing_strategy"]
        assert any(v.pair == "WDO_M15" for v in missing_strat)

    def test_missing_params_flagged(self):
        cfg = _clean_config()
        del cfg["params_by_tf"]["BIT_H1"]
        report = audit_scope(cfg)
        missing_params = [v for v in report.violations
                          if v.kind == "missing_params"]
        assert any(v.pair == "BIT_H1" for v in missing_params)

    def test_disabled_symbol_flagged(self):
        """Um símbolo esperado (WIN) em disabled_symbols gera 4 violações."""
        cfg = _clean_config()
        cfg["disabled_symbols"] = ["WIN"]
        report = audit_scope(cfg)
        win_disabled = [v for v in report.violations
                        if v.kind == "disabled_symbol"]
        assert len(win_disabled) == 4  # WIN × 4 TFs
        assert {v.pair for v in win_disabled} == {
            "WIN_M5", "WIN_M15", "WIN_M30", "WIN_H1"}


# ── 4. IND é ignorado ───────────────────────────────────────────────────────
class TestIndIgnored:
    def test_ind_in_disabled_symbols_does_not_generate_violation(self):
        """IND em disabled_symbols NÃO conta como violação — é hard-kill esperado."""
        cfg = _clean_config()
        cfg["disabled_symbols"] = ["IND"]
        report = audit_scope(cfg)
        ind_violations = [v for v in report.violations if "IND" in v.pair]
        assert ind_violations == []
        assert report.clean is True  # IND não é esperado → não viola nada

    def test_ind_in_config_keys_completely_ignored(self):
        """Mesmo IND presente em vários lugares, auditoria não o menciona."""
        cfg = _clean_config()
        cfg["symbols"] = ["WIN", "BIT", "WSP", "WDO", "IND"]
        cfg["disabled_symbols"] = ["IND"]
        cfg["disabled_timeframes"] = ["IND_M5"]
        cfg["timeframes_by_symbol"]["IND"] = []
        # audit_scope usa EXPECTED_SYMBOLS (sem IND) → IND nunca iterado
        report = audit_scope(cfg)
        assert all("IND" not in v.pair for v in report.violations)
        assert report.clean is True


# ── 5. CLI --json ───────────────────────────────────────────────────────────
class TestCLI:
    def test_json_output_structure(self, capsys):
        cfg = _clean_config()
        with patch("scripts.check_symbols_active.run",
                   return_value=audit_scope(cfg)), \
             patch.object(sys, "argv", ["x", "--json"]):
            rc = main()
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["expected_count"] == 16
        assert data["active_count"] == 16
        assert data["clean"] is True
        assert data["violation_count"] == 0
        assert rc == 0

    def test_exit_code_1_when_violations(self):
        cfg = _clean_config()
        cfg["disabled_timeframes"] = ["BIT_M5"]
        with patch.object(sys, "argv", ["x", "--quiet"]), \
             patch("scripts.check_symbols_active.run",
                   return_value=audit_scope(cfg)):
            rc = main()
        assert rc == 1

    def test_quiet_mode_no_telegram(self):
        """--quiet não envia Telegram."""
        cfg = _clean_config()
        cfg["disabled_symbols"] = ["WIN"]  # violação
        with patch("scripts.check_symbols_active._notify_telegram") as notify, \
             patch("core.vt_config_loader.load_config", return_value=cfg), \
             patch.object(sys, "argv", ["x", "--quiet"]):
            run(alert=False)
            notify.assert_not_called()


# ── 6. Lei 2: run() não modifica config ─────────────────────────────────────
class TestDoesNotMutate:
    def test_audit_does_not_modify_config(self):
        """Lei 2: auditoria é READ-ONLY. Nunca altera o config recebido."""
        cfg = _clean_config()
        cfg["disabled_timeframes"] = ["BIT_M5"]
        before = copy.deepcopy(cfg)
        _ = audit_scope(cfg)
        assert cfg == before  # intacto

    def test_format_alert_mentions_bruno_decision(self):
        """O alerta deixa claro que Bruno decide (não auto-corrigir)."""
        cfg = _clean_config()
        cfg["disabled_timeframes"] = ["BIT_M5"]
        report = audit_scope(cfg)
        msg = format_alert(report)
        assert "Bruno" in msg
        assert "BIT_M5" in msg
        assert "Lei 2" in msg
