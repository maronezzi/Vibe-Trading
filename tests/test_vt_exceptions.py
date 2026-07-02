"""
Testes do core/vt_exceptions.py (Fase 3 — base das Leis 3 e 4).

Valida:
  1. As 3 exceções de domínio existem e herdam de OrderError
  2. Constantes de retcode nomeadas (fim de magic numbers)
  3. error_dict() produz o contrato correto (status=BLOCKED, ticket=0)
  4. from_exception() mapeia exceção → dict corretamente
  5. ACCEPTED_RETCODES contém DONE + PLACED (corrige bug latente do 10008)
"""
from __future__ import annotations

import pytest

from core.vt_exceptions import ACCEPTED_RETCODES, MAGIC_VIBETRADING
from core import vt_exceptions as vte


# ── 1. Exceções de domínio ──────────────────────────────────────────────────
class TestExceptions:
    def test_three_order_exceptions_exist(self):
        for cls_name in ("MissingStopLossError", "OrderNotConfirmedError",
                         "OrderRejectedError"):
            assert hasattr(vte, cls_name), f"{cls_name} faltando"

    def test_all_inherit_from_order_error(self):
        assert issubclass(vte.MissingStopLossError, vte.OrderError)
        assert issubclass(vte.OrderNotConfirmedError, vte.OrderError)
        assert issubclass(vte.OrderRejectedError, vte.OrderError)
        assert issubclass(vte.OrderError, RuntimeError)

    def test_exceptions_carry_message(self):
        exc = vte.MissingStopLossError("BUY WIN sem SL")
        assert "WIN" in str(exc)


# ── 2. Constantes de retcode ────────────────────────────────────────────────
class TestRetcodeConstants:
    def test_done_and_placed_named(self):
        assert vte.TRADE_RETCODE_DONE == 10009
        assert vte.TRADE_RETCODE_PLACED == 10008

    def test_accepted_retcodes_includes_both(self):
        """Bug latente corrigido: 10008 (PLACED) agora é aceito, não rejeitado."""
        assert 10009 in ACCEPTED_RETCODES
        assert 10008 in ACCEPTED_RETCODES   # ANTES tratado como REJECTED

    def test_accepted_retcodes_is_immutable(self):
        """frozenset — não pode ser mutado acidentalmente."""
        with pytest.raises(AttributeError):
            ACCEPTED_RETCODES.add(99999)  # type: ignore

    def test_magic_constant(self):
        assert MAGIC_VIBETRADING == 555501


# ── 3. error_dict() ─────────────────────────────────────────────────────────
class TestErrorDict:
    def test_produces_blocked_contract(self):
        d = vte.error_dict(vte.REASON_MISSING_STOP_LOSS, "sl=0")
        assert d["status"] == "BLOCKED"
        assert d["reason"] == "MISSING_STOP_LOSS"
        assert d["ticket"] == 0
        assert d["detail"] == "sl=0"

    def test_extra_fields_merged(self):
        d = vte.error_dict(vte.REASON_NOT_CONFIRMED, "sem ticket",
                           symbol="WIN", retcode=0)
        assert d["symbol"] == "WIN"
        assert d["retcode"] == 0

    def test_safe_for_safe_buy_classification(self):
        """O dict BLOCKED deve ser classificável pelo safe_buy existente.

        safe_buy trata status != FILLED via _classify_error. BLOCKED não casa
        nenhum padrão conhecido → vira UNKNOWN → retry/abort, NÃO crash.
        Validamos só que o formato é consumível (não propaga exceção).
        """
        d = vte.error_dict(vte.REASON_MISSING_STOP_LOSS)
        assert isinstance(d, dict)
        assert "status" in d and "reason" in d


# ── 4. from_exception() ────────────────────────────────────────────────────
class TestFromException:
    def test_missing_stop_loss_maps(self):
        d = vte.from_exception(vte.MissingStopLossError("sl None"))
        assert d["reason"] == "MISSING_STOP_LOSS"
        assert "sl None" in d["detail"]

    def test_not_confirmed_maps(self):
        d = vte.from_exception(vte.OrderNotConfirmedError("ticket 0"))
        assert d["reason"] == "NOT_CONFIRMED"
        assert "ticket 0" in d["detail"]

    def test_rejected_maps(self):
        d = vte.from_exception(vte.OrderRejectedError("retcode 10004"))
        assert d["reason"] == "REJECTED_BY_RETCODE"

    def test_unknown_error_fallback(self):
        d = vte.from_exception(vte.OrderError("genérico"))
        assert d["reason"] == "UNKNOWN_ERROR"


# ── 5. Round-trip exceção ↔ dict ───────────────────────────────────────────
class TestRoundTrip:
    def test_dict_then_exception_consistent(self):
        """Quem valida explicitamente pode levantar a exceção; quem usa o
        contrato recebe o dict. Ambos carregam a mesma informação."""
        exc = vte.MissingStopLossError("BUY sem SL")
        d = vte.from_exception(exc)
        # Reconstrói exceção a partir do dict
        assert d["reason"] == vte.REASON_MISSING_STOP_LOSS
        re_exc = vte.MissingStopLossError(d["detail"])
        assert str(re_exc) == str(exc)
