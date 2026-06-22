"""Test AGI enhanced analysis — signal_detail, SL effectiveness, direction.

Valida que collect_performance() agora inclui:
1. signal_analysis: correlação RSI/ATR na entrada com resultado
2. sl_analysis: efetividade do SL (entry_sl vs exit_price)
3. direction_analysis: BUY vs SELL performance
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))


def _create_test_db(trades: list[dict]) -> str:
    """Cria DB temporário com trades de teste."""
    fd, path = tempfile.mkstemp(suffix=".db")
    import os
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            entry_ticket TEXT, exit_ticket TEXT, magic_number INTEGER,
            symbol TEXT NOT NULL, direction TEXT NOT NULL, volume REAL NOT NULL,
            timeframe TEXT, entry_time TEXT NOT NULL, entry_price REAL NOT NULL,
            entry_sl REAL, exit_time TEXT, exit_price REAL, exit_reason TEXT,
            exit_sl_price REAL, gross_pnl REAL, fees REAL, swap REAL,
            net_pnl REAL, is_day_trade INTEGER, asset_type TEXT,
            multiplier REAL, strategy TEXT, signal_detail TEXT,
            raw_entry_json TEXT, raw_exit_json TEXT, notes TEXT,
            created_at TEXT, updated_at TEXT
        )
    """)
    for i, t in enumerate(trades, 1):
        conn.execute("""
            INSERT INTO trades (id, symbol, direction, volume, timeframe,
                entry_time, entry_price, entry_sl, exit_time, exit_price,
                exit_reason, net_pnl, fees, strategy, signal_detail,
                is_day_trade, asset_type, multiplier)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            i, t["symbol"], t["direction"], t.get("volume", 1.0),
            t.get("timeframe", "M5"), t["entry_time"], t["entry_price"],
            t.get("entry_sl"), t.get("exit_time"), t.get("exit_price"),
            t.get("exit_reason", "SL_SERVIDOR"), t.get("net_pnl", 0),
            t.get("fees", 0), t.get("strategy", "RSI_REVERSION"),
            json.dumps(t.get("signal_detail", {})),
            1, "FUTURE", t.get("multiplier", 1.0)
        ))
    conn.commit()
    conn.close()
    return path


class TestSignalAnalysis(unittest.TestCase):
    """Testa que collect_performance extrai dados de signal_detail."""

    def setUp(self):
        self.trades = [
            {
                "symbol": "WINQ26", "direction": "BUY", "timeframe": "M5",
                "entry_time": "2026-06-20 10:00:00", "entry_price": 170000,
                "exit_time": "2026-06-20 10:30:00", "exit_price": 170500,
                "net_pnl": 100.0, "strategy": "RSI_REVERSION",
                "signal_detail": {"rsi": 22.0, "atr": 300.0, "sl_pts": 500},
            },
            {
                "symbol": "WINQ26", "direction": "BUY", "timeframe": "M5",
                "entry_time": "2026-06-21 10:00:00", "entry_price": 171000,
                "exit_time": "2026-06-21 10:30:00", "exit_price": 170500,
                "net_pnl": -100.0, "strategy": "RSI_REVERSION",
                "signal_detail": {"rsi": 78.0, "atr": 800.0, "sl_pts": 500},
            },
            {
                "symbol": "WDOQ26", "direction": "SELL", "timeframe": "M15",
                "entry_time": "2026-06-20 11:00:00", "entry_price": 5100,
                "exit_time": "2026-06-20 11:30:00", "exit_price": 5080,
                "net_pnl": 200.0, "strategy": "VWAP",
                "signal_detail": {"rsi": 75.0, "atr": 15.0, "sl_pts": 200},
            },
        ]
        self.db_path = _create_test_db(self.trades)

    def tearDown(self):
        import os
        os.unlink(self.db_path)

    def test_collect_performance_has_signal_analysis(self):
        """collect_performance deve retornar chave signal_analysis."""
        with patch("agi_tuning_17h.DB_PATH", Path(self.db_path)):
            from agi_tuning_17h import collect_performance
            result = collect_performance(days=7)
        self.assertIn("signal_analysis", result)

    def test_signal_analysis_has_avg_rsi_by_outcome(self):
        """signal_analysis deve ter avg_rsi_win e avg_rsi_loss por símbolo."""
        with patch("agi_tuning_17h.DB_PATH", Path(self.db_path)):
            from agi_tuning_17h import collect_performance
            result = collect_performance(days=7)
        sa = result.get("signal_analysis", {})
        self.assertIn("WIN", sa)
        win_data = sa["WIN"]
        self.assertIn("avg_rsi_win", win_data)
        self.assertIn("avg_rsi_loss", win_data)

    def test_signal_analysis_has_avg_atr(self):
        """signal_analysis deve ter avg_atr_win e avg_atr_loss."""
        with patch("agi_tuning_17h.DB_PATH", Path(self.db_path)):
            from agi_tuning_17h import collect_performance
            result = collect_performance(days=7)
        sa = result.get("signal_analysis", {})
        win_data = sa.get("WIN", {})
        self.assertIn("avg_atr_win", win_data)
        self.assertIn("avg_atr_loss", win_data)

    def test_signal_analysis_correct_values(self):
        """RSI 22 (win) e RSI 78 (loss) → avg_rsi_win=22, avg_rsi_loss=78 para WIN."""
        with patch("agi_tuning_17h.DB_PATH", Path(self.db_path)):
            from agi_tuning_17h import collect_performance
            result = collect_performance(days=7)
        sa = result["signal_analysis"]["WIN"]
        self.assertAlmostEqual(sa["avg_rsi_win"], 22.0, places=1)
        self.assertAlmostEqual(sa["avg_rsi_loss"], 78.0, places=1)


class TestSLAnalysis(unittest.TestCase):
    """Testa que collect_performance extrai efetividade do SL."""

    def setUp(self):
        self.trades = [
            {
                "symbol": "WINQ26", "direction": "BUY", "timeframe": "M5",
                "entry_time": "2026-06-20 10:00:00", "entry_price": 170000,
                "entry_sl": 169500,  # SL 500pts abaixo
                "exit_time": "2026-06-20 10:30:00", "exit_price": 170500,
                "exit_reason": "TP", "net_pnl": 100.0, "strategy": "BOLLINGER",
                "signal_detail": {},
            },
            {
                "symbol": "WINQ26", "direction": "BUY", "timeframe": "M5",
                "entry_time": "2026-06-21 10:00:00", "entry_price": 171000,
                "entry_sl": 170000,  # SL 1000pts abaixo (largo)
                "exit_time": "2026-06-21 10:15:00", "exit_price": 170200,
                "exit_reason": "SL_SERVIDOR", "net_pnl": -160.0, "strategy": "BOLLINGER",
                "signal_detail": {},
            },
        ]
        self.db_path = _create_test_db(self.trades)

    def tearDown(self):
        import os
        os.unlink(self.db_path)

    def test_collect_performance_has_sl_analysis(self):
        """collect_performance deve retornar chave sl_analysis."""
        with patch("agi_tuning_17h.DB_PATH", Path(self.db_path)):
            from agi_tuning_17h import collect_performance
            result = collect_performance(days=7)
        self.assertIn("sl_analysis", result)

    def test_sl_analysis_has_sl_hit_rate(self):
        """sl_analysis deve ter sl_hit_rate (% de trades fechados por SL)."""
        with patch("agi_tuning_17h.DB_PATH", Path(self.db_path)):
            from agi_tuning_17h import collect_performance
            result = collect_performance(days=7)
        sla = result.get("sl_analysis", {})
        self.assertIn("WIN", sla)
        self.assertIn("sl_hit_rate", sla["WIN"])

    def test_sl_analysis_has_avg_sl_distance(self):
        """sl_analysis deve ter avg_sl_pts (distância média do SL em pontos)."""
        with patch("agi_tuning_17h.DB_PATH", Path(self.db_path)):
            from agi_tuning_17h import collect_performance
            result = collect_performance(days=7)
        sla = result.get("sl_analysis", {})
        win = sla.get("WIN", {})
        self.assertIn("avg_sl_pts", win)

    def test_sl_hit_rate_correct(self):
        """1 de 2 trades fechou por SL → sl_hit_rate = 50%."""
        with patch("agi_tuning_17h.DB_PATH", Path(self.db_path)):
            from agi_tuning_17h import collect_performance
            result = collect_performance(days=7)
        sla = result["sl_analysis"]["WIN"]
        self.assertAlmostEqual(sla["sl_hit_rate"], 50.0, places=1)


class TestDirectionAnalysis(unittest.TestCase):
    """Testa que collect_performance extrai performance por direction."""

    def setUp(self):
        self.trades = [
            {
                "symbol": "WINQ26", "direction": "BUY", "timeframe": "M5",
                "entry_time": "2026-06-20 10:00:00", "entry_price": 170000,
                "exit_time": "2026-06-20 10:30:00", "exit_price": 170500,
                "net_pnl": 100.0, "strategy": "RSI_REVERSION",
                "signal_detail": {},
            },
            {
                "symbol": "WINQ26", "direction": "BUY", "timeframe": "M5",
                "entry_time": "2026-06-21 10:00:00", "entry_price": 171000,
                "exit_time": "2026-06-21 10:30:00", "exit_price": 170500,
                "net_pnl": -100.0, "strategy": "RSI_REVERSION",
                "signal_detail": {},
            },
            {
                "symbol": "WINQ26", "direction": "SELL", "timeframe": "M5",
                "entry_time": "2026-06-22 10:00:00", "entry_price": 172000,
                "exit_time": "2026-06-22 10:30:00", "exit_price": 171500,
                "net_pnl": 100.0, "strategy": "RSI_REVERSION",
                "signal_detail": {},
            },
        ]
        self.db_path = _create_test_db(self.trades)

    def tearDown(self):
        import os
        os.unlink(self.db_path)

    def test_collect_performance_has_direction_analysis(self):
        """collect_performance deve retornar chave direction_analysis."""
        with patch("agi_tuning_17h.DB_PATH", Path(self.db_path)):
            from agi_tuning_17h import collect_performance
            result = collect_performance(days=7)
        self.assertIn("direction_analysis", result)

    def test_direction_analysis_has_buy_and_sell(self):
        """direction_analysis deve ter BUY e SELL para cada símbolo."""
        with patch("agi_tuning_17h.DB_PATH", Path(self.db_path)):
            from agi_tuning_17h import collect_performance
            result = collect_performance(days=7)
        da = result.get("direction_analysis", {})
        self.assertIn("WIN", da)
        win = da["WIN"]
        self.assertIn("BUY", win)
        self.assertIn("SELL", win)

    def test_direction_analysis_correct_wr(self):
        """BUY: 1W/1L=50%WR, SELL: 1W/0L=100%WR."""
        with patch("agi_tuning_17h.DB_PATH", Path(self.db_path)):
            from agi_tuning_17h import collect_performance
            result = collect_performance(days=7)
        da = result["direction_analysis"]["WIN"]
        self.assertAlmostEqual(da["BUY"]["win_rate"], 50.0, places=1)
        self.assertAlmostEqual(da["SELL"]["win_rate"], 100.0, places=1)

    def test_direction_analysis_has_pnl(self):
        """direction_analysis deve ter total_pnl por direction."""
        with patch("agi_tuning_17h.DB_PATH", Path(self.db_path)):
            from agi_tuning_17h import collect_performance
            result = collect_performance(days=7)
        da = result["direction_analysis"]["WIN"]
        self.assertIn("total_pnl", da["BUY"])
        self.assertIn("total_pnl", da["SELL"])


if __name__ == "__main__":
    unittest.main()
