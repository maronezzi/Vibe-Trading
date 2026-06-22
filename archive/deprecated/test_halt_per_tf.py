#!/usr/bin/env python3
"""TDD — HALT deve ser por symbol+TF, não por símbolo global.

Bug: quando DOL_M5 atinge 3 losses consecutivas, DOL_M30 também é pausado
porque consecutive_losses e halt_until usam o símbolo resolvido como key
(ex: "DOLN26") em vez de "DOL_M5".

Correção: cada par symbol_tf deve ter seu próprio contador de losses,
seu próprio halt_until, e seu próprio threshold (max_consecutive_losses_by_tf).
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

# Importa o módulo UMA VEZ
import vt_autotrader


def _mock_state(consecutive_losses=None, halt_until=None):
    """Configura state mockado no módulo vt_autotrader."""
    vt_autotrader.state.consecutive_losses = consecutive_losses or {}
    vt_autotrader.state.halt_until = halt_until or {}
    vt_autotrader.state.max_consecutive_losses = 3


def _mock_config(extra=None):
    """Configura CONFIG mockado no módulo vt_autotrader."""
    cfg = {
        "symbols": ["DOL"],
        "timeframes": ["M5", "M15", "M30", "H1"],
        "timeframes_by_symbol": {"DOL": ["M5", "M15", "M30", "H1"]},
        "resolved_symbols": {"DOL": "DOLN26"},
        "disabled_symbols": [],
        "disabled_timeframes": [],
        "bars_count": 45,
        "max_consecutive_losses": 3,
        "max_consecutive_losses_by_tf": {
            "DOL_M5": 3,
            "DOL_M15": 3,
            "DOL_M30": 5,
            "DOL_H1": 5,
        },
    }
    if extra:
        cfg.update(extra)

    # Patcha o CONFIG no módulo
    mock = MagicMock()
    mock.get.side_effect = lambda k, default=None: cfg.get(k, default)
    mock.__contains__ = lambda self, k: k in cfg
    mock.__getitem__ = lambda self, k: cfg[k]
    vt_autotrader.CONFIG = mock
    return mock


def test_per_tf_halt_independent():
    """DOL_M5 com 3 losses não deve bloquear DOL_M30 (limite=5)."""
    _mock_config()
    _mock_state(
        consecutive_losses={"DOL_M5": 3},
        halt_until={"DOL_M5": datetime.now() + timedelta(hours=1)},
    )

    # DOL_M5 deve estar bloqueado (3/3 losses)
    result_m5 = vt_autotrader._check_consecutive_losses("DOLN26", "M5")
    assert result_m5 == False, f"DOL_M5 deveria estar bloqueado (3 losses), retornou {result_m5}"

    # DOL_M30 deve estar LIVRE (0/5 losses — key diferente)
    result_m30 = vt_autotrader._check_consecutive_losses("DOLN26", "M30")
    assert result_m30 == True, f"DOL_M30 deveria estar livre (0 losses), retornou {result_m30}"

    print("✅ test_per_tf_halt_independent PASSOU")


def test_per_tf_loss_tracking_separate():
    """Losses em DOL_M5 não devem contar para DOL_M30."""
    _mock_config()

    # 2 losses em DOL_M5 — ainda livre
    _mock_state(consecutive_losses={"DOL_M5": 2})
    result_m5 = vt_autotrader._check_consecutive_losses("DOLN26", "M5")
    assert result_m5 == True, f"DOL_M5 com 2/3 losses deveria ser livre"

    # DOL_M30 com 0 losses — livre
    result_m30 = vt_autotrader._check_consecutive_losses("DOLN26", "M30")
    assert result_m30 == True, f"DOL_M30 com 0 losses deveria ser livre"

    # 3 losses em DOL_M5 — agora bloqueado
    vt_autotrader.state.consecutive_losses["DOL_M5"] = 3
    result_m5 = vt_autotrader._check_consecutive_losses("DOLN26", "M5")
    assert result_m5 == False, f"DOL_M5 com 3/3 losses deveria estar bloqueado"

    # DOL_M30 ainda livre
    result_m30 = vt_autotrader._check_consecutive_losses("DOLN26", "M30")
    assert result_m30 == True, f"DOL_M30 ainda deveria estar livre"

    print("✅ test_per_tf_loss_tracking_separate PASSOU")


def test_per_tf_halt_respects_threshold():
    """DOL_M30 com threshold=5 não deve parar até 5 losses."""
    _mock_config()

    # 4 losses em DOL_M30 (limite=5) — ainda livre
    _mock_state(consecutive_losses={"DOL_M30": 4})
    result = vt_autotrader._check_consecutive_losses("DOLN26", "M30")
    assert result == True, f"DOL_M30 com 4/5 losses deveria ser livre"

    # 5 losses em DOL_M30 — agora bloqueado
    vt_autotrader.state.consecutive_losses["DOL_M30"] = 5
    result = vt_autotrader._check_consecutive_losses("DOLN26", "M30")
    assert result == False, f"DOL_M30 com 5/5 losses deveria estar bloqueado"

    print("✅ test_per_tf_halt_respects_threshold PASSOU")


def test_fallback_to_global_threshold():
    """Se TF não está em max_consecutive_losses_by_tf, usa global."""
    _mock_config(extra={
        "symbols": ["WSP"],
        "timeframes_by_symbol": {"WSP": ["M5"]},
        "resolved_symbols": {"WSP": "WSPM26"},
        "max_consecutive_losses_by_tf": {
            # WSP_M5 não está aqui — deve usar global=3
        },
    })

    # 3 losses em WSP_M5 — deve usar global=3 e bloquear
    _mock_state(consecutive_losses={"WSP_M5": 3})
    result = vt_autotrader._check_consecutive_losses("WSPM26", "M5")
    assert result == False, f"WSP_M5 com 3 losses (global=3) deveria estar bloqueado"

    print("✅ test_fallback_to_global_threshold PASSOU")


def test_win_clears_halt_for_tf():
    """Win deve limpar halt apenas para o TF específico."""
    _mock_config()
    _mock_state(
        consecutive_losses={"DOL_M5": 3, "DOL_M30": 2},
        halt_until={"DOL_M5": datetime.now() + timedelta(hours=1)},
    )

    # DOL_M5 bloqueado
    result = vt_autotrader._check_consecutive_losses("DOLN26", "M5")
    assert result == False, "DOL_M5 deveria estar bloqueado"

    # Simula win em DOL_M5
    vt_autotrader.state.consecutive_losses["DOL_M5"] = 0
    vt_autotrader.state.halt_until.pop("DOL_M5", None)

    # DOL_M5 agora livre
    result = vt_autotrader._check_consecutive_losses("DOLN26", "M5")
    assert result == True, "DOL_M5 deveria estar livre após win"

    # DOL_M30 não foi afetado (2 losses)
    result = vt_autotrader._check_consecutive_losses("DOLN26", "M30")
    assert result == True, "DOL_M30 deveria estar livre (2/4 losses)"

    print("✅ test_win_clears_halt_for_tf PASSOU")


if __name__ == "__main__":
    tests = [
        test_per_tf_halt_independent,
        test_per_tf_loss_tracking_separate,
        test_per_tf_halt_respects_threshold,
        test_fallback_to_global_threshold,
        test_win_clears_halt_for_tf,
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
            print(f"❌ {test.__name__} ERRO: {e}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"Resultado: {passed}/{passed+failed} passaram")
    if failed > 0:
        sys.exit(1)  # RED
    else:
        sys.exit(0)  # GREEN
