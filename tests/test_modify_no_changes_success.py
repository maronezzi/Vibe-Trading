"""
test_modify_no_changes_success.py
==================================
TDD: reproduz o bug do EMERGENCY CLOSE falso (10/08/2026, 2x).

CONTEXTO DO BUG (10/08, WINQ26 09:37 e 09:51):
- Breakeven tenta apertar SL para 5pts (cost_pts) → MT5 rejeita "Invalid stops".
- _fix_invalid_stops_modify consulta o SL REAL da posição (560pts — o SL original,
  que JÁ ESTÁ aplicado e protegendo) e recalcula → tenta reaplicar 560pts.
- MT5 responde retcode 10027 "No changes" = "o SL pedido já está nesse valor".
  Semanticamente é SUCESSO (estado ideal), mas o executor trata retcode != DONE
  como erro → "No changes" cai em UNKNOWN → consulta LLM → LLM timeout (cadeia de
  providers degradada) → LLM abortou → safe_modify_sl retorna falha →
  safe_modify_sl_with_emergency_close vê PnL ≤ 0 → EMERGENCY CLOSE de posição
  que JÁ TINHA SL válido aplicado. Perdas desnecessárias: -R$5 e -R$19.

REPRODUÇÃO AO VIVO (teste manual com mt5_orchestrator):
    modify_sl(ticket, 500)  → {'error': 'No changes'}   ← mesmo SL que já está lá
    modify_sl(ticket, 400)  → {'status': 'ok'}          ← SL novo funciona

FIX:
- safe_modify_sl() deve tratar "No changes" como SUCESSO (SL já aplicado),
  retornando status="ok" com flag already_applied — sem chamar LLM, sem retry,
  sem emergency close.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from mt5 import mt5_error_recovery as er


class TestSafeModifySlNoChangesIsSuccess:
    """'No changes' (retcode 10027) = SL já aplicado → sucesso, não erro."""

    def test_no_changes_returns_ok_without_llm(self):
        """modify_sl devolve {'error': 'No changes'} → status ok, LLM NÃO chamado."""
        with patch("mt5_orchestrator.modify_sl",
                   return_value={"error": "No changes"}) as modify_mock, \
             patch.object(er, "_llm_diagnose_error") as llm_mock:
            result = er.safe_modify_sl("WINQ26", 12345, 560,
                                       entry_price=172990.0, direction="BUY")
        assert result["status"] == "ok"
        assert result.get("already_applied") is True
        assert result.get("new_sl") == 560
        modify_mock.assert_called_once()  # sem retries
        llm_mock.assert_not_called()      # sem consulta LLM

    def test_no_changes_lowercase_variant(self):
        """Variação de caixa: 'no changes' também é sucesso."""
        with patch("mt5_orchestrator.modify_sl",
                   return_value={"error": "no changes"}):
            result = er.safe_modify_sl("WINQ26", 12345, 560,
                                       entry_price=172990.0, direction="BUY")
        assert result["status"] == "ok"
        assert result.get("already_applied") is True

    def test_real_sl_change_still_works(self):
        """SL novo (diferente do atual) continua fluxo normal de retry."""
        with patch("mt5_orchestrator.modify_sl",
                   return_value={"status": "ok", "ticket": "12345",
                                 "new_sl": 400.0}):
            result = er.safe_modify_sl("WINQ26", 12345, 400,
                                       entry_price=172990.0, direction="BUY")
        assert result["status"] == "ok"
        assert result.get("already_applied") is None  # não é no-changes

    def test_emergency_wrapper_no_changes_does_not_close(self):
        """Cadeia completa: No changes → wrapper NÃO faz emergency close."""
        from core import vt_emergency as ve
        with patch("mt5_orchestrator.modify_sl",
                   return_value={"error": "No changes"}), \
             patch.object(ve, "_emergency_close_position") as close_mock, \
             patch.object(ve, "_notify_critical_emergency") as notify_mock:
            result = ve.safe_modify_sl_with_emergency_close(
                "WINQ26", 12345, 560, 172990.0, "BUY")
        assert result["status"] == "ok"
        assert result["emergency_closed"] is False
        close_mock.assert_not_called()
        notify_mock.assert_not_called()

    def test_emergency_wrapper_skipped_does_not_close(self):
        """status 'skipped' (gate stop_level) → SL anterior mantido → NÃO fecha."""
        from core import vt_emergency as ve
        with patch("mt5_orchestrator.modify_sl",
                   return_value={"status": "skipped",
                                 "error": "within_stop_level",
                                 "sl_pts": 5}), \
             patch.object(ve, "_emergency_close_position") as close_mock, \
             patch.object(ve, "_notify_critical_emergency") as notify_mock:
            result = ve.safe_modify_sl_with_emergency_close(
                "WINQ26", 12345, 5, 172990.0, "BUY")
        assert result["status"] == "ok"
        assert result["emergency_closed"] is False
        assert result.get("sl_unchanged") is True
        close_mock.assert_not_called()
        notify_mock.assert_not_called()

    def test_classify_error_still_unknown_for_other_errors(self):
        """Regressão: erros diferentes de No changes continuam UNKNOWN."""
        assert er._classify_error("Some other error") == "UNKNOWN"
        assert er._classify_error("Invalid stops") == "INVALID_STOPS"
