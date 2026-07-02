"""
test_intraday_balance_delta_fallback.py
========================================
Wave 1C.2 (Bruno 02/07 11:14): quando MT5 history esta vazio (broker
demo nao persiste deals), check_intraday_stats() deve usar delta do
saldo MT5 como PnL realizado broker-truth.

Cenários:
1. MT5 history OK com deals — usa MT5, nao fallback
2. MT5 history vazio + saldo MT5 mudou — usa delta saldo (FALLBACK-BALANCE)
3. MT5 history vazio + saldo MT5 igual — pnl_realized=0 (sem mudanca)
4. Trades GHOST no DB — EXCLUIDOS do PnL realizado, mas conta_aviso
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "monitoring"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "core"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "mt5"))


class TestIntradayBalanceDeltaFallback(unittest.TestCase):
    """Wave 1C.2: FALLBACK-BALANCE para MT5 demo sem history."""

    def setUp(self):
        # DB temp
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, timeframe TEXT,
                entry_time TEXT, exit_time TEXT,
                exit_reason TEXT DEFAULT 'TRAILING',
                net_pnl REAL, strategy TEXT
            )
        """)
        today = "2026-07-02"
        # 1 trade GHOST (PnL=0 — bug autotrader)
        conn.execute(
            "INSERT INTO trades (symbol, timeframe, entry_time, exit_time, exit_reason, net_pnl) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("WINQ26", "H1", f"{today} 09:21:00", f"{today} 09:30:00", "GHOST", 0.0),
        )
        # 1 trade com PnL real (não-GHOST)
        conn.execute(
            "INSERT INTO trades (symbol, timeframe, entry_time, exit_time, exit_reason, net_pnl) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("WSPU26", "M30", f"{today} 09:50:00", f"{today} 09:55:00", "TRAILING", -25.0),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_ghost_trades_excluded_from_pnl_realized(self):
        """GHOST trades (PnL=0 bug) NAO devem contar como loss no report."""
        from vt_copilot import check_intraday_stats
        with patch("vt_copilot.DB_PATH", self.db_path), \
             patch("vt_copilot.get_daily_pnl_truth", return_value={"ok": False, "deals_total": 0, "pnl_net": 0, "deals": [], "error": "no_deals"}), \
             patch("mt5_orchestrator.status", return_value={"account": {"balance": 1002230.57, "equity": 1002230.57}, "positions": []}):
            stats = check_intraday_stats()
        # WSPU26 -25 conta, GHOST 0 nao conta
        self.assertEqual(
            stats["ops"], 1,
            f"Esperado 1 trade (WSPU26 -25), GHOST deve ser excluido. achou ops={stats['ops']}"
        )
        self.assertEqual(
            stats["losses"], 1,
            f"Esperado 1 loss (WSPU26 -25), achou losses={stats['losses']}"
        )
        self.assertEqual(
            stats["wins"], 0,
            f"Esperado 0 wins, achou wins={stats['wins']}"
        )

    def test_balance_delta_used_when_mt5_history_empty(self):
        """Quando MT5 history vazio, usar delta saldo MT5 como PnL broker-truth."""
        from vt_copilot import check_intraday_stats
        # Saldo MT5 caiu 75.50 desde abertura — perda real
        with patch("vt_copilot.DB_PATH", self.db_path), \
             patch("vt_copilot.get_daily_pnl_truth", return_value={"ok": False, "deals_total": 0, "pnl_net": 0, "deals": [], "error": "demo_no_history"}), \
             patch("mt5_orchestrator.status", return_value={"account": {"balance": 1002155.07, "equity": 1002155.07}, "positions": []}):
            stats = check_intraday_stats()
        # PnL broker-truth = 1.002.155,07 - 1.002.230,57 = -75,50
        self.assertAlmostEqual(
            stats["pnl_realized"], -75.50, places=2,
            msg=f"FALLBACK-BALANCE deveria usar delta do saldo. Esperado -75.50, achou {stats['pnl_realized']}"
        )
        # Fonte deve ser DB_FALLBACK (MT5 history vazio)
        self.assertEqual(
            stats["source"], "DB_FALLBACK",
            "Source deveria ser DB_FALLBACK quando MT5 history vazio"
        )

    def test_balance_delta_zero_when_no_change(self):
        """Se saldo MT5 nao mudou, pnl_realized=0 (sem atividade)."""
        from vt_copilot import check_intraday_stats
        with patch("vt_copilot.DB_PATH", self.db_path), \
             patch("vt_copilot.get_daily_pnl_truth", return_value={"ok": False, "deals_total": 0, "pnl_net": 0, "deals": [], "error": "no_deals"}), \
             patch("mt5_orchestrator.status", return_value={"account": {"balance": 1002230.57, "equity": 1002230.57}, "positions": []}):
            stats = check_intraday_stats()
        # Saldo inalterado = 0 PnL
        self.assertEqual(
            stats["pnl_realized"], 0.0,
            f"pnl_realized deveria ser 0 quando saldo inalterado, achou {stats['pnl_realized']}"
        )

    def test_mt5_history_with_deals_takes_priority(self):
        """Se MT5 history tem deals, usar MT5 (nao FALLBACK-BALANCE)."""
        from vt_copilot import check_intraday_stats
        with patch("vt_copilot.DB_PATH", self.db_path), \
             patch("vt_copilot.get_daily_pnl_truth", return_value={
                 "ok": True, "deals_total": 1, "pnl_net": -15.0,
                 "deals": [{"ticket": 1, "time": 1782896700, "profit": -15.0, "commission": 0, "swap": 0}],
                 "error": None
             }), \
             patch("mt5_orchestrator.status", return_value={"account": {"balance": 1002230.57, "equity": 1002230.57}, "positions": []}):
            stats = check_intraday_stats()
        # MT5 history wins: pnl = -15 (1 deal)
        self.assertEqual(
            stats["source"], "MT5_HISTORY",
            "Source deveria ser MT5_HISTORY quando deals existem"
        )
        self.assertEqual(
            stats["pnl_realized"], -15.0,
            f"pnl_realized deveria ser -15 (MT5 truth), achou {stats['pnl_realized']}"
        )


class TestIntradayReportShowsBalanceDelta(unittest.TestCase):
    """Wave 1C.2: generate_report() deve mostrar PnL broker-truth mesmo com 0 trades."""

    def test_report_shows_balance_delta_when_ops_zero(self):
        from vt_copilot import generate_report
        # DB com apenas GHOST trades (PnL=0) — ops=0 mas saldo MT5 mudou
        import tempfile, sqlite3
        fd, db_path = tempfile.mkstemp(suffix=".db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, timeframe TEXT,
                entry_time TEXT, exit_time TEXT,
                exit_reason TEXT DEFAULT 'TRAILING',
                net_pnl REAL, strategy TEXT
            )
        """)
        conn.execute(
            "INSERT INTO trades (symbol, timeframe, entry_time, exit_time, exit_reason, net_pnl) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("WINQ26", "H1", "2026-07-02 09:21:00", "2026-07-02 09:30:00", "GHOST", 0.0),
        )
        conn.commit()
        conn.close()
        try:
            with patch("vt_copilot.DB_PATH", db_path), \
                 patch("vt_copilot.get_daily_pnl_truth", return_value={"ok": False, "deals_total": 0, "pnl_net": 0, "deals": [], "error": "no_deals"}), \
                 patch("mt5_orchestrator.status", return_value={"account": {"balance": 1002155.07, "equity": 1002155.07}, "positions": []}), \
                 patch("vt_copilot.check_autotrader_health", return_value={"running": True, "pid": 123}), \
                 patch("vt_copilot.get_truth_from_mt5", return_value={"ok": True, "balance": 1002155.07, "equity": 1002155.07}):
                report = generate_report()
            # Report DEVE mencionar PnL broker-truth mesmo com ops=0
            self.assertIn(
                "broker-truth", report,
                f"Report deveria mencionar 'broker-truth' para PnL via saldo. Report:\n{report}"
            )
            self.assertIn(
                "-75.50", report,
                f"Report deveria mostrar -75.50 (delta saldo). Report:\n{report}"
            )
            # E tambem avisar sobre GHOST
            self.assertIn(
                "GHOST", report,
                f"Report deveria avisar sobre trades GHOST. Report:\n{report}"
            )
        finally:
            os.unlink(db_path)


if __name__ == "__main__":
    unittest.main()