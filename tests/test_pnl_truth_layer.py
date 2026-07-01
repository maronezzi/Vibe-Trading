"""
test_pnl_truth_layer.py
========================
FASE 1 do refactor Vibe-Trading (data/architecture_proposal_2026_07_01.md
linha 280-320): PnL intraday deve vir do MT5 history (broker-truth), NAO
do DB SQLite. DB so pode ser usado como fallback explicito.

ESTES TESTES (RED -> GREEN):
- get_daily_pnl_truth() soma profit+commission+swap por deal
- get_daily_pnl_truth() tolera MT5 vazio (sem deals hoje)
- get_daily_pnl_truth() tolera erro do MT5 (Wine down, timeout)
- get_daily_pnl_truth() tem cache TTL (segunda chamada < 5s -> cache hit)
- get_daily_pnl_truth() invalida cache com force_refresh=True
- check_intraday_stats() usa MT5_HISTORY quando MT5 responde
- check_intraday_stats() usa DB_FALLBACK quando MT5 falha/vazio
- check_intraday_stats() expoe source no retorno

Por que este teste importa (regressao historica):
- Bruno reportou (junho/2026): valor do relatorio Telegram intraday
  diferente do valor real do MT5. Causa: check_intraday_stats() lia
  do DB, que tem trades com PnL=0 (GHOST/ORPHAN). MT5 ja tinha
  computado o PnL real mas o DB ainda nao.
- Solucao: ler MT5 history direto (broker-truth). DB so como fallback.
"""
import sys
import time
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "core"))


# ===== HELPERS =====

def _deal(ticket, profit, commission=0.0, swap=0.0, symbol="WINQ26", time_=None):
    """Monta um deal fake no formato do mt5_executor.cmd_history()."""
    return {
        "ticket": ticket,
        "symbol": symbol,
        "type": "BUY",
        "volume": 1.0,
        "price": 120000.0,
        "profit": profit,
        "swap": swap,
        "commission": commission,
        "fee": 0.0,
        "comment": "VibeTrading",
        "magic": 555501,
        "time": str(time_) if time_ is not None else "1719840300",
        "position_id": ticket,
    }


def _history_payload(deals):
    """Wrapper no formato que mt5_orchestrator.history() retorna."""
    return {"history": deals, "count": len(deals)}


def _empty_history():
    return {"history": [], "info": "sem deals desde 01/07/2026"}


# ===== TESTES =====

class TestGetDailyPnlTruthSums(unittest.TestCase):
    """get_daily_pnl_truth() deve somar profit+commission+swap dos deals."""

    def setUp(self):
        # Importar aqui para garantir patch aplicado
        from monitoring import vt_copilot
        self.copilot = vt_copilot
        self.copilot._invalidate_pnl_truth_cache()

    def test_sums_profit_commission_swap_per_deal(self):
        """3 deals: profit+commission+swap somados corretamente."""
        deals = [
            _deal(101, profit=100.0, commission=-2.50, swap=0.0),
            _deal(102, profit=-50.0, commission=-2.50, swap=-1.0),
            _deal(103, profit=75.0, commission=-2.50, swap=0.0),
        ]
        # Esperado: profit=(100-50+75)=125, commission=(-2.5*3)=-7.5, swap=-1
        # net = 125 + (-7.5) + (-1) = 116.50
        expected_net = 125.0 + (-7.5) + (-1.0)
        with patch.object(self.copilot, "mt5_history",
                          return_value=_history_payload(deals)):
            result = self.copilot.get_daily_pnl_truth(days=1, force_refresh=True)

        self.assertEqual(result["source"], "MT5_HISTORY")
        self.assertTrue(result["ok"])
        self.assertEqual(result["deals_total"], 3)
        self.assertAlmostEqual(result["pnl_profit"], 125.0, places=2)
        self.assertAlmostEqual(result["pnl_commission"], -7.5, places=2)
        self.assertAlmostEqual(result["pnl_swap"], -1.0, places=2)
        self.assertAlmostEqual(result["pnl_net"], expected_net, places=2)

    def test_single_deal_with_all_components(self):
        """1 deal: profit=200, commission=-5, swap=-3 -> net=192."""
        deals = [_deal(201, profit=200.0, commission=-5.0, swap=-3.0)]
        with patch.object(self.copilot, "mt5_history",
                          return_value=_history_payload(deals)):
            result = self.copilot.get_daily_pnl_truth(days=1, force_refresh=True)

        self.assertEqual(result["deals_total"], 1)
        self.assertAlmostEqual(result["pnl_net"], 192.0, places=2)

    def test_zero_deals_returns_mt5_empty(self):
        """MT5 retorna lista vazia -> source=MT5_EMPTY, pnl_net=0."""
        with patch.object(self.copilot, "mt5_history",
                          return_value=_empty_history()):
            result = self.copilot.get_daily_pnl_truth(days=1, force_refresh=True)

        self.assertEqual(result["source"], "MT5_EMPTY")
        self.assertEqual(result["deals_total"], 0)
        self.assertAlmostEqual(result["pnl_net"], 0.0, places=2)
        self.assertFalse(result["stale"])  # primeira chamada nao e cache


