"""
test_orchestrator_close_updates_db.py
======================================

TDD — Garante que ``mt5_orchestrator.close()`` PERSISTE o PnL em vt_trades.db.

PROBLEMA IDENTIFICADO 2026-07-01:
    O ``close(symbol)`` do orchestrator retornava o JSON do executor MT5
    (com profit) mas NUNCA chamava ``core.vt_trade_log.log_exit()`` nem fazia
    UPDATE no DB. Resultado: MT5 fechava a posição mas o trade ficava com
    ``gross_pnl=0`` e ``net_pnl=0`` — trades manuais (fechados pelo usuário
    via GUI MT5 ou por chamadas externas) acumulavam PnL zero no DB mesmo
    após realizarem lucro/prejuízo real.

FIX (Wave 11, 2026-07-01):
    ``mt5_orchestrator.close()`` agora invoca ``_persist_close_to_db()``
    após o MT5 retornar ``status='ok'`` + ``closed>=1``. A função:
      - Itera ``details[]``.
      - Para cada detail com retcode ok:
          * Procura trade por ``entry_ticket = detail.ticket``.
          * UPDATE se exit_time IS NULL (trade legit).
          * UPDATE re-reconciliável se exit_time já está set.
          * INSERT orphan se o ticket não está no DB (server-close).
      - Nunca crasha o close() por erro de DB.

O QUE ESTE TESTE PROTEGE:
    1. ``close()`` existe e é chamável.
    2. Após close com profit=100 → trade existente tem gross_pnl=100,
       net_pnl=100, exit_time IS NOT NULL.
    3. Orphan (ticket NÃO está no DB) → INSERT novo registro com PnL.
    4. Detail com erro (retcode != DONE) é pulado sem afetar os outros.
    5. Erro de DB NÃO crasha close — o resultado do MT5 ainda é retornado.
    6. Status != 'ok' → close() NÃO tenta persistir (skip DB side-effect).
"""
import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
MT5_DIR = Path(PROJECT_ROOT) / "mt5"
TESTS_DIR = Path(PROJECT_ROOT) / "tests"

