#!/usr/bin/env python3
"""TDD — AGI 17h deve poder otimizar max_consecutive_losses_by_tf.

Testa:
1. LLM retornando max_consecutive_losses_by_tf → apply_changes escreve no config
2. Validação de bounds (2-6)
3. Merge: só atualiza pares mencionados, mantém os outros
4. dry_run não persiste
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from agi_tuning_17h import apply_changes


def _make_config(extra=None):
    cfg = {
        "_version": 508,
        "max_consecutive_losses": 3,
        "max_consecutive_losses_by_tf": {
            # 2026-06-19: DOL/IND removidos. Apenas 4 minis × 4 TFs.
            "WIN_M5": 3, "WIN_M15": 3, "WIN_M30": 4, "WIN_H1": 5,
            "BIT_M5": 3, "BIT_M15": 3, "BIT_M30": 4, "BIT_H1": 5,
            "WSP_M5": 3, "WSP_M15": 3, "WSP_M30": 4, "WSP_H1": 5,
            "WDO_M5": 3, "WDO_M15": 3, "WDO_M30": 4, "WDO_H1": 5,
        },
    }
    if extra:
        cfg.update(extra)
    return cfg


def test_llm_halt_per_tf_applied():
    """LLM retorna max_consecutive_losses_by_tf → deve ser aplicado no config."""
    config = _make_config()
    llm_result = {
        "analysis": "Ajuste HALT",
        "changes": [],
        "max_consecutive_losses_by_tf": {
            "WDO_M5": 4,   # 3→4 (mais tolerante) — WDO substitui DOL
            "WDO_M30": 3,  # 4→3 (menos tolerante) — WDO substitui DOL
            "WIN_H1": 6,   # 5→6 (máximo)
        },
    }

    with patch("agi_tuning_17h.load_config", return_value=config), \
         patch("agi_tuning_17h.save_full_config") as mock_save:
        applied = apply_changes(llm_result, config, dry_run=False)

    # Verifica que save_full_config foi chamado
    assert mock_save.called, "save_full_config deveria ter sido chamado"

    # Verifica que os valores foram atualizados no config in-memory
    assert config["max_consecutive_losses_by_tf"]["WDO_M5"] == 4, \
        f"WDO_M5 deveria ser 4, é {config['max_consecutive_losses_by_tf']['WDO_M5']}"
    assert config["max_consecutive_losses_by_tf"]["WDO_M30"] == 3, \
        f"WDO_M30 deveria ser 3, é {config['max_consecutive_losses_by_tf']['WDO_M30']}"
    assert config["max_consecutive_losses_by_tf"]["WIN_H1"] == 6, \
        f"WIN_H1 deveria ser 6, é {config['max_consecutive_losses_by_tf']['WIN_H1']}"

    # Verifica que os outros pares NÃO foram alterados
    assert config["max_consecutive_losses_by_tf"]["WIN_M5"] == 3, \
        "WIN_M5 não deveria ter mudado"

    print("✅ test_llm_halt_per_tf_applied PASSOU")


def test_halt_bounds_validation():
    """Valores fora de [2,6] devem ser clamped."""
    config = _make_config()
    llm_result = {
        "analysis": "Teste bounds",
        "changes": [],
        "max_consecutive_losses_by_tf": {
            "WDO_M5": 1,    # abaixo do min → deve virar 2 (WDO substitui DOL)
            "WIN_H1": 10,   # acima do max → deve virar 6
            "BIT_M15": 4,   # dentro do range → mantém
        },
    }

    with patch("agi_tuning_17h.load_config", return_value=config), \
         patch("agi_tuning_17h.save_full_config"):
        apply_changes(llm_result, config, dry_run=False)

    assert config["max_consecutive_losses_by_tf"]["WDO_M5"] == 2, \
        f"WDO_M5 deveria ser clamped para 2, é {config['max_consecutive_losses_by_tf']['WDO_M5']}"
    assert config["max_consecutive_losses_by_tf"]["WIN_H1"] == 6, \
        f"WIN_H1 deveria ser clamped para 6, é {config['max_consecutive_losses_by_tf']['WIN_H1']}"
    assert config["max_consecutive_losses_by_tf"]["BIT_M15"] == 4, \
        f"BIT_M15 deveria ser 4, é {config['max_consecutive_losses_by_tf']['BIT_M15']}"

    print("✅ test_halt_bounds_validation PASSOU")


def test_halt_dry_run_no_persist():
    """dry_run não deve chamar save_full_config."""
    config = _make_config()
    llm_result = {
        "analysis": "Teste dry-run",
        "changes": [],
        "max_consecutive_losses_by_tf": {"WDO_M5": 5},  # WDO substitui DOL
    }

    with patch("agi_tuning_17h.load_config", return_value=config), \
         patch("agi_tuning_17h.save_full_config") as mock_save:
        apply_changes(llm_result, config, dry_run=True)

    # dry_run NÃO deve persistir
    assert not mock_save.called, "save_full_config NÃO deveria ser chamado em dry_run"

    print("✅ test_halt_dry_run_no_persist PASSOU")


def test_halt_merge_preserves_others():
    """Só pares mencionados devem mudar; outros mantêm valor atual."""
    config = _make_config()
    llm_result = {
        "analysis": "Teste merge",
        "changes": [],
        "max_consecutive_losses_by_tf": {
            "WDO_M5": 5,  # WDO substitui DOL
            # Nenhum outro par mencionado
        },
    }

    with patch("agi_tuning_17h.load_config", return_value=config), \
         patch("agi_tuning_17h.save_full_config"):
        apply_changes(llm_result, config, dry_run=False)

    # WDO_M5 mudou
    assert config["max_consecutive_losses_by_tf"]["WDO_M5"] == 5

    # Todos os outros mantêm valor original (sem DOL)
    for key, expected in [
        ("WDO_M15", 3), ("WDO_M30", 4), ("WDO_H1", 5),
        ("WIN_M5", 3), ("WIN_M15", 3), ("WIN_M30", 4), ("WIN_H1", 5),
        ("BIT_M5", 3), ("BIT_M15", 3), ("BIT_M30", 4), ("BIT_H1", 5),
        ("WSP_M5", 3), ("WSP_M15", 3), ("WSP_M30", 4), ("WSP_H1", 5),
    ]:
        actual = config["max_consecutive_losses_by_tf"][key]
        assert actual == expected, f"{key} deveria ser {expected}, é {actual}"

    print("✅ test_halt_merge_preserves_others PASSOU")


if __name__ == "__main__":
    tests = [
        test_llm_halt_per_tf_applied,
        test_halt_bounds_validation,
        test_halt_dry_run_no_persist,
        test_halt_merge_preserves_others,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"❌ {test.__name__} FALHOU: {e}")
            failed += 1
        except Exception as e:
            print(f"❌ {test.__name__} ERRO: {type(e).__name__}: {e}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"Resultado: {passed}/{passed+failed} passaram")
    sys.exit(1 if failed else 0)
