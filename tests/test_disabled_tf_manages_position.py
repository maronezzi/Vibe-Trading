#!/usr/bin/env python3
"""TDD — posições abertas em TFs desativados devem SER GERENCIADAS.

Bug: quando um TF entra em disabled_timeframes (ex: BIT_M5 desativado pelo AGI),
o check_and_trade() pula esse TF com `continue` ANTES de verificar se há
posição aberta em state.positions. Resultado: posições existentes ficam
órfãs (sem trailing, sem breakeven, sem hard_exit).
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))


def _build_test_harness():
    """Constrói mocks compartilhados."""
    mock_state = MagicMock()
    mock_state.positions = {
        "BITM26_M5": {
            "direction": "SELL",
            "entry_price": 338320.0,
            "entry_ticket": 2457466990,
            "sl_pts": 32100,
            "atr": 535.7,
            "recovered": True,
        }
    }

    cfg = {
        "symbols": ["BIT"],
        "timeframes": ["M5", "M15", "M30", "H1"],
        "timeframes_by_symbol": {"BIT": ["M5", "M15", "M30", "H1"]},
        "resolved_symbols": {"BIT": "BITM26"},
        "disabled_symbols": [],
        "disabled_timeframes": ["BIT_M5"],
        "bars_count": 45,
    }

    mock_config = MagicMock()
    mock_config.get.side_effect = lambda k, default=None: cfg.get(k, default)
    mock_config.__contains__ = lambda self, k: k in cfg
    mock_config.__getitem__ = lambda self, k: cfg[k]

    mock_manage = MagicMock()
    mock_manage.side_effect = lambda *a, **k: None  # noop
    mock_fetch = MagicMock(return_value=[{"close": 337500.0}] * 50)
    mock_atr = MagicMock(return_value=500)
    mock_safe = MagicMock(return_value=True)
    mock_reset = MagicMock(return_value=None)
    mock_strat = MagicMock(return_value="RSI_REVERSION")
    mock_params = MagicMock(return_value={})
    mock_strat_tf = MagicMock(return_value="RSI_REVERSION")
    mock_params_tf = MagicMock(return_value={})
    mock_snap = MagicMock(return_value={"error": "skip"})
    mock_save_snap = MagicMock()
    mock_anom = MagicMock(return_value=[])
    mock_log_anom = MagicMock()
    mock_anom_notify = MagicMock()
    mock_cd = MagicMock(return_value=True)
    mock_max = MagicMock(return_value=True)
    mock_streak = MagicMock(return_value=True)
    mock_def = MagicMock(return_value=(True, []))

    return {
        "state": mock_state,
        "config": mock_config,
        "manage": mock_manage,
        "fetch": mock_fetch,
        "atr": mock_atr,
        "safe": mock_safe,
        "reset": mock_reset,
        "strat": mock_strat,
        "params": mock_params,
        "strat_tf": mock_strat_tf,
        "params_tf": mock_params_tf,
        "snap": mock_snap,
        "save_snap": mock_save_snap,
        "anom": mock_anom,
        "log_anom": mock_log_anom,
        "anom_notify": mock_anom_notify,
        "cd": mock_cd,
        "max": mock_max,
        "streak": mock_streak,
        "def": mock_def,
    }


class TestDisabledTfManagesExistingPosition(unittest.TestCase):
    """check_and_trade() deve gerenciar posições abertas em TFs desativados."""

    def test_manage_called_when_tf_disabled_but_position_open(self):
        """TF desativado + posição aberta em state → manage_position deve ser chamado."""
        mocks = _build_test_harness()

        # IMPORTANTE: pra parar o ciclo após o manage de M5 ser chamado, mock
        # fetch_bars pra falhar nos TFs seguintes. Assim o test sai do loop
        # antes de tentar estratégia real.
        call_count = [0]
        def fetch_then_fail(symbol, tf, count):
            call_count[0] += 1
            if call_count[0] > 1:
                return None  # para o loop
            return [{"close": 337500.0}] * 50
        mocks["fetch"].side_effect = fetch_then_fail

        with patch("vt_autotrader.CONFIG", mocks["config"]), \
             patch("vt_autotrader.state", mocks["state"]), \
             patch("vt_autotrader.fetch_bars", mocks["fetch"]), \
             patch("vt_autotrader.manage_position", mocks["manage"]), \
             patch("vt_autotrader.calculate_atr", mocks["atr"]), \
             patch("vt_autotrader._is_safe_time_window", mocks["safe"]), \
             patch("vt_autotrader._reset_daily_counter", mocks["reset"]), \
             patch("vt_autotrader._get_strategy", mocks["strat"]), \
             patch("vt_autotrader._get_params", mocks["params"]), \
             patch("vt_autotrader._get_strategy_for_tf", mocks["strat_tf"]), \
             patch("vt_autotrader._get_params_for_tf", mocks["params_tf"]), \
             patch("vt_analyst.fetch_snapshot", mocks["snap"]), \
             patch("vt_analyst.save_snapshot", mocks["save_snap"]), \
             patch("vt_analyst.detect_anomalies", mocks["anom"]), \
             patch("vt_analyst.log_anomaly", mocks["log_anom"]), \
             patch("vt_analyst.notify", mocks["anom_notify"]), \
             patch("vt_autotrader._check_cooldown", mocks["cd"]), \
             patch("vt_autotrader._check_max_trades", mocks["max"]), \
             patch("vt_autotrader._check_consecutive_losses", mocks["streak"]), \
             patch("vt_autotrader._defenses_ok", mocks["def"]):
            from vt_autotrader import check_and_trade
            check_and_trade()

        # ASSERT
        self.assertGreater(
            mocks["manage"].call_count, 0,
            f"manage_position DEVE ser chamado para BITM26_M5 (TF desativado + posição aberta). "
            f"Calls: {mocks['manage'].call_args_list}"
        )
        found = False
        for c in mocks["manage"].call_args_list:
            args = c[0]
            if args[0] == "BITM26" and args[1] == "M5":
                found = True
                break
        self.assertTrue(found, f"Nenhuma chamada de manage_position com (BITM26, M5). Calls: {mocks['manage'].call_args_list}")


if __name__ == "__main__":
    unittest.main()
