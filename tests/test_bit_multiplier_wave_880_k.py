"""Testes do fix do multiplier BIT (Wave 880.K — Bruno 2026-07-20).

Bug corrigido: BITN26 vale R$ 0,01 por ponto (1 centavo), não R$ 1,00.
O fallback hardcoded em get_multiplier dizia BIT=1.0 (100x errado), e o
config também. Isso inflava o PnL reportado do BIT 100x — ex.: trade #50
de 20/07 (entry 329620 → exit 332120 = +2500 pts) aparecia como +R$ 2.500
quando o real era +R$ 25 (broker-truth).

Evidência empírica (trade #50, único com broker-truth hoje):
  pontos = 332120 - 329620 = +2500
  PnL broker = R$ 25,00  →  R$ 0,01/pt

WSP NÃO segue o padrão do BIT: WSP é Micro S&P 500 (R$2.50/pt), BIT é
Bitcoin (R$0.01/pt) — contratos distintos. O fix do WSP está em
test_wsp_multiplier_broker_truth.py (Bruno 11/08/2026).
"""
import sys
from pathlib import Path

_PROJECT = Path(__file__).resolve().parent.parent


def _read(path: str) -> str:
    return (_PROJECT / path).read_text(encoding="utf-8")


def test_bit_fallback_is_001_not_10():
    """Fallback hardcoded de BIT deve ser 0.01, não 1.0."""
    src = _read("core/vt_trade_log.py")
    # A linha _mults deve ter BIT: 0.01.
    assert '"BIT": 0.01' in src or "'BIT': 0.01" in src, (
        "fallback BIT deve ser 0.01 (Wave 880.K) — antes era 1.0 (100x errado)"
    )
    # E NÃO deve ter o valor antigo 1.0 atribuído a BIT.
    assert '"BIT": 1.0' not in src and "'BIT': 1.0" not in src, (
        "fallback BIT ainda tem valor antigo 1.0"
    )


def test_wsp_fallback_is_2_5():
    """WSP deve ser 2.5 (R$/pt do Micro S&P, NÃO 0.01 — não é BIT).

    Wave WSP-fix (Bruno 11/08/2026): antes era uma cópia errada do BIT (0.01),
    subestimando o PnL WSP em 250×. Broker-truth MT5 WSPU26 = 2.5 R$/pt.
    """
    src = _read("core/vt_trade_log.py")
    assert '"WSP": 2.5' in src or "'WSP': 2.5" in src, (
        "fallback WSP deve ser 2.5 (Micro S&P, broker-truth) — antes era 0.01 (cópia do BIT)"
    )


def test_w14_7_script_uses_correct_bit():
    """O script de config w14_7 deve gravar BIT=0.01 (não 1.0)."""
    src = _read("scripts/w14_7_fix_contract_specs_mult_20260720.py")
    assert '"BIT$": 0.01' in src or "'BIT$': 0.01" in src, (
        "w14_7 deve gravar BIT$=0.01"
    )
    # E não deve ter o valor errado.
    assert '"BIT$": 1.0' not in src and "'BIT$': 1.00" not in src, (
        "w14_7 não deve gravar BIT$=1.0 (era o bug)"
    )


def test_get_multiplier_fallback_is_truth_not_config():
    """Wave 880.G/K: get_multiplier deve usar o fallback hardcoded como verdade,
    não retornar direto o valor do config (que pode estar errado)."""
    src = _read("core/vt_trade_log.py")
    # Deve haver um warning de divergência (prova que compara config vs fallback).
    assert "diverge do" in src or "diverge" in src
    # E deve retornar fallback_mult no final (não config_mult).
    assert "return fallback_mult" in src


def test_get_multiplier_returns_correct_values():
    """Validação empírica end-to-end: get_multiplier retorna os valores corretos."""
    sys.path.insert(0, str(_PROJECT / "core"))
    try:
        #vt_trade_log importa vt_config_loader no top-level; conftest isola CONFIG_PATH
        # mas como importamos direto aqui, garantimos path do core.
        from vt_trade_log import get_multiplier  # type: ignore
        assert get_multiplier("BITN26") == 0.01, (
            "BITN26 deve ser R$ 0.01/pt (evidência trade #50: 2500pts = R$ 25)"
        )
        assert get_multiplier("WSPU26") == 2.5, (
            "WSPU26 deve ser R$ 2.5/pt (Micro S&P, broker-truth MT5) — não 0.01"
        )
        assert get_multiplier("WINQ26") == 0.20
        assert get_multiplier("WDON26") == 10.00
    finally:
        sys.path.remove(str(_PROJECT / "core"))


def test_empirical_evidence_bit_50():
    """Confirma o cálculo empírico do trade #50 (broker-truth).

    trade #50 BITN26 BUY: entry=329620, exit=332120
    pontos = +2500, PnL broker-truth = R$ 25,00
    → R$ 25,00 / 2500 pts = R$ 0,01/pt  ✓

    Antes do fix: pontos × mult(1.0) = R$ 2.500,00 (100x inflado).
    """
    entry = 329620.0
    exit_price = 332120.0
    direction = "BUY"
    broker_truth_pnl = 25.00  # da note do trade #50

    pontos = (exit_price - entry) if direction == "BUY" else (entry - exit_price)
    assert pontos == 2500

    # Com mult correto (0.01):
    correct_pnl = pontos * 0.01
    assert correct_pnl == 25.00
    assert correct_pnl == broker_truth_pnl, "PnL calculado deve bater com broker-truth"

    # Com mult errado (1.0) — o bug:
    inflated_pnl = pontos * 1.0
    assert inflated_pnl == 2500.00
    assert inflated_pnl == broker_truth_pnl * 100, (
        "mult=1.0 infla o PnL 100x (R$ 25 → R$ 2500)"
    )
