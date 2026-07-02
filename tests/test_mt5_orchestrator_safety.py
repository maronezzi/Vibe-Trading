"""
Testes de safety do mt5_orchestrator buy/sell (Fase 3 — Leis 3 e 4).

Cobertura:
  Lei 3 (SL obrigatório):
    1. buy/sell com sl_pts=None → BLOCKED MISSING_STOP_LOSS (NÃO envia ao MT5)
    2. buy/sell com sl_pts=0 → BLOCKED
    3. buy/sell com sl_pts negativo → BLOCKED
    4. buy/sell com sl_pts válido → envia ao MT5 (não bloqueado)

  Lei 4 (Garantia MT5):
    5. resposta FILLED com ticket>0 → passa (sucesso)
    6. resposta FILLED sem ticket → BLOCKED NOT_CONFIRMED
    7. resposta com retcode 10008 (PLACED) + ticket → recuperada como FILLED
       (corrige bug latente onde PLACED virava REJECTED)
    8. resposta com retcode não-aceito → BLOCKED REJECTED_BY_RETCODE
    9. resposta REJECTED normal → devolvida como está

  Contrato:
    10. buy() nunca propaga exceção (não derruba o bot ao vivo)
    11. _run_wine nunca chamado quando sl inválido (Lei 3 short-circuit)
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from mt5 import mt5_orchestrator as orch


# ── Lei 3: SL obrigatório ───────────────────────────────────────────────────
class TestLei3MissingStopLoss:
    def test_buy_none_sl_blocked_without_mt5_call(self):
        """sl_pts=None → BLOCKED, _run_wine NUNCA chamado."""
        with patch.object(orch, "_run_wine") as rw:
            result = orch.buy("WINQ26", 1, sl_pts=None)
        assert result["status"] == "BLOCKED"
        assert result["reason"] == "MISSING_STOP_LOSS"
        assert result["ticket"] == 0
        rw.assert_not_called()  # CRÍTICO: nem chegou ao MT5

    def test_sell_none_sl_blocked(self):
        with patch.object(orch, "_run_wine") as rw:
            result = orch.sell("WDOQ26", 1, sl_pts=None)
        assert result["reason"] == "MISSING_STOP_LOSS"
        rw.assert_not_called()

    def test_buy_zero_sl_blocked(self):
        with patch.object(orch, "_run_wine") as rw:
            result = orch.buy("WINQ26", 1, sl_pts=0)
        assert result["reason"] == "MISSING_STOP_LOSS"
        rw.assert_not_called()

    def test_buy_negative_sl_blocked(self):
        with patch.object(orch, "_run_wine") as rw:
            result = orch.buy("WINQ26", 1, sl_pts=-50)
        assert result["reason"] == "MISSING_STOP_LOSS"
        rw.assert_not_called()

    def test_valid_sl_proceeds_to_mt5(self):
        """sl_pts válido → chama _run_wine com sl incluído nos args."""
        with patch.object(orch, "_run_wine",
                          return_value={"status": "FILLED", "ticket": 12345,
                                        "price": 100.0}) as rw:
            result = orch.buy("WINQ26", 1, sl_pts=200)
        rw.assert_called_once()
        # sl_pts deve estar nos args enviados ao Wine
        sent_args = rw.call_args[0]
        assert "200" in sent_args


# ── Lei 4: Garantia MT5 (confirmação) ───────────────────────────────────────
class TestLei4Confirmation:
    def test_filled_with_valid_ticket_passes(self):
        """FILLED + ticket>0 → sucesso, devolvido inalterado."""
        resp = {"status": "FILLED", "ticket": 246789, "price": 175000.0}
        with patch.object(orch, "_run_wine", return_value=resp):
            result = orch.buy("WINQ26", 1, sl_pts=200)
        assert result["status"] == "FILLED"
        assert result["ticket"] == 246789

    def test_filled_without_ticket_blocked(self):
        """FILLED mas ticket=0/'?' → NOT_CONFIRMED (Lei 4)."""
        resp = {"status": "FILLED", "ticket": 0, "price": 100.0}
        with patch.object(orch, "_run_wine", return_value=resp):
            result = orch.buy("WINQ26", 1, sl_pts=200)
        assert result["status"] == "BLOCKED"
        assert result["reason"] == "NOT_CONFIRMED"

    def test_filled_with_question_mark_ticket_blocked(self):
        """ticket='?' (placeholder do autotrader) → NOT_CONFIRMED."""
        resp = {"status": "FILLED", "ticket": "?", "price": 100.0}
        with patch.object(orch, "_run_wine", return_value=resp):
            result = orch.buy("WINQ26", 1, sl_pts=200)
        assert result["reason"] == "NOT_CONFIRMED"

    def test_retcode_placed_with_ticket_recovered_as_filled(self):
        """Bug latente corrigido: retcode 10008 (PLACED) + ticket → FILLED.

        Antes da Fase 3, 10008 caía no bucket REJECTED → orphan + duplicate.
        Agora é recuperado como FILLED (Lei 4: ordem aceita pelo broker).
        """
        resp = {"status": "REJECTED", "retcode": 10008, "ticket": 999999,
                "price": 100.0}
        with patch.object(orch, "_run_wine", return_value=resp):
            result = orch.buy("WINQ26", 1, sl_pts=200)
        assert result["status"] == "FILLED"
        assert result.get("recovered_placed") is True
        assert result["ticket"] == 999999

    def test_retcode_placed_without_ticket_not_recovered(self):
        """10008 sem ticket → não confirma (Lei 4 exige ticket)."""
        resp = {"status": "REJECTED", "retcode": 10008, "ticket": 0}
        with patch.object(orch, "_run_wine", return_value=resp):
            result = orch.buy("WINQ26", 1, sl_pts=200)
        # Sem ticket, não pode confirmar — devolve o REJECTED original
        assert result["status"] in ("REJECTED", "BLOCKED")

    def test_rejected_retcode_blocked(self):
        """retcode não-aceito (ex: 10004) → REJECTED_BY_RETCODE."""
        resp = {"status": "REJECTED", "retcode": 10004, "ticket": 0}
        with patch.object(orch, "_run_wine", return_value=resp):
            result = orch.buy("WINQ26", 1, sl_pts=200)
        # 10004 rejeitado: ou devolve REJECTED original ou BLOCKED com reason
        if result.get("reason"):
            assert result["reason"] == "REJECTED_BY_RETCODE"
        else:
            assert result["status"] == "REJECTED"

    def test_normal_rejected_returned_as_is(self):
        """REJECTED sem retcode → devolvido como está (contrato preservado)."""
        resp = {"status": "REJECTED", "error": "INVALID_VOLUME"}
        with patch.object(orch, "_run_wine", return_value=resp):
            result = orch.buy("WINQ26", 1, sl_pts=200)
        assert result["status"] == "REJECTED"

    def test_run_wine_error_returned_as_is(self):
        """Erro de transporte (_run_wine timeout) → devolvido como está."""
        resp = {"error": "timeout"}
        with patch.object(orch, "_run_wine", return_value=resp):
            result = orch.buy("WINQ26", 1, sl_pts=200)
        assert "error" in result or result["status"] != "FILLED"


# ── Contrato: nunca propaga exceção ─────────────────────────────────────────
class TestNoExceptionPropagation:
    def test_buy_never_raises_on_mt5_error(self):
        """buy() devolve dict de erro, nunca propaga exceção do MT5.

        Contrato real: _run_wine engole exceções internamente e devolve
        {"error": ...}. buy() herda isso — valida que o caminho de erro
        produz um dict, não um raise (não derruba o bot ao vivo).
        """
        # _run_wine real captura exceções → devolve {"error": ...}
        with patch.object(orch, "_run_wine",
                          return_value={"error": "timeout do Wine"}):
            result = orch.buy("WINQ26", 1, sl_pts=200)
        assert isinstance(result, dict)  # dict, não exceção

    def test_validate_safety_pure_function(self):
        """_validate_order_safety não tem side-effects (pure check)."""
        r = orch._validate_order_safety("WINQ26", 200)
        assert r is None  # válido → None
        r = orch._validate_order_safety("WINQ26", None)
        assert r is not None
        assert r["reason"] == "MISSING_STOP_LOSS"