# Garante import do orchestrator (mt5/mt5_orchestrator.py)
for p in (PROJECT_ROOT, str(MT5_DIR), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _init_trades_schema(db_path: Path) -> None:
    """Schema mínimo de trades (espelha o que o orchestrator cria sozinho)."""
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_ticket TEXT,
            exit_ticket TEXT,
            magic_number INTEGER DEFAULT 555501,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            volume REAL NOT NULL,
            timeframe TEXT DEFAULT 'M5',
            entry_time TEXT NOT NULL,
            entry_price REAL NOT NULL,
            entry_sl REAL,
            exit_time TEXT,
            exit_price REAL,
            exit_reason TEXT,
            exit_sl_price REAL,
            gross_pnl REAL DEFAULT 0,
            fees REAL DEFAULT 0,
            swap REAL DEFAULT 0,
            net_pnl REAL DEFAULT 0,
            is_day_trade INTEGER DEFAULT 1,
            asset_type TEXT DEFAULT 'FUTURE',
            multiplier REAL DEFAULT 0.20,
            strategy TEXT DEFAULT 'VWAP',
            signal_detail TEXT,
            raw_entry_json TEXT,
            raw_exit_json TEXT,
            notes TEXT,
            close_source TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT DEFAULT (datetime('now', 'localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_trades_entry_ticket ON trades(entry_ticket);
    """)
    conn.commit()
    conn.close()


def _seed_trade(db_path: Path, entry_ticket: str, symbol: str = "WSPU26",
                direction: str = "BUY", volume: float = 1.0,
                entry_price: float = 50.0) -> int:
    """Insere trade 'aberto' (sem exit_time) no DB. Retorna trade_id."""
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    cur = conn.execute(
        """
        INSERT INTO trades (
            entry_ticket, symbol, direction, volume,
            entry_time, entry_price, entry_sl
        ) VALUES (?, ?, ?, ?, datetime('now', 'localtime'), ?, NULL)
        """,
        (entry_ticket, symbol, direction, volume, entry_price),
    )
    trade_id = cur.lastrowid
    conn.commit()
    conn.close()
    return trade_id


def _make_close_result(symbol: str, details: list, closed: int = None) -> dict:
    """Constrói o JSON que o mt5_executor retorna no close."""
    if closed is None:
        closed = sum(1 for d in details if "error" not in d)
    return {
        "status": "ok",
        "closed": closed,
        "total": len(details),
        "details": details,
    }


# ──────────────────────────────────────────────────────────────────────────
# Mixin: tmp DB isolada + patch do TRADES_DB do orchestrator
# ──────────────────────────────────────────────────────────────────────────


class _TmpDBMixin(unittest.TestCase):
    """Redireciona mt5_orchestrator.TRADES_DB para tmp_path.

    Herda de unittest.TestCase para que as classes que usam o mixin
    herdem corretamente os métodos assert*. (Ver test_reconcile_positions.py
    para o mesmo padrão.)
    """

    def setUp(self):
        import tempfile
        from mt5 import mt5_orchestrator

        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_db = Path(self._tmpdir.name) / "vt_trades.db"
        _init_trades_schema(self.tmp_db)

        # Patch TRADES_DB no módulo do orchestrator
        self._trades_db_patcher = patch.object(
            mt5_orchestrator, "TRADES_DB", self.tmp_db
        )
        self._trades_db_patcher.start()

    def tearDown(self):
        try:
            self._trades_db_patcher.stop()
        except Exception:
            pass
        try:
            self._tmpdir.cleanup()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────
# Testes
# ──────────────────────────────────────────────────────────────────────────


class TestCloseFunctionExists(unittest.TestCase):
    """Sanidade: função close() existe e é chamável."""

    def test_close_defined(self):
        from mt5 import mt5_orchestrator
        self.assertTrue(
            hasattr(mt5_orchestrator, "close"),
            "mt5_orchestrator deve expor close()",
        )
        self.assertTrue(callable(mt5_orchestrator.close))

    def test_persist_helper_defined(self):
        from mt5 import mt5_orchestrator
        self.assertTrue(
            hasattr(mt5_orchestrator, "_persist_close_to_db"),
            "mt5_orchestrator deve expor _persist_close_to_db()",
        )


class TestCloseUpdatesExistingTrade(_TmpDBMixin):
    """close() com trade existente no DB → UPDATE com PnL."""

    def test_close_with_profit_updates_db(self):
        from mt5 import mt5_orchestrator

        # Seed: trade aberto (sem exit_time), ticket=2467793364
        entry_ticket = "2467793364"
        trade_id = _seed_trade(self.tmp_db, entry_ticket, "WSPU26", "BUY", 1.0, 50.0)

        # MT5 retorna close ok com profit=100
        close_result = _make_close_result("WSPU26", [{
            "ticket": int(entry_ticket),
            "symbol": "WSPU26",
            "type": "BUY",
            "volume": 1.0,
            "entry_price": 50.0,
            "close_price": 60.0,
            "profit": 100.0,
            "swap": 0.0,
            "magic": 555501,
        }])

        with patch.object(mt5_orchestrator, "_run_wine", return_value=close_result):
            result = mt5_orchestrator.close("WSPU26")

        # Verifica retorno do MT5 preservado
        self.assertEqual(result.get("status"), "ok")
        self.assertEqual(result.get("closed"), 1)
        self.assertIn("db_persist", result, "close() deve adicionar stats de DB")
        self.assertEqual(result["db_persist"]["updated"], 1)

        # Verifica DB: trade agora tem PnL
        conn = sqlite3.connect(str(self.tmp_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT exit_time, exit_price, gross_pnl, net_pnl, "
            "exit_reason, close_source FROM trades WHERE id = ?",
            (trade_id,),
        ).fetchone()
        conn.close()

        self.assertIsNotNone(row, "Trade deveria existir no DB")
        self.assertIsNotNone(
            row["exit_time"],
            f"exit_time deveria estar preenchido, veio NULL. Row: {dict(row)}",
        )
        self.assertEqual(
            row["gross_pnl"], 100.0,
            f"gross_pnl deveria ser 100.0, veio {row['gross_pnl']}",
        )
        self.assertEqual(
            row["net_pnl"], 100.0,
            f"net_pnl deveria ser 100.0 (sem fees conhecidas), veio {row['net_pnl']}",
        )
        self.assertEqual(
            row["exit_reason"], "MANUAL_CLOSE_OR_ORPHAN",
            f"exit_reason errado: {row['exit_reason']}",
        )
        self.assertEqual(
            row["close_source"], "mt5_orchestrator_close",
            f"close_source errado: {row['close_source']}",
        )
        self.assertEqual(row["exit_price"], 60.0)

    def test_close_with_loss_records_negative_pnl(self):
        from mt5 import mt5_orchestrator

        entry_ticket = "2467800100"
        trade_id = _seed_trade(self.tmp_db, entry_ticket, "WINQ26", "SELL", 1.0, 120.0)

        close_result = _make_close_result("WINQ26", [{
            "ticket": int(entry_ticket),
            "symbol": "WINQ26",
            "type": "SELL",
            "volume": 1.0,
            "entry_price": 120.0,
            "close_price": 130.0,  # SELL fechou acima → loss
            "profit": -250.0,
            "swap": 0.0,
            "magic": 555501,
        }])

        with patch.object(mt5_orchestrator, "_run_wine", return_value=close_result):
            mt5_orchestrator.close("WINQ26")

        conn = sqlite3.connect(str(self.tmp_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT gross_pnl, net_pnl, exit_time FROM trades WHERE id = ?",
            (trade_id,),
        ).fetchone()
        conn.close()

        self.assertEqual(row["gross_pnl"], -250.0)
        self.assertEqual(row["net_pnl"], -250.0)
        self.assertIsNotNone(row["exit_time"])

    def test_close_with_swap(self):
        from mt5 import mt5_orchestrator

        entry_ticket = "2467811111"
        trade_id = _seed_trade(self.tmp_db, entry_ticket, "WDON26", "BUY", 1.0, 5000.0)

        close_result = _make_close_result("WDON26", [{
            "ticket": int(entry_ticket),
            "symbol": "WDON26",
            "type": "BUY",
            "volume": 1.0,
            "entry_price": 5000.0,
            "close_price": 5010.0,
            "profit": 100.0,
            "swap": -5.0,  # swap cobrado
            "magic": 555501,
        }])

        with patch.object(mt5_orchestrator, "_run_wine", return_value=close_result):
            mt5_orchestrator.close("WDON26")

        conn = sqlite3.connect(str(self.tmp_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT gross_pnl, swap, net_pnl FROM trades WHERE id = ?",
            (trade_id,),
        ).fetchone()
        conn.close()

        self.assertEqual(row["gross_pnl"], 100.0)
        self.assertEqual(row["swap"], -5.0)
        # net_pnl = gross_pnl + swap (não descontamos fees porque não temos)
        self.assertEqual(row["net_pnl"], 100.0)


class TestCloseInsertsOrphan(_TmpDBMixin):
    """close() com ticket que NÃO está no DB → INSERT orphan."""

    def test_close_orphan_inserts_trade(self):
        from mt5 import mt5_orchestrator

        # Não seed nenhum trade — ticket 'novo' deve virar INSERT orphan
        close_result = _make_close_result("WSPU26", [{
            "ticket": 2467899999,
            "symbol": "WSPU26",
            "type": "BUY",
            "volume": 2.0,
            "entry_price": 55.0,
            "close_price": 65.0,
            "profit": 200.0,
            "swap": 0.0,
            "magic": 555501,
        }])

        with patch.object(mt5_orchestrator, "_run_wine", return_value=close_result):
            result = mt5_orchestrator.close("WSPU26")

        self.assertEqual(result["db_persist"]["orphans_inserted"], 1)
        self.assertEqual(result["db_persist"]["updated"], 0)

        conn = sqlite3.connect(str(self.tmp_db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM trades WHERE entry_ticket = ?", ("2467899999",),
        ).fetchall()
        conn.close()

        self.assertEqual(len(rows), 1, "Deveria haver 1 orphan inserido")
        row = rows[0]
        self.assertEqual(row["symbol"], "WSPU26")
        self.assertEqual(row["direction"], "BUY")
        self.assertEqual(row["volume"], 2.0)
        self.assertEqual(row["entry_price"], 55.0)
        self.assertEqual(row["exit_price"], 65.0)
        self.assertEqual(row["gross_pnl"], 200.0)
        self.assertEqual(row["net_pnl"], 200.0)
        self.assertEqual(row["exit_reason"], "MANUAL_CLOSE_OR_ORPHAN")
        self.assertEqual(row["close_source"], "mt5_orchestrator_close")
        self.assertIsNotNone(row["exit_time"])
        self.assertIn("orchestrator_close", row["notes"])


class TestCloseMultiDetails(_TmpDBMixin):
    """close() com múltiplos details — UPDATE + INSERT misturados."""

    def test_mixed_update_and_orphan(self):
        from mt5 import mt5_orchestrator

        # Seed: 1 trade existente
        existing_ticket = "2467700001"
        existing_id = _seed_trade(self.tmp_db, existing_ticket, "WSPU26", "BUY", 1.0, 50.0)

        # Orphan: ticket novo
        orphan_ticket = "2467700002"

        close_result = _make_close_result("WSPU26", [
            {
                "ticket": int(existing_ticket),
                "symbol": "WSPU26",
                "type": "BUY",
                "volume": 1.0,
                "entry_price": 50.0,
                "close_price": 55.0,
                "profit": 50.0,
                "swap": 0.0,
                "magic": 555501,
            },
            {
                "ticket": int(orphan_ticket),
                "symbol": "WSPU26",
                "type": "SELL",
                "volume": 1.0,
                "entry_price": 55.0,
                "close_price": 50.0,
                "profit": 5.0,
                "swap": 0.0,
                "magic": 555501,
            },
        ])

        with patch.object(mt5_orchestrator, "_run_wine", return_value=close_result):
            result = mt5_orchestrator.close("WSPU26")

        self.assertEqual(result["db_persist"]["updated"], 1)
        self.assertEqual(result["db_persist"]["orphans_inserted"], 1)

        conn = sqlite3.connect(str(self.tmp_db))
        conn.row_factory = sqlite3.Row

        # Trade existente: deve ter PnL
        existing_row = conn.execute(
            "SELECT gross_pnl, exit_time FROM trades WHERE id = ?", (existing_id,),
        ).fetchone()
        self.assertEqual(existing_row["gross_pnl"], 50.0)
        self.assertIsNotNone(existing_row["exit_time"])

        # Orphan: deve existir
        orphan_count = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE entry_ticket = ?",
            (orphan_ticket,),
        ).fetchone()[0]
        self.assertEqual(orphan_count, 1, "Orphan deveria ter sido inserido")

        conn.close()


class TestCloseSkipsErrorDetails(_TmpDBMixin):
    """Detail com 'error' (retcode != DONE) é pulado."""

    def test_error_detail_is_skipped(self):
        from mt5 import mt5_orchestrator

        valid_ticket = "2467800001"
        valid_id = _seed_trade(self.tmp_db, valid_ticket, "WSPU26", "BUY", 1.0, 50.0)

        close_result = _make_close_result("WSPU26", [
            {
                "ticket": int(valid_ticket),
                "symbol": "WSPU26",
                "type": "BUY",
                "volume": 1.0,
                "entry_price": 50.0,
                "close_price": 60.0,
                "profit": 100.0,
                "swap": 0.0,
                "magic": 555501,
            },
            {
                "ticket": 2467800002,
                "symbol": "WSPU26",
                "error": "Requote",  # MT5 falhou nesse
            },
        ])

        with patch.object(mt5_orchestrator, "_run_wine", return_value=close_result):
            result = mt5_orchestrator.close("WSPU26")

        # Só 1 detail válido foi processado
        self.assertEqual(result["db_persist"]["updated"], 1)
        self.assertEqual(result["db_persist"]["orphans_inserted"], 0)
        # O segundo (com error) foi pulado e NÃO virou orphan
        self.assertEqual(result["db_persist"]["skipped"], 1)

        # DB: só o trade válido tem exit
        conn = sqlite3.connect(str(self.tmp_db))
        conn.row_factory = sqlite3.Row
        valid_row = conn.execute(
            "SELECT gross_pnl, exit_time FROM trades WHERE id = ?", (valid_id,),
        ).fetchone()
        orphan_count = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE entry_ticket = ?", ("2467800002",),
        ).fetchone()[0]
        conn.close()

        self.assertEqual(valid_row["gross_pnl"], 100.0)
        self.assertIsNotNone(valid_row["exit_time"])
        self.assertEqual(orphan_count, 0, "Detail com erro não deve virar orphan")


class TestCloseSkipsOnNonOkStatus(_TmpDBMixin):
    """close() com status != 'ok' NÃO tenta persistir no DB."""

    def test_error_status_does_not_persist(self):
        from mt5 import mt5_orchestrator

        entry_ticket = "2467900001"
        _seed_trade(self.tmp_db, entry_ticket, "WSPU26", "BUY", 1.0, 50.0)

        # MT5 retorna erro (sem 'status=ok')
        error_result = {"error": "timeout", "raw_stdout": ""}

        with patch.object(mt5_orchestrator, "_run_wine", return_value=error_result):
            result = mt5_orchestrator.close("WSPU26")

        # Sem db_persist (porque status != 'ok')
        self.assertNotIn("db_persist", result)

        # DB: trade continua sem exit
        conn = sqlite3.connect(str(self.tmp_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT gross_pnl, exit_time FROM trades WHERE entry_ticket = ?",
            (entry_ticket,),
        ).fetchone()
        conn.close()

        self.assertIsNone(row["exit_time"], "exit_time NÃO deveria estar setado")
        self.assertEqual(row["gross_pnl"], 0)

    def test_empty_close_does_not_persist(self):
        from mt5 import mt5_orchestrator

        # MT5 retorna status=ok mas sem details (nada pra fechar)
        result_no_close = {
            "status": "ok",
            "closed": 0,
            "total": 0,
            "details": [],
        }

        with patch.object(mt5_orchestrator, "_run_wine", return_value=result_no_close):
            result = mt5_orchestrator.close("WSPU26")

        # Não tenta persistir se closed=0
        self.assertNotIn("db_persist", result)


class TestCloseSafeOnDBError(_TmpDBMixin):
    """Erro de DB NÃO crasha close — retorna JSON mesmo assim."""

    def test_db_error_returns_mt5_result_anyway(self):
        from mt5 import mt5_orchestrator

        entry_ticket = "2467100001"
        _seed_trade(self.tmp_db, entry_ticket, "WSPU26", "BUY", 1.0, 50.0)

        close_result = _make_close_result("WSPU26", [{
            "ticket": int(entry_ticket),
            "symbol": "WSPU26",
            "type": "BUY",
            "volume": 1.0,
            "entry_price": 50.0,
            "close_price": 60.0,
            "profit": 100.0,
            "swap": 0.0,
            "magic": 555501,
        }])

        # Força erro de DB
        with patch.object(mt5_orchestrator, "_run_wine", return_value=close_result), \
             patch.object(mt5_orchestrator, "_persist_close_to_db",
                          side_effect=Exception("disk full")):
            try:
                result = mt5_orchestrator.close("WSPU26")
            except Exception as e:
                self.fail(
                    f"close() não deveria propagar DB errors, mas lançou: {e}"
                )

        # Mesmo com DB falhando, o resultado do MT5 é preservado
        self.assertEqual(result.get("status"), "ok")
        self.assertEqual(result.get("closed"), 1)
        self.assertIn("db_persist_error", result)

    def test_db_unavailable_does_not_crash(self):
        from mt5 import mt5_orchestrator

        close_result = _make_close_result("WSPU26", [{
            "ticket": 2467111111,
            "symbol": "WSPU26",
            "type": "BUY",
            "volume": 1.0,
            "entry_price": 50.0,
            "close_price": 60.0,
            "profit": 100.0,
            "swap": 0.0,
            "magic": 555501,
        }])

        # DB não conectável
        with patch.object(mt5_orchestrator, "_run_wine", return_value=close_result), \
             patch("sqlite3.connect", side_effect=Exception("DB locked")):
            try:
                result = mt5_orchestrator.close("WSPU26")
            except Exception as e:
                self.fail(
                    f"close() não deveria propagar sqlite3.connect error, "
                    f"mas lançou: {e}"
                )

        self.assertEqual(result.get("status"), "ok")


class TestClosePreservesExistingExitTime(_TmpDBMixin):
    """Trade que JÁ tem exit_time (já fechado) → re-reconcilia, não sobrescreve exit_time."""

    def test_replay_safe_for_already_closed_trade(self):
        from mt5 import mt5_orchestrator

        # Seed: trade JÁ fechado
        conn = sqlite3.connect(str(self.tmp_db))
        conn.execute(
            """
            INSERT INTO trades (
                entry_ticket, symbol, direction, volume,
                entry_time, entry_price, exit_time, exit_price,
                gross_pnl, net_pnl, exit_reason, close_source
            ) VALUES (?, 'WSPU26', 'BUY', 1.0,
                      '2026-07-01 11:00:00', 50.0,
                      '2026-07-01 11:30:00', 55.0,
                      50.0, 50.0, 'TRAILING', 'AUTOTRADER')
            """,
            ("2467999999",),
        )
        conn.commit()
        trade_id = conn.execute(
            "SELECT id FROM trades WHERE entry_ticket = ?", ("2467999999",),
        ).fetchone()[0]
        conn.close()

        # close() chega com profit NOVO (reconciliação de MT5)
        close_result = _make_close_result("WSPU26", [{
            "ticket": 2467999999,
            "symbol": "WSPU26",
            "type": "BUY",
            "volume": 1.0,
            "entry_price": 50.0,
            "close_price": 57.0,
            "profit": 70.0,  # novo valor (reconciliação)
            "swap": 0.0,
            "magic": 555501,
        }])

        with patch.object(mt5_orchestrator, "_run_wine", return_value=close_result):
            result = mt5_orchestrator.close("WSPU26")

        self.assertEqual(result["db_persist"]["updated"], 1)

        # exit_time original preservado, mas gross_pnl/net_pnl atualizados
        conn = sqlite3.connect(str(self.tmp_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT exit_time, exit_price, gross_pnl, net_pnl, "
            "exit_reason, close_source FROM trades WHERE id = ?",
            (trade_id,),
        ).fetchone()
        conn.close()

        self.assertEqual(row["exit_time"], "2026-07-01 11:30:00",
                         "exit_time original deve ser preservado")
        self.assertEqual(row["gross_pnl"], 70.0,
                         "gross_pnl deve ser atualizado para reconciliação")
        self.assertEqual(row["net_pnl"], 70.0)
        self.assertEqual(row["exit_reason"], "TRAILING",
                         "exit_reason original preservado (TRAILING)")
        self.assertEqual(row["close_source"], "AUTOTRADER",
                         "close_source original preservado")


if __name__ == "__main__":
    unittest.main()