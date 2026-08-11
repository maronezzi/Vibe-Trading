"""
test_eod_double_check.py
========================
TDD: EOD close com DOUBLE-CHECK — após o sweep close_all(), verificar se o MT5
ficou realmente flat; se sobraram posições, fechar de novo (até N tentativas).

CONTEXTO (Bruno 10/08): "Adicione um Double check para fechar, manda o comando
aguarda um pouco e analisa e manda de novo" — o fechamento EOD não deve confiar
em UMA única chamada: fechar → aguardar → analisar (status()) → se ainda tem
posição, fechar novamente. Protege contra close que retorna ok mas o MT5 não
processou (race), ou posições que reabriram no intervalo.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from core.vt_autotrader import close_all_and_report


class TestEodDoubleCheck:
    def test_double_check_detects_remaining_and_closes_again(self):
        """status() mostra 1 posição remanescente após 1º close_all → fecha de novo."""
        # state vazio (cenário ghost) + close_all NÃO fecha (retorna closed=0)
        # mas status() ainda mostra 1 posição → double-check deve chamar close_all de novo
        with patch("core.vt_autotrader.state") as state_mock, \
             patch("core.vt_autotrader.safe_close",
                   return_value={"status": "ok"}) as safe_close_mock, \
             patch("core.vt_autotrader.tick",
                   return_value={"bid": 172000.0}) as tick_mock, \
             patch("core.vt_autotrader.log_exit",
                   return_value={"net_pnl": 0.0}) as log_exit_mock, \
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
                                 "total": 1}) as close_all_mock, \
             patch("core.vt_autotrader.status") as status_mock:
            # status() retorna 1 posição na 1ª verificação, 0 na 2ª
            status_mock.side_effect = [
                {"positions": [{"symbol": "WINQ26", "ticket": 999,
                                "volume": 1.0, "magic": 555501}]},
                {"positions": []},
                {"positions": []},
            ]
            state_mock.positions = {}
            state_mock.daily_pnl = 0.0
            state_mock.trade_count = 0
            state_mock.wins = 0
            state_mock.losses = 0
            state_mock.consecutive_losses = {}
            state_mock.closed = False

            close_all_and_report(close_source="EOD_CLOSE", exit_reason="EOD_16:45")

            # close_all chamado 2x: 1x sweep inicial + 1x double-check
            assert close_all_mock.call_count >= 2, \
                f"close_all chamado {close_all_mock.call_count}x — esperado >=2 (sweep + double-check)"

    def test_double_check_stops_when_flat(self):
        """status() mostra 0 posições na 1ª verificação → close_all chamado 1x só."""
        with patch("core.vt_autotrader.state") as state_mock, \
             patch("core.vt_autotrader.safe_close",
                   return_value={"status": "ok"}) as safe_close_mock, \
             patch("core.vt_autotrader.tick",
                   return_value={"bid": 172000.0}) as tick_mock, \
             patch("core.vt_autotrader.log_exit",
                   return_value={"net_pnl": 0.0}) as log_exit_mock, \
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
                                 "total": 0}) as close_all_mock, \
             patch("core.vt_autotrader.status") as status_mock:
            # status() sempre flat
            status_mock.return_value = {"positions": []}
            state_mock.positions = {}
            state_mock.daily_pnl = 0.0
            state_mock.trade_count = 0
            state_mock.wins = 0
            state_mock.losses = 0
            state_mock.consecutive_losses = {}
            state_mock.closed = False

            close_all_and_report(close_source="EOD_CLOSE", exit_reason="EOD_16:45")

            assert close_all_mock.call_count == 1, \
                f"close_all chamado {close_all_mock.call_count}x — esperado 1 (flat na 1ª verificação)"
