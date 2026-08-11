"""
test_eod_close_all_sweep.py
============================
TDD: EOD close (close_all_and_report) deve fechar TAMBÉM posições ghost/órfãs
que existem no MT5 mas não estão no state do bot.

CONTEXTO (Bruno 10/08): "quando operamos nos trades reais, uma operação real
ficou aberta" — no 1º dia REAL (05/08) o state foi reconstruído vazio às
09:21:12 (C2 do lesson_learning_2026-08-05.md). Posições abertas depois disso
podem não estar no state. O close_all_and_report() itera state.positions —
se o bot não conhece a posição, ela NÃO é fechada no EOD 16:45.

O executor mt5/mt5_executor.py JÁ TEM cmd_close_all() que fecha TODAS as
posições via mt5.positions_get() (sem filtro por símbolo/state) — broker-truth.
Este teste garante que close_all_and_report() chama esse sweep para cobrir
ghosts, além do loop normal do state.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

# import direto do módulo (não dispara daemon)
from core.vt_autotrader import close_all_and_report


class TestEodCloseAllSweep:
    def test_close_all_and_report_calls_close_all_sweep(self):
        """EOD deve chamar mt5_orchestrator.close_all() (sweep broker-truth)."""
        # state vazio (bot não conhece nenhuma posição — cenário ghost)
        with patch("core.vt_autotrader.state") as state_mock, \
             patch("core.vt_autotrader.safe_close") as safe_close_mock, \
             patch("core.vt_autotrader.tick") as tick_mock, \
             patch("core.vt_autotrader.log_exit") as log_exit_mock, \
             patch("core.vt_autotrader.history",
                   return_value={"history": []}) as history_mock, \
             patch("core.vt_autotrader.import_mt5_history",
                   return_value=0) as import_mock, \
             patch("core.vt_autotrader.sync_fees_from_mt5",
                   return_value=0) as fees_mock, \
             patch("core.vt_autotrader.get_events_daily_summary",
                   return_value=None) as ev_mock, \
             patch("core.vt_autotrader.get_daily_summary",
                   return_value={"total_trades": 0, "net_pnl": 0.0,
                                 "best_trade": 0.0, "worst_trade": 0.0,
                                 "win_rate": 0.0}) as db_mock, \
             patch("core.vt_autotrader.close_all",
                   return_value={"status": "ok", "closed": 0,
                                 "total": 0}) as close_all_mock:
            # state vazio: positions={} — cenário onde o bot perdeu o tracking
            state_mock.positions = {}
            state_mock.daily_pnl = 0.0
            state_mock.trade_count = 0
            state_mock.wins = 0
            state_mock.losses = 0
            state_mock.consecutive_losses = {}
            state_mock.closed = False

            close_all_and_report(close_source="EOD_CLOSE", exit_reason="EOD_16:45")

            # O sweep broker-truth DEVE ser chamado mesmo com state vazio
            close_all_mock.assert_called_once()

    def test_close_all_sweep_called_with_state_positions(self):
        """Com posições no state, o sweep também roda (defesa em profundidade)."""
        with patch("core.vt_autotrader.state") as state_mock, \
             patch("core.vt_autotrader.safe_close",
                   return_value={"status": "ok"}) as safe_close_mock, \
             patch("core.vt_autotrader.tick",
                   return_value={"bid": 172000.0}) as tick_mock, \
             patch("core.vt_autotrader.log_exit",
                   return_value={"net_pnl": 10.0}) as log_exit_mock, \
             patch("core.vt_autotrader.history",
                   return_value={"history": []}) as history_mock, \
             patch("core.vt_autotrader.import_mt5_history",
                   return_value=0) as import_mock, \
             patch("core.vt_autotrader.sync_fees_from_mt5",
                   return_value=0) as fees_mock, \
             patch("core.vt_autotrader.get_events_daily_summary",
                   return_value=None) as ev_mock, \
             patch("core.vt_autotrader.get_daily_summary",
                   return_value={"total_trades": 0, "net_pnl": 0.0,
                                 "best_trade": 0.0, "worst_trade": 0.0,
                                 "win_rate": 0.0}) as db_mock, \
             patch("core.vt_autotrader.close_all",
                   return_value={"status": "ok", "closed": 0,
                                 "total": 0}) as close_all_mock:
            state_mock.positions = {
                "WINQ26_M15": {"symbol": "WINQ26", "tf": "M15",
                               "entry_price": 172000.0,
                               "trade_log_id": 1, "entry_ticket": "111"},
            }
            state_mock.daily_pnl = 0.0
            state_mock.trade_count = 0
            state_mock.wins = 0
            state_mock.losses = 0
            state_mock.consecutive_losses = {}
            state_mock.closed = False

            close_all_and_report(close_source="EOD_CLOSE", exit_reason="EOD_16:45")

            close_all_mock.assert_called_once()
            # fluxo normal preservado: safe_close por símbolo também rodou
            assert safe_close_mock.called
