"""
TDD: Reativar TFs questionáveis do AGI.

Histórico:
- 17/06: 3 TFs reativados (WSP_M15, WSP_H1, BIT_H1) → 11 disabled
- 17/06: Experimento de troca de estratégia → 9 pares reativados → 2 disabled

Agora: apenas DOL_H1 e WDO_H1 permanecem desativados
(winners com <3 trades — amostra insuficiente para confiar).
"""

import json
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / "vt_config.json"


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def test_wsp_m15_now_active():
    """WSP_M15 deve estar FORA de disabled_timeframes."""
    cfg = load_config()
    assert "WSP_M15" not in cfg.get("disabled_timeframes", [])


def test_wsp_h1_now_active():
    """WSP_H1 deve estar FORA de disabled_timeframes."""
    cfg = load_config()
    assert "WSP_H1" not in cfg.get("disabled_timeframes", [])


def test_bit_h1_now_active():
    """BIT_H1 deve estar FORA de disabled_timeframes."""
    cfg = load_config()
    assert "BIT_H1" not in cfg.get("disabled_timeframes", [])


def test_total_2_tfs_remain_disabled():
    """Devem restar 2 TFs desativados (DOL_H1, WDO_H1 — winner com <3 trades)."""
    cfg = load_config()
    assert len(cfg.get("disabled_timeframes", [])) == 2, \
        f"Esperado 2 TFs desativados, achou {len(cfg.get('disabled_timeframes', []))}"


def test_reactivation_increments_version():
    """Version deve ser > 0 após reativações."""
    cfg = load_config()
    assert cfg.get("_version", 0) > 0, "Version deve ser incrementada"
    assert len(cfg.get("disabled_timeframes", [])) == 2, \
        "2 TFs devem estar desativados"


def test_experiment_reactivated_pairs():
    """Pares reativados pelo experimento devem estar ativos."""
    cfg = load_config()
    disabled = cfg.get("disabled_timeframes", [])
    # Estes foram reativados pelo experimento com winner positivo
    should_be_active = [
        "WDO_M5", "WDO_M30", "DOL_M5", "IND_M30",
        "BIT_M30", "BIT_M5", "WIN_M5", "WIN_M30", "IND_M5",
    ]
    for tf in should_be_active:
        assert tf not in disabled, f"{tf} deveria estar ativo (experimento encontrou winner)"


def test_only_insufficient_samples_stay_disabled():
    """Apenas pares com <3 trades no winner ficam desativados."""
    cfg = load_config()
    disabled = cfg.get("disabled_timeframes", [])
    # DOL_H1 e WDO_H1: winner tinha <3 trades
    assert "DOL_H1" in disabled, "DOL_H1 deve permanecer desativado (<3 trades)"
    assert "WDO_H1" in disabled, "WDO_H1 deve permanecer desativado (<3 trades)"


def test_strategy_swaps_applied():
    """Estratégias devem ter sido trocadas pelo experimento."""
    cfg = load_config()
    strategy_by_tf = cfg.get("strategy_by_tf", {})
    # Winners do experimento
    expected = {
        "WDO_M5": "MACD_MOMENTUM",
        "WDO_M30": "EMA_PULLBACK",
        "BIT_M30": "BOLLINGER",
        "BIT_M5": "EMA_PULLBACK",
        "WIN_M5": "EMA_PULLBACK",
        "WIN_M30": "EMA_PULLBACK",
        "IND_M5": "RSI_REVERSION",
    }
    for pair, strat in expected.items():
        assert strategy_by_tf.get(pair) == strat, \
            f"{pair}: esperado {strat}, achou {strategy_by_tf.get(pair)}"
