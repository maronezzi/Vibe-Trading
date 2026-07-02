"""
Testes de safety do mt5_error_recovery safe_buy/safe_sell (Fase 3 — Lei 3).

Valida a defesa em profundidade: safe_buy/safe_sell validam SL ANTES do loop de
retry. Se sl_pts inválido (None/0/negativo), abortam imediatamente com BLOCKED
MISSING_STOP_LOSS — não desperdiçam retries nem chamam buy/sell.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from mt5 import mt5_error_recovery as er


class TestSafeBuySellStopLossValidation:
    def test_safe_buy_none_sl_aborts_without_retry(self):
        """sl_pts=None → BLOCKED imediato, buy() NUNCA chamado (0 retries)."""
        with patch("mt5_orchestrator.buy") as buy_mock:
            result = er.safe_buy("WINQ26", 1, sl_pts=None)
        assert result["status"] == "BLOCKED"
        assert result["reason"] == "MISSING_STOP_LOSS"
        assert result["ticket"] == 0
        buy_mock.assert_not_called()

    def test_safe_sell_none_sl_aborts_without_retry(self):
        with patch("mt5_orchestrator.sell") as sell_mock:
            result = er.safe_sell("WDOQ26", 1, sl_pts=None)
        assert result["reason"] == "MISSING_STOP_LOSS"
        sell_mock.assert_not_called()

    def test_safe_buy_zero_sl_aborts(self):
        with patch("mt5_orchestrator.buy") as buy_mock:
            result = er.safe_buy("WINQ26", 1, sl_pts=0)
        assert result["reason"] == "MISSING_STOP_LOSS"
        buy_mock.assert_not_called()

    def test_safe_buy_negative_sl_aborts(self):
        with patch("mt5_orchestrator.buy") as buy_mock:
            result = er.safe_buy("WINQ26", 1, sl_pts=-100)
        assert result["reason"] == "MISSING_STOP_LOSS"
        buy_mock.assert_not_called()

    def test_valid_sl_proceeds_to_buy(self):
        """sl_pts válido → não aborta, chama buy (que fará o trabalho)."""
        with patch("mt5_orchestrator.buy",
                   return_value={"status": "FILLED", "ticket": 123,
                                 "price": 100.0}) as buy_mock:
            result = er.safe_buy("WINQ26", 1, sl_pts=200)
        buy_mock.assert_called_once()
        assert result["status"] == "FILLED"

    def test_block_does_not_trigger_llm_fallback(self):
        """SL inválido aborta ANTES do LLM fallback (não gasta cota de LLM)."""
        with patch("mt5_orchestrator.buy") as buy_mock, \
             patch.object(er, "_llm_diagnose_error") as llm_mock:
            er.safe_buy("WINQ26", 1, sl_pts=None)
        llm_mock.assert_not_called()
