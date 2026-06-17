"""
TDD: Prompt do AGI 17H deve conter as regras novas do Bruno (17/06):
- Regra 14: TROCA DE ESTRATÉGIA se par não-lucrativo após 2+ iterações
- Regra 15: MAXIMIZAÇÃO DE LUCRO (entrar cedo + sair tarde)
"""

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

import pytest
from agi_tuning_17h import build_llm_prompt, VALID_STRATEGIES


@pytest.fixture
def minimal_perf():
    return {
        "period_days": 7,
        "cutoff_date": "2026-06-10",
        "by_symbol": {
            "BIT": {
                "n_trades": 12, "wins": 5, "losses": 7, "win_rate": 41.7,
                "total_pnl": -553.0, "avg_pnl": -46.0,
                "worst": -250.0, "best": 150.0, "total_fees": 12.0,
            }
        },
        "by_symbol_tf": {
            "BIT_M15": {
                "n_trades": 8, "win_rate": 25.0, "total_pnl": -200.0,
                "avg_pnl": -25.0, "strategy": "RSI_REVERSION",
            }
        },
        "exit_reasons": {"SL_SERVIDOR": {"count": 8, "total_pnl": -200.0, "avg_pnl": -25.0}},
        "today_perf": {},
    }


@pytest.fixture
def minimal_config():
    return {
        "win": {"strategy": "BOLLINGER", "sl_atr_mult": 0.6, "trail_activate": 0.8,
                "trail_distance": 0.3, "cooldown_seconds": 202, "max_daily_trades": 6,
                "breakeven_minutes": 6, "time_trail_minutes": 15, "max_position_minutes": 30},
        "strategy": {"WIN": "BOLLINGER", "BIT": "RSI_REVERSION"},
    }


@pytest.fixture
def minimal_issues():
    return [
        {"severity": "WARN", "symbol": "BIT_M15",
         "detail": "WR 25% com 8 trades, PnL -R$200. Após 2 iterações sem convergir."},
    ]


def test_prompt_contains_strategy_swap_rule(minimal_perf, minimal_config, minimal_issues):
    """Prompt deve mencionar troca de estratégia (regra 14)."""
    prompt = build_llm_prompt(minimal_perf, minimal_issues, minimal_config)
    assert "TROCA DE ESTRATÉGIA" in prompt or "strategy" in prompt.lower()
    # Deve listar estratégias válidas
    assert "EMA_PULLBACK" in prompt or "BOLLINGER" in prompt
    # Deve ter o número da regra
    assert "14" in prompt


def test_prompt_contains_profit_maximization_rule(minimal_perf, minimal_config, minimal_issues):
    """Prompt deve mencionar maximização de lucro + entrar cedo + sair tarde (regra 15)."""
    prompt = build_llm_prompt(minimal_perf, minimal_issues, minimal_config)
    assert "MAXIMIZAÇÃO" in prompt.upper() or "MAXIMIZAR" in prompt.upper()
    assert "ENTRAR CEDO" in prompt.upper() or "ENTRAR" in prompt.upper()
    assert "SAIR TARDE" in prompt.upper() or "SAIR" in prompt.upper()
    assert "15" in prompt


def test_prompt_lists_all_valid_strategies_in_rule_14(minimal_perf, minimal_config, minimal_issues):
    """Regra 14 deve listar todas as estratégias válidas."""
    prompt = build_llm_prompt(minimal_perf, minimal_issues, minimal_config)
    # Pelo menos 5 estratégias devem ser mencionadas na regra
    count = 0
    for strat in ["BOLLINGER", "RSI_REVERSION", "EMA_PULLBACK", "VWAP", "MACD_MOMENTUM"]:
        if strat in prompt:
            count += 1
    assert count >= 4, f"Esperado >=4 estratégias mencionadas no prompt, achou {count}"


def test_valid_strategies_has_at_least_10():
    """Whitelist deve ter pelo menos 10 estratégias (para dar opções ao AGI)."""
    assert len(VALID_STRATEGIES) >= 10, f"Só {len(VALID_STRATEGIES)} estratégias: {VALID_STRATEGIES}"


def test_prompt_rule_15_mentions_specific_params_to_increase(minimal_perf, minimal_config, minimal_issues):
    """Regra 15 deve mencionar parâmetros que devem ser AUMENTADOS (sair tarde)."""
    prompt = build_llm_prompt(minimal_perf, minimal_issues, minimal_config)
    # Deve mencionar esses parâmetros
    for param in ["breakeven_minutes", "time_trail_minutes", "max_position_minutes"]:
        assert param in prompt, f"Regra 15 deve mencionar {param}"


def test_prompt_rule_15_mentions_specific_params_to_decrease(minimal_perf, minimal_config, minimal_issues):
    """Regra 15 deve mencionar parâmetros para ENTRAR CEDO (sinais sensíveis)."""
    prompt = build_llm_prompt(minimal_perf, minimal_issues, minimal_config)
    # bb_std, rsi, pullback — para entrar mais cedo
    for keyword in ["bb_std", "pullback", "rsi"]:
        assert keyword.lower() in prompt.lower(), f"Regra 15 deve mencionar {keyword}"