class TestGetDailyPnlTruthFallbacks(unittest.TestCase):
    """get_daily_pnl_truth() deve tolerar erros do MT5 sem crashar."""

    def setUp(self):
        from monitoring import vt_copilot
        self.copilot = vt_copilot
        self.copilot._invalidate_pnl_truth_cache()

    def test_mt5_returns_error_dict(self):
        """Wine down / timeout -> error={'error': 'timeout'} -> MT5_EMPTY."""
        with patch.object(self.copilot, "mt5_history",
                          return_value={"error": "timeout"}):
            result = self.copilot.get_daily_pnl_truth(days=1, force_refresh=True)

        self.assertEqual(result["source"], "MT5_EMPTY")
        self.assertFalse(result["ok"])
        self.assertIn("timeout", result["error"])

    def test_mt5_returns_invalid_type(self):
        """mt5_history retorna string (nao-dict) -> nao crasha."""
        with patch.object(self.copilot, "mt5_history",
                          return_value="alguma coisa estranha"):
            result = self.copilot.get_daily_pnl_truth(days=1, force_refresh=True)

        self.assertEqual(result["source"], "MT5_EMPTY")
        self.assertFalse(result["ok"])

    def test_mt5_history_raises_exception(self):
        """mt5_history lanca Exception -> capturada, nao propaga."""
        def boom(*args, **kwargs):
            raise RuntimeError("Wine crashed")
        with patch.object(self.copilot, "mt5_history", side_effect=boom):
            result = self.copilot.get_daily_pnl_truth(days=1, force_refresh=True)

        self.assertEqual(result["source"], "MT5_EMPTY")
        self.assertFalse(result["ok"])
        self.assertIn("Wine crashed", result["error"])

    def test_mt5_returns_none(self):
        """Caso degenerado: mt5_history retorna None."""
        with patch.object(self.copilot, "mt5_history", return_value=None):
            result = self.copilot.get_daily_pnl_truth(days=1, force_refresh=True)

        self.assertEqual(result["source"], "MT5_EMPTY")
        self.assertFalse(result["ok"])


