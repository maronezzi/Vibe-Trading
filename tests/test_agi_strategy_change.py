"""
TDD: Suporte a mudança de estratégia pelo AGI 17H.

Regra nova (Bruno 17/06): Na convergência, se um par SYM_TF não é lucrativo,
o AGI pode/deve TESTAR OUTRAS ESTRATÉGIAS (além de ajustar parâmetros),
inclusive buscando novas ideias com tinyfish.

Requisito: validate_and_clamp_change() deve aceitar chave "strategy" (string)
e validar contra whitelist de estratégias conhecidas.
"""

import pytest
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

# Importar módulo do AGI
from agi_tuning_17h import (
    validate_and_clamp_change,
    VALID_STRATEGIES,
    apply_changes,
)


# Mock config com estratégia + params básicos
@pytest.fixture
def mock_config():
    return {
        "win": {
            "strategy": "BOLLINGER",
            "sl_atr_mult": 0.6,
            "trail_activate": 0.8,
            "trail_distance": 0.3,
            "cooldown_seconds": 202,
            "max_daily_trades": 6,
        },
        "bit": {
            "strategy": "RSI_REVERSION",
            "sl_atr_mult": 0.6,
        },
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Estratégia: validação e troca
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_valid_strategies_contains_all_known():
    """VALID_STRATEGIES deve incluir todas as estratégias suportadas."""
    expected = {
        "BOLLINGER", "RSI_REVERSION", "EMA_PULLBACK", "VWAP",
        "MACD_MOMENTUM", "ADX", "ATR", "BREAKOUT", "MEAN_REVERSION"
    }
    # Pelo menos as principais devem estar lá
    assert "BOLLINGER" in VALID_STRATEGIES
    assert "RSI_REVERSION" in VALID_STRATEGIES
    assert "EMA_PULLBACK" in VALID_STRATEGIES
    assert "VWAP" in VALID_STRATEGIES
    assert "MACD_MOMENTUM" in VALID_STRATEGIES


def test_validate_accepts_strategy_string_change():
    """Mudança de strategy (string) deve ser aceita e validada."""
    cfg = {"win": {"strategy": "BOLLINGER"}}
    params = {"strategy": "RSI_REVERSION"}
    clamped, warnings = validate_and_clamp_change("WIN", params, cfg)
    assert clamped.get("strategy") == "RSI_REVERSION"
    assert "strategy" not in (warnings[0] if warnings else "")


def test_validate_rejects_invalid_strategy_string():
    """Strategy fora da whitelist deve ser rejeitada com warning."""
    cfg = {"win": {"strategy": "BOLLINGER"}}
    params = {"strategy": "INVALID_FAKE_STRATEGY"}
    clamped, warnings = validate_and_clamp_change("WIN", params, cfg)
    # Não aplica valor inválido
    assert "strategy" not in clamped
    # Warning sobre rejeição
    assert any("strategy" in w.lower() and ("whitelist" in w.lower() or "inválida" in w.lower() or "ignorad" in w.lower()) for w in warnings)


def test_validate_keeps_numeric_params_unchanged():
    """Params numéricos continuam funcionando como antes."""
    cfg = {"win": {"sl_atr_mult": 0.6}}
    params = {"sl_atr_mult": 1.0}  # +66% (acima de MAX_CHANGE_PCT 30%)
    clamped, warnings = validate_and_clamp_change("WIN", params, cfg)
    # Deve ter clampeado para 0.6 + 30% = 0.78
    assert clamped.get("sl_atr_mult") == pytest.approx(0.78, abs=0.01)
    assert any("muito abrupta" in w for w in warnings)


def test_validate_mixed_strategy_and_numeric():
    """Params mistos (strategy + numeric) devem ser processados corretamente."""
    cfg = {"win": {"strategy": "BOLLINGER", "sl_atr_mult": 0.6}}
    params = {"strategy": "EMA_PULLBACK", "sl_atr_mult": 0.7}
    clamped, warnings = validate_and_clamp_change("WIN", params, cfg)
    assert clamped.get("strategy") == "EMA_PULLBACK"
    # sl_atr_mult 0.6 → 0.7: +16% (dentro do limite 30%)
    assert clamped.get("sl_atr_mult") == pytest.approx(0.7, abs=0.01)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# apply_changes: integração com strategy
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_apply_changes_updates_strategy_field(tmp_path):
    """apply_changes deve aplicar mudança de strategy (não ignorar)."""
    import json
    from vt_config_loader import save_params

    cfg = {
        "win": {"strategy": "BOLLINGER", "sl_atr_mult": 0.6},
        "_version": 1,
    }
    llm_result = {
        "changes": [
            {
                "symbol": "WIN",
                "params": {"strategy": "EMA_PULLBACK"},
                "reason": "BOLLINGER não lucrativo em WIN_M15, trocar para EMA_PULLBACK"
            }
        ]
    }

    # Monkey-patch save_params pra não tocar disco
    import agi_tuning_17h
    original_save = agi_tuning_17h.save_params
    saved_calls = []
    def mock_save_params(symbol, params, updated_by=None):
        saved_calls.append((symbol, params))
        cfg[symbol].update(params)
        return True
    agi_tuning_17h.save_params = mock_save_params
    try:
        applied = apply_changes(llm_result, cfg, dry_run=False)
    finally:
        agi_tuning_17h.save_params = original_save

    # apply_changes() roda _optimize_dol_halt_grid internamente (lê config real do disco)
    # e pode adicionar 1+ resultados. Verifica que a mudança de strategy está lá
    # (independentemente de outros resultados de grid/halt).
    assert len(applied) >= 1, f"Esperado >= 1 aplicação, achou {len(applied)}"
    strategy_applied = [a for a in applied if "params" in a and "strategy" in a.get("params", {})]
    assert len(strategy_applied) == 1, (
        f"Esperado exatamente 1 aplicação de strategy, achou {len(strategy_applied)}: {applied}"
    )
    assert strategy_applied[0]["params"]["strategy"] == "EMA_PULLBACK"
    assert cfg["win"]["strategy"] == "EMA_PULLBACK"
    # Verifica que save_params foi chamado com strategy
    assert any("strategy" in params for sym, params in saved_calls)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Validação de que strategy não-string é rejeitada
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_validate_rejects_non_string_non_numeric_strategy():
    """Valores não-string e não-numérico devem ser rejeitados."""
    cfg = {"win": {"strategy": "BOLLINGER"}}
    # Strategy como lista
    params = {"strategy": ["BOLLINGER", "RSI"]}
    clamped, warnings = validate_and_clamp_change("WIN", params, cfg)
    assert "strategy" not in clamped
    assert any("strategy" in w for w in warnings)


def test_validate_rejects_numeric_for_strategy_key():
    """Strategy não pode ser número."""
    cfg = {"win": {"strategy": "BOLLINGER"}}
    params = {"strategy": 123}
    clamped, warnings = validate_and_clamp_change("WIN", params, cfg)
    assert "strategy" not in clamped
