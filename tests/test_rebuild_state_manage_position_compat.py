"""
test_rebuild_state_manage_position_compat.py
============================================

WAVE 14.2 — BUG FIX 2026-07-14 (Bruno): rebuild_state_from_mt5() NAO
populava campos hard-accessed por manage_position().

PROBLEMA:
    - manage_position() faz pos["atr"], pos["sl_pts"], pos["best_price"],
      pos["trail_on"], pos["bar_count"], pos["trade_log_id"] hard-access.
    - rebuild_state_from_mt5() (introduzido Wave 12) só populava
      {direction, entry_price, entry_ticket, entry_time, volume, tf, from_mt5_rebuild}.
    - Toda posição reconstruída do MT5 caía em KeyError na primeira
      chamada de manage_position, gerando crash loop a cada 30s.

SINTOMAS OBSERVADOS (autotrader.log 2026-07-14 12:57+):
    [2026-07-14 12:57:35] [ERRO] 'atr'
    KeyError: 'atr' (line 2375 manage_position)
    Repetido a cada 30s enquanto MT5 mantinha 1 pos WINQ26 aberta.

FIX:
    rebuild_state_from_mt5() agora popula todos os campos que
    manage_position() consome, com defaults fail-safe (atr tenta
    buscar de bars; best_price = entry_price; trail_on=False;
    bar_count estimado da idade; trade_log_id tenta achar no DB).

O QUE ESTE TESTE PROTEGE:
    1. test_rebuild_populates_atr_field
    2. test_rebuild_populates_sl_pts_field
    3. test_rebuild_populates_best_price_field
    4. test_rebuild_populates_trail_on_field
    5. test_rebuild_populates_bar_count_field
    6. test_rebuild_populates_trade_log_id_field
    7. test_manage_position_does_not_keyerror_on_rebuilt_position
"""
import sys
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


def _mt5_position_dict(ticket, symbol, direction="BUY", magic=555501,
                        comment="VibeTrading", volume=1.0, price_open=100.0,
                        time="2026-07-01 12:00:00"):
    """Helper: pos MT5 no formato mt5_executor.status() retorna."""
    return {
        "ticket": ticket,
        "symbol": symbol,
        "type": "0" if direction == "BUY" else "1",
        "volume": volume,
        "price_open": price_open,
        "price_current": price_open,
        "sl": 0.0,
        "tp": 0.0,
        "profit": 0.0,
        "swap": 0.0,
        "comment": comment,
        "time": time,
        "magic": magic,
        "identifier": ticket,
        "time_msc": 0,
        "reason": 0,
        "external_id": "",
    }


class _CompatTestBase(unittest.TestCase):
    def setUp(self):
        from core import vt_truth
        vt_truth._reset_caches_for_testing()

    def tearDown(self):
        from core import vt_truth
        vt_truth._reset_caches_for_testing()