class TestGetDailyPnlTruthCache(unittest.TestCase):
    """Cache TTL de 5s: segunda chamada dentro da janela NAO bate no MT5."""

    def setUp(self):
        from monitoring import vt_copilot
        self.copilot = vt_copilot
        self.copilot._invalidate_pnl_truth_cache()

    def test_cache_hit_within_ttl(self):
        """2a chamada < 5s -> usa cache (stale=True), nao chama MT5 de novo."""
        deals = [_deal(301, profit=100.0)]
        call_count = {"n": 0}

        def fake_history(*args, **kwargs):
            call_count["n"] += 1
            return _history_payload(deals)

        with patch.object(self.copilot, "mt5_history", side_effect=fake_history):
            r1 = self.copilot.get_daily_pnl_truth(days=1, force_refresh=True)
            self.assertFalse(r1["stale"])
            self.assertEqual(call_count["n"], 1)

            # 2a chamada (sem force_refresh) -> cache hit
            r2 = self.copilot.get_daily_pnl_truth(days=1, force_refresh=False)
            self.assertTrue(r2["stale"], "2a chamada dentro do TTL deve ser cache")
            self.assertEqual(call_count["n"], 1, "MT5 NAO deve ser chamado 2x")
            self.assertEqual(r2["pnl_net"], r1["pnl_net"])

    def test_force_refresh_bypasses_cache(self):
        """force_refresh=True ignora cache sempre."""
        deals = [_deal(302, profit=50.0)]
        with patch.object(self.copilot, "mt5_history",
                          return_value=_history_payload(deals)):
            self.copilot.get_daily_pnl_truth(days=1, force_refresh=True)
            # 2a chamada com force_refresh=True deve re-executar
            self.copilot.get_daily_pnl_truth(days=1, force_refresh=True)

        # Chamadas dentro do mesmo assertIsInstance nao contam chamadas; mock call_args lista
        # Mas como mt5_history foi chamado 2x, deals_total sempre sera igual.
        # Validacao real: cache foi invalidado -> stale=False na 2a
        with patch.object(self.copilot, "mt5_history",
                          return_value=_history_payload(deals)):
            r1 = self.copilot.get_daily_pnl_truth(days=1, force_refresh=True)
            r2 = self.copilot.get_daily_pnl_truth(days=1, force_refresh=True)
            # 2a com force_refresh=True -> NAO e cache
            self.assertFalse(r2["stale"])

    def test_invalidate_cache_function(self):
        """_invalidate_pnl_truth_cache() limpa cache forcado."""
        deals = [_deal(303, profit=10.0)]
        with patch.object(self.copilot, "mt5_history",
                          return_value=_history_payload(deals)):
            r1 = self.copilot.get_daily_pnl_truth(days=1, force_refresh=True)
            self.assertFalse(r1["stale"])

            # Invalida manualmente
            self.copilot._invalidate_pnl_truth_cache()

            r2 = self.copilot.get_daily_pnl_truth(days=1, force_refresh=False)
            # Cache foi invalidado -> NAO e cache hit
            self.assertFalse(r2["stale"])


