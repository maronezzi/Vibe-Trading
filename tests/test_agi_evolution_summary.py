"""
TDD: AGI 17H deve reportar EVOLUÇÃO e PERCENTUAL no Telegram.

Regra Bruno 17/06: usuário precisa ver quanto cada mudança impactou o PnL.
Formato esperado: "WIN: sl_atr_mult 0.6→0.78 (+R$ 250, +12%)"

Função helper a ser implementada em agi_tuning_17h.py:
- build_evolution_summary(applied, baseline_perf, current_perf) -> list[str]
"""

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

import pytest
from agi_tuning_17h import build_evolution_summary


# Fixtures
@pytest.fixture
def baseline_perf():
    return {
        "by_symbol": {
            "BIT": {"n_trades": 12, "total_pnl": -500.0, "win_rate": 25.0},
            "WIN": {"n_trades": 3, "total_pnl": 0.40, "win_rate": 33.3},
            "WDO": {"n_trades": 14, "total_pnl": 100.0, "win_rate": 43.0},
        }
    }


@pytest.fixture
def current_perf_improved():
    """BIT melhorou de -500 para -200 (redução de 60%)."""
    return {
        "by_symbol": {
            "BIT": {"n_trades": 12, "total_pnl": -200.0, "win_rate": 33.0},
            "WIN": {"n_trades": 3, "total_pnl": 0.40, "win_rate": 33.3},
            "WDO": {"n_trades": 14, "total_pnl": 100.0, "win_rate": 43.0},
        }
    }


@pytest.fixture
def applied_changes_bit():
    return [
        {
            "symbol": "BIT",
            "params": {"sl_atr_mult": 0.6, "cooldown_seconds": 300},
            "reason": "BIT perdendo muito, apertar SL",
            "applied": True,
        }
    ]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# build_evolution_summary: testes básicos
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_function_exists():
    """build_evolution_summary deve existir no agi_tuning_17h."""
    assert callable(build_evolution_summary)


def test_returns_list_of_strings(applied_changes_bit, baseline_perf, current_perf_improved):
    """Retorna lista de linhas formatadas."""
    result = build_evolution_summary(applied_changes_bit, baseline_perf, current_perf_improved)
    assert isinstance(result, list)
    assert all(isinstance(s, str) for s in result)


def test_shows_symbol_name(applied_changes_bit, baseline_perf, current_perf_improved):
    """Linha deve mencionar o symbol (BIT)."""
    result = build_evolution_summary(applied_changes_bit, baseline_perf, current_perf_improved)
    assert any("BIT" in line for line in result)


def test_shows_pnl_delta(applied_changes_bit, baseline_perf, current_perf_improved):
    """Linha deve mostrar delta PnL em R$ (BIT: -500 → -200 = +R$ 300)."""
    result = build_evolution_summary(applied_changes_bit, baseline_perf, current_perf_improved)
    # BIT melhorou de -500 para -200: delta = +300
    assert any("+300" in line or "300" in line for line in result)


def test_shows_percentage(applied_changes_bit, baseline_perf, current_perf_improved):
    """Linha deve mostrar % de evolução."""
    result = build_evolution_summary(applied_changes_bit, baseline_perf, current_perf_improved)
    # BIT -500 → -200: melhoria de 60%
    # Aceitar formatos: "60%", "+60%", "60.0%"
    text = " ".join(result)
    assert "60" in text and "%" in text


def test_handles_multiple_changes(baseline_perf, current_perf_improved):
    """Múltiplas mudanças devem gerar múltiplas linhas."""
    applied = [
        {"symbol": "BIT", "params": {"sl_atr_mult": 0.7}, "applied": True},
        {"symbol": "WIN", "params": {"bb_std": 2.5}, "applied": True},
    ]
    result = build_evolution_summary(applied, baseline_perf, current_perf_improved)
    # Deve ter 1 linha por mudança (pode ter cabeçalho/footer extras)
    assert len(result) >= 2


def test_handles_no_baseline():
    """Se symbol não está em baseline, ainda mostra linha (sem %)."""
    applied = [{"symbol": "NEW", "params": {"sl_atr_mult": 0.6}, "applied": True}]
    baseline = {"by_symbol": {}}
    current = {"by_symbol": {"NEW": {"total_pnl": 100.0}}}
    result = build_evolution_summary(applied, baseline, current)
    assert len(result) >= 1
    assert any("NEW" in line for line in result)


def test_handles_zero_baseline_pnl():
    """Se baseline_pnl=0, % não pode ser calculado (divisão por zero)."""
    applied = [{"symbol": "FLAT", "params": {"x": 1}, "applied": True}]
    baseline = {"by_symbol": {"FLAT": {"total_pnl": 0.0}}}
    current = {"by_symbol": {"FLAT": {"total_pnl": 100.0}}}
    result = build_evolution_summary(applied, baseline, current)
    # Deve mostrar "novo" ou "∞" em vez de tentar %
    text = " ".join(result)
    # Não pode quebrar
    assert "FLAT" in text
    # Não pode mostrar "inf" ou erro
    assert "error" not in text.lower()


def test_empty_applied_returns_empty(baseline_perf, current_perf_improved):
    """Sem mudanças aplicadas = lista vazia ou só cabeçalho."""
    result = build_evolution_summary([], baseline_perf, current_perf_improved)
    assert isinstance(result, list)


def test_regression_flag_visible(baseline_perf):
    """Se symbol PIOROU, deve estar claro (❌ ou seta ↓)."""
    applied = [{"symbol": "BIT", "params": {"x": 1}, "applied": True}]
    # BIT piorou de -500 para -600 (delta -100, regressão)
    current = {"by_symbol": {"BIT": {"total_pnl": -600.0}}}
    result = build_evolution_summary(applied, baseline_perf, current)
    text = " ".join(result)
    # Deve ter algum indicador de regressão
    has_regression_marker = "↓" in text or "❌" in text or "-100" in text or "-R$" in text or "regress" in text.lower()
    assert has_regression_marker


def test_improvement_flag_visible(baseline_perf):
    """Se symbol MELHOROU, deve estar claro (✅ ou seta ↑)."""
    applied = [{"symbol": "BIT", "params": {"x": 1}, "applied": True}]
    # BIT melhorou muito: -500 → +200 (delta +700, melhoria de 240%)
    current = {"by_symbol": {"BIT": {"total_pnl": 200.0}}}
    result = build_evolution_summary(applied, baseline_perf, current)
    text = " ".join(result)
    has_improvement_marker = "↑" in text or "✅" in text or "+700" in text or "+R$" in text
    assert has_improvement_marker
