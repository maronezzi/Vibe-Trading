"""
test_exclude_phantom_trade_2972.py
==================================
TDD para Bruno 09/07: trade #2972 com entry_ticket=2473969614 não existe
no MT5 demo (XPMT5-DEMO não persiste deals). Está poluindo o watchdog drift
report (R$ 403,80 falso) e o intraday copilot.

REGRA:
- NÃO deletar (skill recomenda EXCLUDED_FROM_STATS marker — preserva audit)
- Aplicar padrao: strategy = original + ' [EXCLUDED]', notes += ' | PHANTOM_TICKET'
- Estender queries de PnL diario para filtrar WHERE strategy NOT LIKE '%[EXCLUDED]%'

RED:
- get_db_daily_pnl() deve retornar R$ 0,00 quando so ha trade #2972 fantasma
  + outras GHOST (que ja sao excluidas).
- check_intraday_stats() pnl_realized deve ser 0 quando #2972 marcado como PHANTOM.
- Drift mt5=0, db=0, alerta=False (criterio de aceitacao).
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class TestExcludePhantomTrade2972(unittest.TestCase):
    """Bug 09/07: trade #2972 SELL WINQ26 SL_SERVIDOR R$+403,80 nao existe
    no MT5 demo, polui watchdog drift + intraday PnL."""

    def setUp(self):
        # DB temporario com trade #2972 + GHOSTs
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        import os
        os.close(fd)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        # Schema minimo do vt_trades.db
        self.conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                entry_ticket TEXT,
                exit_ticket TEXT,
                symbol TEXT,
                direction TEXT,
                volume REAL,
                entry_time TEXT,
                exit_time TEXT,
                exit_reason TEXT,
                exit_price REAL,
                gross_pnl REAL,
                fees REAL,
                swap REAL,
                net_pnl REAL,
                strategy TEXT,
                notes TEXT
            )
        """)
        # Trade fantasma #2972 (R$ +403,80, MT5 nao tem)
        self.conn.execute("""
            INSERT INTO trades (id, entry_ticket, exit_ticket, symbol, direction,
                              volume, entry_time, exit_time, exit_reason,
                              exit_price, gross_pnl, fees, swap, net_pnl,
                              strategy, notes)
            VALUES (2972, '2473969614', 'server', 'WINQ26', 'SELL', 1.0,
                    '2026-07-09 10:15:07', '2026-07-09 11:00:53', 'SL_SERVIDOR',
                    173280.0, 405.0, 1.2, 0.0, 403.8,
                    'HTF_BIAS_LTF_ENTRY',
                    'FECHADO PELO SERVIDOR | PnL real: R$405.00 (broker-truth via MT5 history)')
        """)
        # GHOST trades (ja filtrados pela query atual, mas marcam presenca)
        for tid in (2969, 2970, 2971, 2973) :
            self.conn.execute(f"""
                INSERT INTO trades (id, entry_ticket, exit_ticket, symbol, direction,
                                  volume, entry_time, exit_time, exit_reason,
                                  exit_price, net_pnl, strategy, notes)
                VALUES ({tid}, '2473{tid}', 'ghost_reconcile', 'WINQ26', 'SELL',
                        1.0, '2026-07-09 09:31:20', '2026-07-09 10:01:02',
                        'GHOST', 173635.0, 0.0, 'RSI_REVERSION',
                        'GHOST_RECONCILED | state tinha, MT5 nao tem mais')
            """)
        self.conn.commit()

    def tearDown(self):
        import os
        self.conn.close()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    # -------------------------- RED: filtros SQL --------------------------

    def test_query_excludes_excluded_from_stats_marker(self):
        """Query do get_db_daily_pnl() deve excluir [EXCLUDED] alem de GHOST."""
        # Aplica a marcacao
        from scripts.exclude_phantom_today import mark_trade_excluded
        mark_trade_excluded(self.db_path, trade_id=2972, reason="PHANTOM_TICKET")
        # Aplica query do watchdog+intraday
        row = self.conn.execute("""
            SELECT COALESCE(SUM(net_pnl), 0.0) AS total
            FROM trades
            WHERE date(entry_time) = '2026-07-09'
              AND exit_time IS NOT NULL
              AND exit_reason != 'stale_close'
              AND exit_reason != 'GHOST'
              AND strategy NOT LIKE '%[EXCLUDED]%'
        """).fetchone()
        self.assertEqual(float(row["total"]), 0.0,
                         f"DB PnL deveria ser 0 com #2972 marcado [EXCLUDED], "
                         f"foi {row['total']}")

    def test_before_marker_db_pnl_was_fake_403(self):
        """RED confirmando o bug: SEM marker, DB soma R$ +403,80 (incorrect)."""
        row = self.conn.execute("""
            SELECT COALESCE(SUM(net_pnl), 0.0) AS total
            FROM trades
            WHERE date(entry_time) = '2026-07-09'
              AND exit_time IS NOT NULL
              AND exit_reason != 'stale_close'
              AND exit_reason != 'GHOST'
        """).fetchone()
        # Antes do fix, isto é 403.8 (o bug). Proposital - confirma reproducao.
        self.assertEqual(float(row["total"]), 403.8,
                         "Esperando bug reproduzido: 403.80 fantasma somado")

    def test_mark_trade_excluded_preserves_audit(self):
        """Marcacao nao deleta: trade continua no DB com markers."""
        from scripts.exclude_phantom_today import mark_trade_excluded
        mark_trade_excluded(self.db_path, trade_id=2972, reason="PHANTOM_TICKET")
        row = self.conn.execute(
            "SELECT strategy, notes, net_pnl FROM trades WHERE id=2972"
        ).fetchone()
        self.assertEqual(float(row["net_pnl"]), 403.8,
                         "net_pnl preservado para auditoria")
        self.assertIn("[EXCLUDED]", row["strategy"],
                      "strategy deve ter marcador [EXCLUDED]")
        self.assertIn("PHANTOM_TICKET", row["notes"],
                      "notes deve registrar motivo")

    def test_drift_alert_resolves_to_zero(self):
        """Apos aplicar marker, mt5(db) devem bater (0 vs 0 = no alert)."""
        from scripts.exclude_phantom_today import mark_trade_excluded
        mark_trade_excluded(self.db_path, trade_id=2972, reason="PHANTOM_TICKET")
        # Recarrega conn (mark_trade_excluded pode ter commitado)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        row = self.conn.execute("""
            SELECT COALESCE(SUM(net_pnl), 0.0) AS total
            FROM trades
            WHERE date(entry_time) = '2026-07-09'
              AND exit_time IS NOT NULL
              AND exit_reason != 'GHOST'
              AND strategy NOT LIKE '%[EXCLUDED]%'
        """).fetchone()
        db = float(row["total"])
        mt5 = 0.0  # XPMT5-DEMO sem deals
        drift = abs(mt5 - db)
        self.assertEqual(drift, 0.0,
                         f"Drift esperado 0 apos excluir #2972, foi {drift}")


if __name__ == "__main__":
    unittest.main()