class TestCheckIntradayStatsUsesMT5(unittest.TestCase):
    """check_intraday_stats() deve preferir MT5 history ao DB."""

    def setUp(self):
        from monitoring import vt_copilot
        self.copilot = vt_copilot
        self.copilot._invalidate_pnl_truth_cache()

        # DB temporario vazio (sem trades hoje)
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        self.db_path = Path(self.tmp_db.name)
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                symbol TEXT,
                timeframe TEXT,
                entry_time TEXT,
                exit_time TEXT,
                exit_reason TEXT DEFAULT 'TRAILING',
                net_pnl REAL
            )
        """)
        conn.commit()
        conn.close()

        # Helper MT5 mockado: balance/equity, 0 posicoes abertas
        self.fake_truth = {
            "balance": 1003185.67, "equity": 1003185.67,
            "margin_free": 1003185.67,
            "positions_open": [], "n_positions": 0, "pnl_flutuante": 0.0,
            "ts": datetime.now().isoformat(), "ok": True, "error": None,
        }

    def tearDown(self):
        if self.db_path.exists():
            self.db_path.unlink()

    def test_uses_mt5_history_when_available(self):
        """MT5 tem 2 deals hoje -> check_intraday_stats usa eles, nao DB."""
        deals = [
            _deal(401, profit=100.0, commission=-2.50, swap=0.0, time_=1719840300),
            _deal(402, profit=-50.0, commission=-2.50, swap=-1.0, time_=1719840400),
        ]
        # Esperado: net = 100 + (-2.5) + 0 + (-50) + (-2.5) + (-1) = 44.0
        expected_pnl = 100.0 + (-2.5) + 0.0 + (-50.0) + (-2.5) + (-1.0)
        with patch.object(self.copilot, "mt5_history",
                          return_value=_history_payload(deals)), \
             patch.object(self.copilot, "get_truth_from_mt5",
                          return_value=self.fake_truth), \
             patch.object(self.copilot, "DB_PATH", self.db_path):
            stats = self.copilot.check_intraday_stats()

        self.assertEqual(stats["source"], "MT5_HISTORY",
                         f"Esperado MT5_HISTORY, achou {stats['source']}")
        self.assertEqual(stats["ops"], 2)
        self.assertEqual(stats["wins"], 1)
        self.assertEqual(stats["losses"], 1)
        self.assertAlmostEqual(stats["pnl_realized"], expected_pnl, places=2)
        self.assertEqual(stats["deals_total"], 2)
        self.assertIsNone(stats["truth_error"])

    def test_fallback_to_db_when_mt5_empty(self):
        """MT5 retorna vazio (sem deals hoje) -> cai no DB."""
        today = datetime.now().strftime("%Y-%m-%d")
        # Insere 1 trade de HOJE no DB
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO trades (symbol, timeframe, entry_time, exit_time, net_pnl) "
            "VALUES (?, ?, ?, ?, ?)",
            ("WINQ26", "M5",
             f"{today} 09:30:00", f"{today} 09:45:00", 25.0),
        )
        conn.commit()
        conn.close()

        with patch.object(self.copilot, "mt5_history",
                          return_value=_empty_history()), \
             patch.object(self.copilot, "get_truth_from_mt5",
                          return_value=self.fake_truth), \
             patch.object(self.copilot, "DB_PATH", self.db_path):
            stats = self.copilot.check_intraday_stats()

        self.assertEqual(stats["source"], "DB_FALLBACK")
        self.assertEqual(stats["ops"], 1)
        self.assertAlmostEqual(stats["pnl_realized"], 25.0, places=2)
        # truth_error deve ter alguma string
        self.assertIsNotNone(stats["truth_error"])

    def test_fallback_to_db_when_mt5_errors(self):
        """MT5 retorna erro (Wine down) -> cai no DB."""
        today = datetime.now().strftime("%Y-%m-%d")
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO trades (symbol, timeframe, entry_time, exit_time, net_pnl) "
            "VALUES (?, ?, ?, ?, ?)",
            ("WINQ26", "M5",
             f"{today} 09:30:00", f"{today} 09:45:00", -15.0),
        )
        conn.commit()
        conn.close()

        with patch.object(self.copilot, "mt5_history",
                          return_value={"error": "timeout"}), \
             patch.object(self.copilot, "get_truth_from_mt5",
                          return_value=self.fake_truth), \
             patch.object(self.copilot, "DB_PATH", self.db_path):
            stats = self.copilot.check_intraday_stats()

        self.assertEqual(stats["source"], "DB_FALLBACK")
        self.assertAlmostEqual(stats["pnl_realized"], -15.0, places=2)

    def test_returns_source_field(self):
        """check_intraday_stats() expoe source no retorno (contrato)."""
        deals = [_deal(501, profit=10.0)]
        with patch.object(self.copilot, "mt5_history",
                          return_value=_history_payload(deals)), \
             patch.object(self.copilot, "get_truth_from_mt5",
                          return_value=self.fake_truth), \
             patch.object(self.copilot, "DB_PATH", self.db_path):
            stats = self.copilot.check_intraday_stats()

        self.assertIn("source", stats)
        self.assertIn(stats["source"], ("MT5_HISTORY", "DB_FALLBACK"))

    def test_mt5_truth_overrides_db_drift(self):
        """REGRESSAO BRUNO: DB tem PnL=0 (GHOST) mas MT5 tem PnL real -> usa MT5."""
        today = datetime.now().strftime("%Y-%m-%d")
        # DB tem trade com PnL=0 (GHOST/ORPHAN — drift classico)
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO trades (symbol, timeframe, entry_time, exit_time, net_pnl) "
            "VALUES (?, ?, ?, ?, ?)",
            ("WINQ26", "M5",
             f"{today} 09:30:00", f"{today} 09:45:00", 0.0),
        )
        conn.commit()
        conn.close()

        # MT5 retorna 1 deal com profit REAL = +50
        deals = [_deal(601, profit=50.0, commission=-2.0, time_=1719840300)]
        expected = 50.0 + (-2.0)  # net = 48
        with patch.object(self.copilot, "mt5_history",
                          return_value=_history_payload(deals)), \
             patch.object(self.copilot, "get_truth_from_mt5",
                          return_value=self.fake_truth), \
             patch.object(self.copilot, "DB_PATH", self.db_path):
            stats = self.copilot.check_intraday_stats()

        # Source deve ser MT5 (NUNCA DB) porque MT5 respondeu OK
        self.assertEqual(stats["source"], "MT5_HISTORY")
        # E o PnL reportado deve ser o do MT5, NAO 0 do DB
        self.assertAlmostEqual(stats["pnl_realized"], expected, places=2)
        self.assertNotAlmostEqual(stats["pnl_realized"], 0.0, places=2,
                                  msg="NUNCA deve mostrar 0.0 do DB quando MT5 tem dados")


class TestCheckIntradayStatsOpenPositions(unittest.TestCase):
    """pnl_total = pnl_realized + open_pnl (via get_truth_from_mt5)."""

    def setUp(self):
        from monitoring import vt_copilot
        self.copilot = vt_copilot
        self.copilot._invalidate_pnl_truth_cache()
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        self.db_path = Path(self.tmp_db.name)
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                symbol TEXT, timeframe TEXT,
                entry_time TEXT, exit_time TEXT,
                exit_reason TEXT DEFAULT 'TRAILING', net_pnl REAL
            )
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        if self.db_path.exists():
            self.db_path.unlink()

    def test_open_pnl_from_truth_helper(self):
        """pnl_total = realized (MT5) + open_pnl (get_truth_from_mt5)."""
        deals = [_deal(701, profit=100.0)]
        truth_with_open = {
            "balance": 1000000.0, "equity": 999990.0,
            "margin_free": 999990.0,
            "positions_open": [{"symbol": "WINQ26", "profit": -10.0}],
            "n_positions": 1, "pnl_flutuante": -10.0,
            "ts": datetime.now().isoformat(), "ok": True, "error": None,
        }
        with patch.object(self.copilot, "mt5_history",
                          return_value=_history_payload(deals)), \
             patch.object(self.copilot, "get_truth_from_mt5",
                          return_value=truth_with_open), \
             patch.object(self.copilot, "DB_PATH", self.db_path):
            stats = self.copilot.check_intraday_stats()

        self.assertEqual(stats["open_count"], 1)
        self.assertAlmostEqual(stats["open_pnl"], -10.0, places=2)
        # pnl_total = 100 (realized) + (-10) (open) = 90
        self.assertAlmostEqual(stats["pnl_total"], 90.0, places=2)

    def test_open_pnl_zero_when_mt5_down(self):
        """Se get_truth_from_mt5 falha, open_count=0, open_pnl=0 (sem crash)."""
        deals = [_deal(702, profit=10.0)]
        truth_down = {
            "balance": 0, "equity": 0, "margin_free": 0,
            "positions_open": [], "n_positions": 0, "pnl_flutuante": 0.0,
            "ts": datetime.now().isoformat(), "ok": False, "error": "MT5 down",
        }
        with patch.object(self.copilot, "mt5_history",
                          return_value=_history_payload(deals)), \
             patch.object(self.copilot, "get_truth_from_mt5",
                          return_value=truth_down), \
             patch.object(self.copilot, "DB_PATH", self.db_path):
            stats = self.copilot.check_intraday_stats()

        self.assertEqual(stats["open_count"], 0)
        self.assertAlmostEqual(stats["open_pnl"], 0.0, places=2)
        # realized ainda funciona
        self.assertAlmostEqual(stats["pnl_realized"], 10.0, places=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)