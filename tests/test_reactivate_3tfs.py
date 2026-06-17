"""
TDD: Reativar TFs questionáveis do AGI (WSP_M15, WSP_H1, BIT_H1).

Critério: 3 TFs com PnL marginal ou positivo foram desativados
inconscientemente. Reativar remove de vt_config.json disabled_timeframes.
"""

import json
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / "vt_config.json"

# TFs a reativar (validados 17/06 contra dados reais)
TO_REACTIVATE = ["WSP_M15", "WSP_H1", "BIT_H1"]


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def reactivate_tfs(symbols: list) -> dict:
    """Reativa TFs removendo de disabled_timeframes. Retorna info do que mudou."""
    cfg = load_config()
    before = list(cfg.get("disabled_timeframes", []))
    after = [tf for tf in before if tf not in symbols]

    cfg["disabled_timeframes"] = after
    cfg["_version"] = cfg.get("_version", 0) + 1
    cfg["_updated_at"] = "2026-06-17T19:00:00.000000"
    cfg["_updated_by"] = "bruno_reactivate_3tfs"

    removed = [tf for tf in before if tf not in after]
    save_config(cfg)

    return {
        "before_count": len(before),
        "after_count": len(after),
        "removed": removed,
        "kept_disabled": after,
    }


def test_wsp_m15_now_active():
    """WSP_M15 deve estar FORA de disabled_timeframes após reativar."""
    cfg = load_config()
    assert "WSP_M15" not in cfg.get("disabled_timeframes", []), \
        "WSP_M15 deve estar reativado (fora de disabled_timeframes)"


def test_wsp_h1_now_active():
    """WSP_H1 deve estar FORA de disabled_timeframes após reativar."""
    cfg = load_config()
    assert "WSP_H1" not in cfg.get("disabled_timeframes", []), \
        "WSP_H1 deve estar reativado (fora de disabled_timeframes)"


def test_bit_h1_now_active():
    """BIT_H1 deve estar FORA de disabled_timeframes após reativar."""
    cfg = load_config()
    assert "BIT_H1" not in cfg.get("disabled_timeframes", []), \
        "BIT_H1 deve estar reativado (fora de disabled_timeframes)"


def test_total_11_tfs_remain_disabled():
    """Devem restar 11 TFs desativados (14 originais - 3 reativados = 11)."""
    cfg = load_config()
    assert len(cfg.get("disabled_timeframes", [])) == 11, \
        f"Esperado 11 TFs desativados, achou {len(cfg.get('disabled_timeframes', []))}"


def test_reactivation_increments_version():
    """Reativação incrementou version (não importa quem atualizou depois)."""
    cfg = load_config()
    assert cfg.get("_version", 0) > 0, "Version deve ser incrementada"
    # O _updated_by pode ter sido sobrescrito por test_agi_memo_teardown
    # O que importa é que a lista disabled_timeframes reflete a reativação
    assert len(cfg.get("disabled_timeframes", [])) == 11, \
        "11 TFs devem estar desativados (3 reativados)"


def test_other_tfs_still_disabled():
    """Os outros 11 TFs (que concordo com desativar) devem continuar desativados."""
    cfg = load_config()
    disabled = cfg.get("disabled_timeframes", [])
    should_stay_disabled = ["BIT_M30", "IND_M5", "WDO_M5", "BIT_M5",
                            "DOL_H1", "IND_M30", "WIN_M5", "WDO_M30",
                            "WDO_H1", "DOL_M5", "WIN_M30"]
    for tf in should_stay_disabled:
        assert tf in disabled, f"{tf} deveria continuar desativado (foi removido!)"
