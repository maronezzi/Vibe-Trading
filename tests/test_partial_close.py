"""
test_partial_close.py — Wave N+2A (2026-07-08)

Valida o pathway de TP1 + partial close:
  1. mt5_orchestrator.partial_close: chama Wine corretamente + trata erros.
  2. safe_partial_close: retry Lei 3 + idempotência em POSITION_NOT_FOUND.
  3. Logic de TP1 em autotrader: heurística + estado (tp1_done, remaining).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for p in (
    str(PROJECT_ROOT),
    str(PROJECT_ROOT / "mt5"),
    str(PROJECT_ROOT / "core"),
):
    if p not in sys.path:
        sys.path.insert(0, p)


# ═══════════════════════════════════════════════════════════
# mt5_orchestrator.partial_close (mockando Wine subprocess)
# ═══════════════════════════════════════════════════════════

def test_partial_close_calls_wine_with_correct_args():
    """Verifica que orchestrator passa args certos ao subprocess Wine."""
    from mt5 import mt5_orchestrator

    with mock.patch.object(mt5_orchestrator, "_run_wine") as mock_wine:
        mock_wine.return_value = {
            "status": "ok",
            "ticket": 12345,
            "closed_volume": 0.5,
            "remaining_volume": 0.5,
            "exit_price": 5020.0,
            "profit": 50.0,
        }
        result = mt5_orchestrator.partial_close("WDON26", 12345, 0.5)

    # Args: partial_close, symbol, str(ticket), str(volume)
    mock_wine.assert_called_once()
    call_args = mock_wine.call_args.args
    assert call_args[0] == mt5_orchestrator.EXECUTOR_WIN
    assert call_args[1] == "partial_close"
    assert call_args[2] == "WDON26"
    assert call_args[3] == "12345"
    assert call_args[4] == "0.5"
    assert result["status"] == "ok"
    assert result["remaining_volume"] == 0.5


def test_partial_close_rejects_zero_or_negative():
    """Volume <= 0 vira erro imediato sem chamar Wine."""
    from mt5 import mt5_orchestrator
    with mock.patch.object(mt5_orchestrator, "_run_wine") as mock_wine:
        r1 = mt5_orchestrator.partial_close("WDON26", 1, 0)
        r2 = mt5_orchestrator.partial_close("WDON26", 1, -1.0)
    assert r1["status"] == "error"
    assert r2["status"] == "error"
    assert mock_wine.call_count == 0


def test_partial_close_propagates_wine_error():
    """Se Wine retorna erro, partial_close devolve dict error original."""
    from mt5 import mt5_orchestrator
    with mock.patch.object(mt5_orchestrator, "_run_wine") as mock_wine:
        mock_wine.return_value = {"status": "error", "error": "Invalid volume"}
        r = mt5_orchestrator.partial_close("WDON26", 1, 0.5)
    assert r["status"] == "error"
    assert r["error"] == "Invalid volume"


# ═══════════════════════════════════════════════════════════
# safe_partial_close (mockando mt5_orchestrator.partial_close)
# ═══════════════════════════════════════════════════════════

def test_safe_partial_close_succeeds_first_try():
    """Sucesso na 1ª tentativa — sem retry."""
    from mt5 import mt5_error_recovery
    with mock.patch(
        "mt5.mt5_orchestrator.partial_close",
        return_value={"status": "ok", "ticket": 1, "closed_volume": 0.5,
                       "remaining_volume": 0.5},
    ):
        r = mt5_error_recovery.safe_partial_close("WDON26", 1, 0.5)
    assert r["status"] == "ok"


def test_safe_partial_close_retries_on_requote():
    """REQUOTE → retry até sucesso."""
    from mt5 import mt5_error_recovery
    responses = iter([
        {"status": "error", "error": "Requote prices changed"},
        {"status": "ok", "ticket": 1, "closed_volume": 0.5,
         "remaining_volume": 0.5},
    ])
    with mock.patch(
        "mt5.mt5_orchestrator.partial_close",
        side_effect=lambda *a, **kw: next(responses),
    ):
        r = mt5_error_recovery.safe_partial_close("WDON26", 1, 0.5)
    assert r["status"] == "ok"


def test_safe_partial_close_position_not_found_is_already_closed():
    """POSITION_NOT_FOUND é idempotente: ticket sumiu = objetivo atingido."""
    from mt5 import mt5_error_recovery
    with mock.patch(
        "mt5.mt5_orchestrator.partial_close",
        return_value={"status": "error", "error": "position not found"},
    ):
        r = mt5_error_recovery.safe_partial_close("WDON26", 1, 0.5)
    assert r["status"] == "already_closed"
    assert r["remaining_volume"] == 0.0


def test_safe_partial_close_invalid_volume_no_retry():
    """INVALID_VOLUME é permanente — não retry."""
    from mt5 import mt5_error_recovery
    with mock.patch(
        "mt5.mt5_orchestrator.partial_close",
        return_value={"status": "error", "error": "Invalid volume amount"},
    ) as m:
        r = mt5_error_recovery.safe_partial_close("WDON26", 1, 0.5)
    assert r["status"] == "error"
    # 1 só tentativa (não retry)
    assert m.call_count == 1


def test_safe_partial_close_exhausts_retries():
    """UNKNOWN error → retry até MAX_RETRIES (3) e devolve último resultado."""
    from mt5 import mt5_error_recovery
    with mock.patch(
        "mt5.mt5_orchestrator.partial_close",
        return_value={"status": "error", "error": "broker offline"},
    ) as m:
        r = mt5_error_recovery.safe_partial_close("WDON26", 1, 0.5)
    assert r["status"] == "error"
    # MAX_RETRIES = 3 → 3 tentativas
    assert m.call_count == 3
