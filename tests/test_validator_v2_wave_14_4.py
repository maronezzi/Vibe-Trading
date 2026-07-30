"""Wave 14.4 — Forense do bug "Sem histórico do setup (0 trades em 30d)".

Reproduz o caso real BITN26 M5 SUPERTREND visto em 14/07/2026 14:08 BRT:
  - 2 BUY M5 SUPERTREND fechados (PnL -5 cada) ANTES do signal
  - 1 SELL M5 SUPERTREND aberto (exit_time NULL) NO momento do signal
  - validator com direction='SELL' retornava n_trades=0 (bug)
  - Wave 14.4: validator retorna n_trades=3 (todas direções, abertos+fechados)
"""
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))


class TestHistoricalSetupStatsDirection(unittest.TestCase):
    """Bug #1: direction não deveria filtrar n_trades do setup."""

    def setUp(self):
        """Cria DB temporário com o cenário real de 14/07/2026."""
        import tempfile
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._seed_bit_m5_supertrend(Path(self._tmp.name))

    def tearDown(self):
        import os
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def _seed_bit_m5_supertrend(self, db_path: Path) -> None:
        """Popula DB com o cenário real de 14/07/2026."""
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL, direction TEXT NOT NULL,
                timeframe TEXT DEFAULT 'M5', strategy TEXT DEFAULT 'SUPERTREND',
                entry_time TEXT NOT NULL, exit_time TEXT,
                exit_reason TEXT, net_pnl REAL DEFAULT 0
            );
        """)
        # 2 BUY fechados (PnL -5 cada) — os 2 trades que o validator ignorava
        for et, xt, pnl in [
            ("2026-07-14 11:36:00", "2026-07-14 11:38:43", -5.0),
            ("2026-07-14 11:40:16", "2026-07-14 11:50:43", -5.0),
        ]:
            conn.execute(
                "INSERT INTO trades(symbol,direction,timeframe,strategy,entry_time,"
                "exit_time,exit_reason,net_pnl) VALUES(?,?,?,?,?,?,?,?)",
                ("BITN26", "BUY", "M5", "SUPERTREND", et, xt, "BROKER_CLOSE", pnl),
            )
        # 1 SELL ABERTO (exit_time NULL) — o trade que estava sendo validado
        conn.execute(
            "INSERT INTO trades(symbol,direction,timeframe,strategy,entry_time,"
            "exit_time,exit_reason,net_pnl) VALUES(?,?,?,?,?,?,?,?)",
            ("BITN26", "SELL", "M5", "SUPERTREND",
             "2026-07-14 14:08:46", None, None, 0.0),
        )
        conn.commit()
        conn.close()

    def test_setup_with_sell_direction_returns_total_3(self):
        """Sinal SELL M5 SUPERTREND deve ver 3 trades (todas direções), não 0."""
        from core import vt_order_validator_v2
        tmp = Path(self._tmp.name)
        with patch.object(vt_order_validator_v2, "DB_PATH", tmp):
            stats = vt_order_validator_v2.historical_setup_stats(
                symbol="BITN26", tf="M5", strategy="SUPERTREND",
                direction="SELL", days=30,
            )

        # Wave 14.4 fix: n_trades = total do setup (não filtrado por direction)
        self.assertEqual(
            stats["n_trades"], 3,
            f"Esperava n_trades=3 (2 BUY + 1 SELL), recebi {stats['n_trades']}",
        )
        # Subset mesmo direction: 1 SELL
        self.assertEqual(stats["n_trades_with_direction"], 1)
        # n_trades_closed = 2 (BUY; o SELL está aberto)
        self.assertEqual(stats["n_trades_closed"], 2)
        # WR sobre fechados: 0 wins / 2 fechados = 0%
        self.assertEqual(stats["win_rate"], 0.0)

    def test_setup_with_buy_direction_returns_total_3(self):
        """Sinal BUY M5 SUPERTREND deve ver 3 trades (todas direções)."""
        from core import vt_order_validator_v2
        tmp = Path(self._tmp.name)
        with patch.object(vt_order_validator_v2, "DB_PATH", tmp):
            stats = vt_order_validator_v2.historical_setup_stats(
                symbol="BITN26", tf="M5", strategy="SUPERTREND",
                direction="BUY", days=30,
            )

        self.assertEqual(stats["n_trades"], 3)
        self.assertEqual(stats["n_trades_with_direction"], 2)  # 2 BUY

    def test_gate_historical_losing_not_triggered_with_2_closed(self):
        """Gate HISTORICAL_LOSING não dispara com 2 fechados (precisa >= 10)."""
        from core import vt_order_validator_v2 as v
        tmp = Path(self._tmp.name)
        with patch.object(v, "DB_PATH", tmp):
            stats = v.historical_setup_stats(
                symbol="BITN26", tf="M5", strategy="SUPERTREND",
                direction="SELL", days=30,
            )
            # Antes: gate usava n_trades (=3) e podia disparar.
            # Depois: gate usa n_trades_closed (=2), que é < HISTORICAL_MIN_TRADES=10.
            n_closed_for_gate = stats.get("n_trades_closed", stats["n_trades"])
            self.assertLess(
                n_closed_for_gate, v.HISTORICAL_MIN_TRADES,
                "Gate não deveria disparar com amostra < HISTORICAL_MIN_TRADES",
            )

    def test_empty_db_returns_zeros(self):
        """DB vazio deve retornar zeros sem crash."""
        import tempfile
        from core import vt_order_validator_v2 as v
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            empty_db = Path(f.name)
        try:
            with patch.object(v, "DB_PATH", empty_db):
                stats = v.historical_setup_stats(
                    symbol="BITN26", tf="M5", strategy="SUPERTREND",
                    direction="SELL", days=30,
                )
            self.assertEqual(stats["n_trades"], 0)
            self.assertEqual(stats["win_rate"], 0.0)
        finally:
            empty_db.unlink()


class TestPromptHonesty(unittest.TestCase):
    """Wave 14.4: prompt do LLM deve mostrar breakdown honesto."""

    def test_prompt_includes_breakdown_lines(self):
        from core import vt_order_validator_v2 as v
        # Não patchar DB — só verificar construção de prompt com h_stats fictício
        order_data = {
            "symbol": "BITN26", "direction": "SELL", "tf": "M5",
            "strategy": "SUPERTREND", "sl_pts": 50000, "atr": 677.0,
            "entry_price": 330620.0,
        }
        h_stats = {
            "n_trades": 3, "n_trades_with_direction": 1, "n_trades_closed": 2,
            "wins": 0, "losses": 2, "win_rate": 0.0,
            "avg_pnl": -3.33, "total_pnl": -10.0, "avg_duration_min": 5.0,
        }
        ctx = {
            "hora": "14:08", "trading_phase": "main",
            "daily_pnl": -100.0, "consecutive_losses": 0,
            "historical_setup": h_stats, "open_position": None,
        }
        validator = v.ValidatorV2.__new__(v.ValidatorV2)  # bypass __init__
        prompt = validator._build_llm_prompt(order_data, "BIT", ctx)

        # Verifica que as 3 linhas do breakdown estão presentes
        self.assertIn("Total do setup (todas direções)", prompt)
        self.assertIn("Mesma direção (SELL)", prompt)
        self.assertIn("WR (apenas fechados)", prompt)
        self.assertIn("3 trades", prompt)
        self.assertIn("0.0%", prompt)


if __name__ == "__main__":
    unittest.main()