# ==============================================================================
# 1-6. test_rebuild_populates_*_field
# ==============================================================================
class TestRebuildPopulatesAllManagePositionFields(_CompatTestBase):
    """rebuild_state_from_mt5() deve popular todos os campos
    que manage_position() faz hard-access."""

    def test_rebuild_populates_all_required_fields(self):
        """Wave 14.2 bug fix: pos reconstruída do MT5 precisa ter
        {atr, sl_pts, best_price, trail_on, bar_count, trade_log_id}"""
        from core import vt_autotrader

        # Monta 1 pos MT5 fictícia
        fake_pos = _mt5_position_dict(
            ticket=99999, symbol="WINQ26", direction="SELL",
            price_open=178000.0,
        )

        # Mock: _truth.get_open_positions retorna [fake_pos]
        # Mock: fetch_bars retorna lista vazia (atr fallback para 0)
        # Mock: DB connection nao tem trade aberto (trade_log_id = None)
        # Limpa state.positions (a funcao faz self.positions = {} internamente)
        vt_autotrader.state.positions = {}
        with patch(
            "core.vt_truth.get_open_positions",
            return_value=[_PosFromDict(fake_pos)],
        ):
            with patch.object(
                vt_autotrader, 'fetch_bars', return_value=[]
            ):
                with patch.object(
                    vt_autotrader, 'calculate_atr', return_value=0
                ):
                    n = vt_autotrader.state.rebuild_state_from_mt5()

        assert n == 1, f"Esperava 1 pos reconstruída, veio {n}"
        assert "WINQ26_M5" in vt_autotrader.state.positions
        pos = vt_autotrader.state.positions["WINQ26_M5"]

        # Campos ORIGINAIS (Wave 12)
        assert pos["direction"] == "SELL"
        assert pos["entry_price"] == 178000.0
        assert pos["entry_ticket"] == "99999"
        assert pos["volume"] == 1.0
        assert pos["tf"] == "M5"
        assert pos["from_mt5_rebuild"] is True

        # Campos NOVOS (Wave 14.2) — sem eles, manage_position() KeyError
        assert "atr" in pos, "FALTA atr — manage_position() linha 2375 KeyError"
        assert "sl_pts" in pos, "FALTA sl_pts"
        assert "best_price" in pos, "FALTA best_price"
        assert "trail_on" in pos, "FALTA trail_on"
        assert "bar_count" in pos, "FALTA bar_count"
        assert "trade_log_id" in pos, "FALTA trade_log_id"

    def test_manage_position_does_not_keyerror_on_rebuilt_position(self):
        """Simulação: pos reconstruída do MT5 NÃO pode dar KeyError
        quando manage_position() é chamada. Este é o bug live de 14/07."""
        from core import vt_autotrader

        # Pos reconstruída (depois do fix) — todos os campos
        pos = {
            "direction": "SELL",
            "entry_price": 178000.0,
            "entry_ticket": "99999",
            "entry_time": "2026-07-14 12:00:00",
            "volume": 1.0,
            "tf": "M5",
            "from_mt5_rebuild": True,
            "atr": 0,
            "sl_pts": 0,
            "best_price": 178000.0,
            "trail_on": False,
            "bar_count": 1,
            "trade_log_id": None,
        }

        # Stub: tick() retornando price válido (gestão continua)
        # Stub: safe_modify_sl* — não chama (porque tp1/trail guard)
        # Stub: notify_telegram — não chama
        with patch.object(vt_autotrader, 'tick', return_value={
            "bid": 177800.0, "ask": 177805.0, "last": 0.0
        }):
            with patch.object(
                vt_autotrader, 'safe_modify_sl_with_emergency_close',
                return_value={"status": "ok"},
            ):
                with patch.object(
                    vt_autotrader, 'notify_telegram', return_value=True,
                ):
                    try:
                        # O bug original: KeyError 'atr' na linha 2375
                        # O fix: 'atr' existe (=0), então atr=0,
                        # todos os guards atr>0 pulam, função retorna.
                        vt_autotrader.manage_position(
                            "WINQ26", "M5", pos,
                            current_atr=0, strategy="STRONG_TREND", params={},
                        )
                    except KeyError as e:
                        self.fail(
                            f"manage_position() ainda KeyError após fix: {e}. "
                            f"Campos faltando em pos: {set(['atr','sl_pts','best_price','trail_on','bar_count','trade_log_id']) - set(pos.keys())}"
                        )


# Helper: converte dict no formato esperado por rebuild_state_from_mt5()
class _PosFromDict:
    """Adapta um dict (formato executor.status) para um objeto com
    atributos acessíveis por getattribute (formato vt_truth.Position)."""
    def __init__(self, d):
        self._d = d
        self.symbol = d["symbol"]
        self.ticket = d["ticket"]
        self.direction = d["type"]  # "0"=BUY, "1"=SELL
        self.price_open = d["price_open"]
        self.price_current = d["price_current"]
        self.volume = d["volume"]
        self.open_time = d["time"]
        self.sl = d.get("sl", 0.0)
        self.tp = d.get("tp", 0.0)
        self.profit = d.get("profit", 0.0)
        self.magic = d.get("magic", 555501)
        self.comment = d.get("comment", "")


if __name__ == "__main__":
    unittest.main()
