"""
test_starting_balance_helper.py
================================
Wave 1C.3 (Bruno 08/07): TDD do helper `core.vt_starting_balance` que fixa
o PnL fallback-balance do copilot.

Cobre:
1. get_today_starting_balance retorna None sem arquivo no path.
2. set + get roundtrip atomico (escreve, le, valor confere).
3. get retorna None se date do snapshot eh de outro dia (nao cruza dia).
4. set recusa overwrite no mesmo dia (idempotente — defesa contra restart
   mid-day sobrescrever baseline do helper caller).
5. set rejeita balance <= 0 (sanity check cobre 0, negativo, valor absurdo).

Tambem validamos: balance acima do teto (10M) eh rejeitado.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def tmp_balance_path(monkeypatch, tmp_path):
    """Redireciona core.vt_starting_balance.STARTING_BALANCE_PATH para tmp.

    Sem isso, os testes poluiriam /tmp/vt_intraday_starting_balance.json
    (caminho real do helper) e ainda por cima leriamos lixo de runs
    anteriores do autotrader. Com tmp_path, cada teste eh isolado.
    """
    import core.vt_starting_balance as sb

    fake = tmp_path / "vt_intraday_starting_balance.json"
    monkeypatch.setattr(sb, "STARTING_BALANCE_PATH", fake)
    return fake


def test_get_today_starting_balance_returns_none_when_no_file(tmp_balance_path):
    """Sem arquivo no path: get retorna None + log, caller cai no fallback."""
    from core.vt_starting_balance import get_today_starting_balance

    assert not tmp_balance_path.exists()
    result = get_today_starting_balance()
    assert result is None, (
        f"Esperava None quando arquivo nao existe, achou {result!r}"
    )


def test_set_then_get_roundtrip(tmp_balance_path):
    """set grava atomicamente + get le de volta valor exato (mesma data)."""
    from core.vt_starting_balance import (
        get_today_starting_balance,
        set_today_starting_balance,
    )

    today = date.today().isoformat()
    ok = set_today_starting_balance(1002230.57, source="test_roundtrip")
    assert ok is True, "set deveria retornar True em primeira escrita"

    # Arquivo criado no path tmp do fixture.
    assert tmp_balance_path.exists(), "set deveria ter criado arquivo"

    # Schema confere.
    payload = json.loads(tmp_balance_path.read_text(encoding="utf-8"))
    assert payload["date"] == today
    assert payload["balance"] == pytest.approx(1002230.57)
    assert payload["source"] == "test_roundtrip"
    assert "ts" in payload and isinstance(payload["ts"], str)

    # Roundtrip: get le o mesmo valor.
    result = get_today_starting_balance()
    assert result == pytest.approx(1002230.57), (
        f"Roundtrip falhou: esperava 1002230.57, achou {result!r}"
    )


def test_get_returns_none_if_date_is_yesterday(tmp_balance_path):
    """Snapshot de outro dia NAO cruza: get retorna None (caller usa fallback)."""
    from core.vt_starting_balance import get_today_starting_balance

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    payload = {
        "date": yesterday,
        "balance": 1000000.00,
        "ts": "2026-07-01T09:00:00",
        "source": "manual",
    }
    tmp_balance_path.write_text(json.dumps(payload), encoding="utf-8")

    assert tmp_balance_path.exists()
    result = get_today_starting_balance()
    assert result is None, (
        f"Snapshot de {yesterday} deveria ser descartado quando hoje eh "
        f"{date.today().isoformat()}; achou {result!r}"
    )


def test_set_refuses_overwrite_same_day(tmp_balance_path):
    """Idempotencia: set eh NO-OP quando ja existe snapshot do mesmo dia,
    NAO pisa em valor ja gravado (defesa contra restart mid-day)."""
    from core.vt_starting_balance import (
        get_today_starting_balance,
        set_today_starting_balance,
    )

    first = set_today_starting_balance(1000000.00, source="primeiro")
    assert first is True

    # Tentar sobrescrever mesmo dia com valor diferente — deve recusar.
    second = set_today_starting_balance(9999999.99, source="tentativa_overwrite")
    assert second is False, (
        "set deveria recusar overwrite no mesmo dia (retornar False)"
    )

    # Valor original preservado em disco.
    payload = json.loads(tmp_balance_path.read_text(encoding="utf-8"))
    assert payload["balance"] == pytest.approx(1000000.00), (
        f"Valor em disco NAO deveria ter sido sobrescrito. "
        f"Achou {payload['balance']}"
    )
    assert payload["source"] == "primeiro", (
        f"Source NAO deveria ter sido sobrescrito. Achou {payload['source']}"
    )

    # E get retorna o valor original, nao o novo.
    assert get_today_starting_balance() == pytest.approx(1000000.00)


@pytest.mark.parametrize(
    "bad_balance",
    [0.0, -1.0, -1000000.0, -0.01],
    ids=["zero", "menos_um", "muito_negativo", "quase_zero_negativo"],
)
def test_set_rejects_zero_or_negative(tmp_balance_path, bad_balance):
    """Sanity check: set REJEITA balance <= 0 com ValueError (cobre 0,
    negativo, valor minimo invalido). Tambem rejeita > 10M (cobre absurdo)."""
    from core.vt_starting_balance import set_today_starting_balance

    with pytest.raises(ValueError, match=r"sanity|faixa"):
        set_today_starting_balance(bad_balance, source="sanity_test")

    # E NAO deve ter escrito nada no disco.
    assert not tmp_balance_path.exists(), (
        f"set NAO deveria gravar nada com balance={bad_balance}; "
        f"mas {tmp_balance_path} existe"
    )


def test_set_rejects_above_sanity_ceiling(tmp_balance_path):
    """Sanity check: balance >= 10_000_000 (teto) eh rejeitado — protege
    contra retorno lixoso do MT5 (ex.: -1 ou 1e9 quando broker offline)."""
    from core.vt_starting_balance import set_today_starting_balance

    with pytest.raises(ValueError, match=r"sanity|faixa"):
        set_today_starting_balance(10_000_000.0, source="above_ceiling")
    with pytest.raises(ValueError, match=r"sanity|faixa"):
        set_today_starting_balance(99_999_999.99, source="way_above_ceiling")
    assert not tmp_balance_path.exists()


def test_set_rejects_non_numeric_balance(tmp_balance_path):
    """Sanity defensivo: tipo nao-numerico levanta ValueError (cobre None,
    string, lista — caller pode ter passado resultado de JSON parse falho)."""
    from core.vt_starting_balance import set_today_starting_balance

    with pytest.raises(ValueError, match=r"float|faixa"):
        set_today_starting_balance(None, source="none")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=r"float|faixa"):
        set_today_starting_balance("1002230.57", source="string")  # type: ignore[arg-type]
    assert not tmp_balance_path.exists()


def test_get_returns_none_for_malformed_json(tmp_balance_path):
    """Robustez: JSON malformado nao derruba o caller — get retorna None."""
    from core.vt_starting_balance import get_today_starting_balance

    tmp_balance_path.write_text("{ isto nao eh json valido", encoding="utf-8")
    result = get_today_starting_balance()
    assert result is None, (
        f"Esperava None para JSON malformado, achou {result!r}"
    )


def test_get_returns_none_for_missing_balance_field(tmp_balance_path):
    """Robustez: arquivo de hoje mas sem campo `balance` valido -> None."""
    from core.vt_starting_balance import get_today_starting_balance

    payload = {
        "date": date.today().isoformat(),
        "ts": "2026-07-08T09:00:00",
        "source": "manual",
        # 'balance' intencionalmente ausente / invalido
    }
    tmp_balance_path.write_text(json.dumps(payload), encoding="utf-8")
    assert get_today_starting_balance() is None
