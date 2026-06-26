"""
test_state_daily_pnl_sync_with_db.py
=======================================
TDD: garante que state.daily_pnl é SINCRONIZADO com o DB ao
iniciar, evitando off-by-one com trades pré-restart.

BUG IDENTIFICADO 2026-06-26:
  - Autotrader às 09:01 iniciou com v898
  - 09:25 fechou trade #3633 WDO_M5 +R$ 83.80
  - 09:45 RESTART (Wave 8.4.1 fix do bug _is_blocked_time)
  - 12:15 state.daily_pnl = R$ -38,75 (só conta trades pós-restart)
  - DB real: +R$ 45,05 (inclui o #3633 pré-restart)
  - Diferença: R$ 83,80 = exato valor do #3633

FIX (Wave 8.6): ao carregar state do disco, validar contra DB.
Se diferença > R$ X, recalcular state.daily_pnl a partir do DB.
"""
import os
import sqlite3
import sys
import unittest
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


class TestStateDailyPnlSync(unittest.TestCase):
    """state.daily_pnl deve bater com soma do DB."""

    def test_db_real_pnl_matches_calculation(self):
        """Sanity check: soma do DB é a verdade."""
        db = sqlite3.connect(os.path.join(PROJECT_ROOT, "vt_trades.db"))
        # Total PnL do dia
        row = db.execute("""
            SELECT COALESCE(SUM(net_pnl), 0), COUNT(*)
            FROM trades WHERE date(entry_time) = date('now')
            AND exit_time IS NOT NULL
        """).fetchone()
        total = row[0] or 0
        n = row[1]
        # Se state.daily_pnl != total, é off-by-one
        # Aqui só validamos o SQL
        self.assertIsNotNone(total)
        print(f"  DB real: R$ {total:+.2f} ({n} trades)")


class TestStateRestorationSyncsWithDB(unittest.TestCase):
    """Ao restaurar state, deve reconciliar com DB."""

    def test_state_pnl_matches_db_after_sync(self):
        """Função _sync_daily_pnl_with_db() deve atualizar state.daily_pnl."""
        from core.vt_autotrader import SessionState, _sync_daily_pnl_with_db

        state = SessionState()
        # Simular state legado (off-by-one com DB)
        state.daily_pnl = -38.75
        state.current_day = datetime.now().date()
        state.trade_count = 10

        # Sync com DB
        _sync_daily_pnl_with_db(state)

        # Após sync, state.daily_pnl deve bater com DB
        db = sqlite3.connect(os.path.join(PROJECT_ROOT, "vt_trades.db"))
        row = db.execute("""
            SELECT COALESCE(SUM(net_pnl), 0), COUNT(*)
            FROM trades WHERE date(entry_time) = date('now')
            AND exit_time IS NOT NULL
        """).fetchone()
        expected_pnl = row[0] or 0
        expected_n = row[1]

        self.assertAlmostEqual(
            state.daily_pnl, expected_pnl, places=2,
            msg=f"state.daily_pnl ({state.daily_pnl}) deve igualar DB ({expected_pnl})"
        )
        self.assertEqual(
            state.trade_count, expected_n,
            f"state.trade_count ({state.trade_count}) deve igualar DB ({expected_n})"
        )


if __name__ == "__main__":
    unittest.main()