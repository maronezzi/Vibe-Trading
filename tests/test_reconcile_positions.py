"""
test_reconcile_positions.py
============================

TDD — Reconciliação state.positions / DB ↔ MT5 a cada tick (anti-orphan).

PROBLEMA IDENTIFICADO 2026-07-01:
    Bot abre ordens no MT5 mas, em casos de exceção (DB locked, JSON marshal,
    Wine subprocess timeout), NÃO consegue persistir em state.positions ou em
    vt_trades.db. O bot segue operando e cria mais orphans (4+ vezes no dia).
    MT5 fica com a posição; state vazio; DB tem ghosts ou nada.

FIX:
    ``reconcile_positions_with_mt5()`` (em core/vt_autotrader.py) é chamada
    no início de cada iteração do loop do autotrader. MT5 é fonte absoluta de
    verdade. Função ingere orphans (MT5 → state/DB) e marca ghosts
    (state → DB com exit_reason='GHOST').

O QUE ESTE TESTE PROTEGE:
    1. ``reconcile_positions_with_mt5()`` existe em ``core.vt_autotrader``.
    2. Orphan no MT5 (state vazio) → state.positions ingerido + INSERT no DB.
    3. Ghost no state (MT5 sem a posição) → state.positions removido + DB
       UPDATE com ``exit_reason='GHOST'``.
    4. Idempotência: rodar 2x não duplica o INSERT no DB.
    5. DB error não crasha: log e segue.
    6. Reconciliação é chamada no tick loop (sentinela no run_daemon).
"""
import json
import os
import sqlite3
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


def _mt5_position(ticket, symbol, direction, price_open=100.0, volume=1.0,
                  magic=555501, comment="VibeTrading", sl=0.0):
    """Helper: cria um dict de posição no formato que mt5_executor retorna."""
    return {
        "ticket": ticket,
        "symbol": symbol,
        "type": 0 if direction == "BUY" else 1,
        "volume": volume,
        "price_open": price_open,
        "price_current": price_open,
        "sl": sl,
        "tp": 0.0,
        "profit": 0.0,
        "swap": 0.0,
        "comment": comment,
        "time": "2026-07-01 12:00:00",
        "magic": magic,
        "identifier": ticket,
        "time_msc": 0,
        "reason": 0,
        "external_id": "",
    }


def _init_trades_schema(db_path):
    """Cria schema mínimo de trades em db_path (não mexe em produção)."""
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


class _TmpDBMixin:
    """Mixin: redireciona sqlite3.connect para um arquivo em tmp_path.

    O reconcile_positions_with_mt5() chama sqlite3.connect("vt_trades.db")
    diretamente, então patchamos a referência de sqlite3.connect dentro
    do módulo core.vt_autotrader para apontar pro tmp_path. Em paralelo,
    o teste faz as queries diretamente no tmp_path.
    """

    def setUp(self):
        from core import vt_autotrader
        vt_autotrader.state.positions.clear()
        self.tmp_db = self._setup_tmp_db()
        # Patch sqlite3.connect dentro do módulo para apontar pro tmp
        self._real_connect = sqlite3.connect
        self._connect_patcher = patch(
            "core.vt_autotrader.sqlite3.connect",
            side_effect=lambda *a, **kw: self._real_connect(
                str(self.tmp_db), *a[1:], **kw
            ) if not a or "vt_trades.db" in str(a[0]) else self._real_connect(*a, **kw),
        )
        # Substitui o sqlite3 dentro do módulo de forma mais limpa:
        self._sqlite3_patcher = patch(
            "core.vt_autotrader.sqlite3", self._SafeSqlite(self.real_connect_to_tmp)
        )
        self._sqlite3_patcher.start()

    def tearDown(self):
        from core import vt_autotrader
        vt_autotrader.state.positions.clear()
        try:
            self._sqlite3_patcher.stop()
        except Exception:
            pass

    def _setup_tmp_db(self):
        raise NotImplementedError

    def real_connect_to_tmp(self, *args, **kwargs):
        # Redireciona qualquer chamada com "vt_trades.db" pra tmp_db
        if args and isinstance(args[0], str) and "vt_trades.db" in args[0]:
            return self._real_connect(str(self.tmp_db), *args[1:], **kwargs)
        return self._real_connect(*args, **kwargs)

    class _SafeSqlite:
        """Wrapper que intercepta connect() pra apontar ao tmp_db."""

        def __init__(self, connect_fn):
            self._connect_fn = connect_fn
            self.OperationalError = sqlite3.OperationalError
            self.IntegrityError = sqlite3.IntegrityError

        def connect(self, *args, **kwargs):
            return self._connect_fn(*args, **kwargs)


# ──────────────────────────────────────────────────────────────────────────
# Os testes (não usam o mixin: fazem patch inline por clareza)
# ──────────────────────────────────────────────────────────────────────────


class TestReconcileFunctionExists(unittest.TestCase):
    """Sanidade: função existe em core.vt_autotrader e tem assinatura."""

    def test_function_defined(self):
        from core import vt_autotrader
        self.assertTrue(
            hasattr(vt_autotrader, "reconcile_positions_with_mt5"),
            "core.vt_autotrader deve expor reconcile_positions_with_mt5()",
        )

    def test_function_callable(self):
        from core import vt_autotrader
        self.assertTrue(callable(vt_autotrader.reconcile_positions_with_mt5))


class TestReconcileIngestsOrphan(unittest.TestCase):
    """Orphan no MT5 com state vazio → state.positions recebe e DB é populado."""

    def setUp(self):
        from core import vt_autotrader
        vt_autotrader.state.positions.clear()
        # DB temporário isolado do de produção
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_db = Path(self._tmpdir.name) / "vt_trades.db"
        _init_trades_schema(self.tmp_db)
        # Patch sqlite3.connect dentro do módulo autotrader
        self._real_connect = sqlite3.connect
        self._patcher = patch(
            "core.vt_autotrader.sqlite3.connect",
            side_effect=self._connect_to_tmp,
        )
        self._patcher.start()

    def tearDown(self):
        from core import vt_autotrader
        vt_autotrader.state.positions.clear()
        self._patcher.stop()
        self._tmpdir.cleanup()

    def _connect_to_tmp(self, *args, **kwargs):
        if args and isinstance(args[0], str) and "vt_trades.db" in args[0]:
            return self._real_connect(str(self.tmp_db), *args[1:], **kwargs)
        return self._real_connect(*args, **kwargs)

    def test_orphan_ingested_into_state(self):
        from core import vt_autotrader
        mt5_pos = _mt5_position(2467793364, "WSPU26", "BUY", price_open=50.0)
        fake_status = {
            "account": {"balance": 1000.0, "equity": 1000.0},
            "positions": [mt5_pos],
            "n_positions": 1,
        }
        with patch("core.vt_autotrader.status", return_value=fake_status), \
             patch("core.vt_autotrader.log") as _log:
            vt_autotrader.reconcile_positions_with_mt5()

        self.assertEqual(
            len(vt_autotrader.state.positions), 1,
            f"state.positions deveria ter 1 entrada após reconcile, "
            f"veio {len(vt_autotrader.state.positions)}: {dict(vt_autotrader.state.positions)}"
        )
        pos_dict = next(iter(vt_autotrader.state.positions.values()))
        self.assertEqual(pos_dict["entry_ticket"], "2467793364")
        self.assertEqual(pos_dict["direction"], "BUY")
        self.assertEqual(pos_dict["entry_price"], 50.0)
        self.assertTrue(pos_dict.get("reconciled"), "reconciled flag deve ser True")

    def test_orphan_inserted_into_db(self):
        from core import vt_autotrader
        mt5_pos = _mt5_position(2467831005, "WINQ26", "BUY", price_open=120.0)
        fake_status = {
            "account": {},
            "positions": [mt5_pos],
            "n_positions": 1,
        }
        with patch("core.vt_autotrader.status", return_value=fake_status), \
             patch("core.vt_autotrader.log"):
            vt_autotrader.reconcile_positions_with_mt5()

        # DB deve ter a entrada
        conn = self._real_connect(str(self.tmp_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM trades WHERE entry_ticket = ?",
            ("2467831005",),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row, "DB deveria ter trade com entry_ticket=2467831005")
        self.assertEqual(row["symbol"], "WINQ26")
        self.assertEqual(row["direction"], "BUY")
        self.assertEqual(row["entry_price"], 120.0)
        pos_dict = next(iter(vt_autotrader.state.positions.values()))
        self.assertEqual(pos_dict["trade_log_id"], row["id"])

    def test_orphan_log_message_emitted(self):
        from core import vt_autotrader
        mt5_pos = _mt5_position(2467793364, "WSPU26", "BUY")
        fake_status = {"account": {}, "positions": [mt5_pos], "n_positions": 1}
        with patch("core.vt_autotrader.status", return_value=fake_status), \
             patch("core.vt_autotrader.log") as _log:
            vt_autotrader.reconcile_positions_with_mt5()
        log_calls = [str(c) for c in _log.call_args_list]
        any_reconcile = any("[RECONCILE]" in c and "2467793364" in c for c in log_calls)
        self.assertTrue(
            any_reconcile,
            f"Esperava log '[RECONCILE] Ingerido orphan ... 2467793364 ...', "
            f"obtido: {log_calls[:5]}"
        )


class TestReconcileMarksGhost(unittest.TestCase):
    """state tem posição mas MT5 não tem mais → marca como GHOST e remove."""

    def setUp(self):
        from core import vt_autotrader
        vt_autotrader.state.positions.clear()
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_db = Path(self._tmpdir.name) / "vt_trades.db"
        _init_trades_schema(self.tmp_db)
        self._real_connect = sqlite3.connect
        self._patcher = patch(
            "core.vt_autotrader.sqlite3.connect",
            side_effect=self._connect_to_tmp,
        )
        self._patcher.start()

    def tearDown(self):
        from core import vt_autotrader
        vt_autotrader.state.positions.clear()
        self._patcher.stop()
        self._tmpdir.cleanup()

    def _connect_to_tmp(self, *args, **kwargs):
        if args and isinstance(args[0], str) and "vt_trades.db" in args[0]:
            return self._real_connect(str(self.tmp_db), *args[1:], **kwargs)
        return self._real_connect(*args, **kwargs)

    def test_ghost_removed_from_state(self):
        from core import vt_autotrader
        vt_autotrader.state.positions["WINQ26_M5"] = {
            "direction": "BUY",
            "entry_price": 120.0,
            "entry_ticket": "999999999",
            "sl_pts": 200, "atr": 100, "trail_on": False,
            "best_price": 120.0, "bar_count": 5,
            "trade_log_id": None, "strategy": "VWAP",
            "entry_time": datetime.now(), "volume": 1.0, "tf": "M5",
        }
        fake_status = {"account": {}, "positions": [], "n_positions": 0}
        with patch("core.vt_autotrader.status", return_value=fake_status), \
             patch("core.vt_autotrader.log"):
            vt_autotrader.reconcile_positions_with_mt5()

        self.assertEqual(
            len(vt_autotrader.state.positions), 0,
            f"state.positions deveria estar vazio após ghost reconcile, "
            f"veio {len(vt_autotrader.state.positions)}: {dict(vt_autotrader.state.positions)}"
        )

    def test_ghost_marked_in_db(self):
        from core import vt_autotrader
        # Inserir trade no DB
        conn = self._real_connect(str(self.tmp_db))
        conn.execute("""
            INSERT INTO trades (symbol, direction, volume, entry_time, entry_price,
                                entry_ticket, exit_time, exit_reason)
            VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
        """, ("WINQ26", "BUY", 1.0, "2026-07-01 11:00:00", 120.0, "999999999"))
        conn.commit()
        trade_id = conn.execute(
            "SELECT id FROM trades WHERE entry_ticket = ?", ("999999999",)
        ).fetchone()[0]
        conn.close()

        vt_autotrader.state.positions["WINQ26_M5"] = {
            "direction": "BUY",
            "entry_price": 120.0,
            "entry_ticket": "999999999",
            "sl_pts": 200, "atr": 100, "trail_on": False,
            "best_price": 120.0, "bar_count": 5,
            "trade_log_id": trade_id, "strategy": "VWAP",
            "entry_time": datetime.now(), "volume": 1.0, "tf": "M5",
        }

        fake_status = {"account": {}, "positions": [], "n_positions": 0}
        with patch("core.vt_autotrader.status", return_value=fake_status), \
             patch("core.vt_autotrader.log"):
            vt_autotrader.reconcile_positions_with_mt5()

        conn = self._real_connect(str(self.tmp_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT exit_time, exit_reason, close_source FROM trades WHERE id = ?",
            (trade_id,),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(
            row["exit_reason"], "GHOST",
            f"DB deveria ter exit_reason='GHOST' para ticket 999999999, "
            f"veio {row['exit_reason']}"
        )
        self.assertIsNotNone(row["exit_time"], "exit_time deve estar preenchido")
        self.assertEqual(row["close_source"], "RECONCILE")

    def test_ghost_log_message_emitted(self):
        from core import vt_autotrader
        vt_autotrader.state.positions["WINQ26_M5"] = {
            "direction": "BUY",
            "entry_price": 120.0,
            "entry_ticket": "888888888",
            "sl_pts": 200, "atr": 100, "trail_on": False,
            "best_price": 120.0, "bar_count": 1,
            "trade_log_id": None, "strategy": "VWAP",
            "entry_time": datetime.now(), "volume": 1.0, "tf": "M5",
        }
        fake_status = {"account": {}, "positions": [], "n_positions": 0}
        with patch("core.vt_autotrader.status", return_value=fake_status), \
             patch("core.vt_autotrader.log") as _log:
            vt_autotrader.reconcile_positions_with_mt5()
        log_calls = [str(c) for c in _log.call_args_list]
        any_ghost = any("[RECONCILE]" in c and "Ghost" in c and "888888888" in c
                        for c in log_calls)
        self.assertTrue(
            any_ghost,
            f"Esperava log '[RECONCILE] Ghost detectado ... 888888888', "
            f"obtido: {log_calls[:5]}"
        )


class TestReconcileIdempotent(unittest.TestCase):
    """Rodar reconcile 2x não duplica entradas no DB nem no state."""

    def setUp(self):
        from core import vt_autotrader
        vt_autotrader.state.positions.clear()
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_db = Path(self._tmpdir.name) / "vt_trades.db"
        _init_trades_schema(self.tmp_db)
        self._real_connect = sqlite3.connect
        self._patcher = patch(
            "core.vt_autotrader.sqlite3.connect",
            side_effect=self._connect_to_tmp,
        )
        self._patcher.start()

    def tearDown(self):
        from core import vt_autotrader
        vt_autotrader.state.positions.clear()
        self._patcher.stop()
        self._tmpdir.cleanup()

    def _connect_to_tmp(self, *args, **kwargs):
        if args and isinstance(args[0], str) and "vt_trades.db" in args[0]:
            return self._real_connect(str(self.tmp_db), *args[1:], **kwargs)
        return self._real_connect(*args, **kwargs)

    def test_running_twice_does_not_duplicate(self):
        from core import vt_autotrader
        mt5_pos = _mt5_position(2467793364, "WSPU26", "BUY", price_open=50.0)
        fake_status = {"account": {}, "positions": [mt5_pos], "n_positions": 1}

        with patch("core.vt_autotrader.status", return_value=fake_status), \
             patch("core.vt_autotrader.log"):
            vt_autotrader.reconcile_positions_with_mt5()
            n_state_1 = len(vt_autotrader.state.positions)

            vt_autotrader.reconcile_positions_with_mt5()
            n_state_2 = len(vt_autotrader.state.positions)

        self.assertEqual(n_state_1, 1, f"1a execução deveria ter 1 posição, veio {n_state_1}")
        self.assertEqual(n_state_2, 1, f"2a execução deveria manter 1 posição, veio {n_state_2}")

        conn = self._real_connect(str(self.tmp_db))
        count = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE entry_ticket = ?",
            ("2467793364",),
        ).fetchone()[0]
        conn.close()
        self.assertEqual(
            count, 1, f"DB deveria ter 1 trade com entry_ticket=2467793364, veio {count}"
        )


class TestReconcileSafeOnDBError(unittest.TestCase):
    """DB error não deve crashar o bot."""

    def setUp(self):
        from core import vt_autotrader
        vt_autotrader.state.positions.clear()

    def tearDown(self):
        from core import vt_autotrader
        vt_autotrader.state.positions.clear()

    def test_db_error_does_not_crash(self):
        from core import vt_autotrader
        mt5_pos = _mt5_position(2467793364, "WSPU26", "BUY")
        fake_status = {"account": {}, "positions": [mt5_pos], "n_positions": 1}

        with patch("core.vt_autotrader.status", return_value=fake_status), \
             patch("core.vt_autotrader.log"), \
             patch("core.vt_autotrader.sqlite3.connect",
                   side_effect=Exception("disk full")):
            try:
                vt_autotrader.reconcile_positions_with_mt5()
            except Exception as e:
                self.fail(
                    f"reconcile_positions_with_mt5 não deveria propagar "
                    f"DB errors, mas lançou: {e}"
                )

    def test_mt5_error_does_not_crash(self):
        from core import vt_autotrader
        with patch("core.vt_autotrader.status",
                   side_effect=Exception("MT5 indisponível")), \
             patch("core.vt_autotrader.log"):
            try:
                vt_autotrader.reconcile_positions_with_mt5()
            except Exception as e:
                self.fail(
                    f"reconcile_positions_with_mt5 não deveria propagar "
                    f"MT5 errors, mas lançou: {e}"
                )

    def test_malformed_status_returns_gracefully(self):
        from core import vt_autotrader
        with patch("core.vt_autotrader.status", return_value="not a dict"), \
             patch("core.vt_autotrader.log"):
            try:
                vt_autotrader.reconcile_positions_with_mt5()
            except Exception as e:
                self.fail(
                    f"reconcile_positions_with_mt5 não deveria crashar com "
                    f"status malformado, lançou: {e}"
                )

    def test_empty_positions_is_noop(self):
        from core import vt_autotrader
        with patch("core.vt_autotrader.status",
                   return_value={"account": {}, "positions": [], "n_positions": 0}), \
             patch("core.vt_autotrader.log"):
            vt_autotrader.reconcile_positions_with_mt5()
        self.assertEqual(len(vt_autotrader.state.positions), 0)


class TestReconcileIgnoresOtherMagic(unittest.TestCase):
    """Posições de outros EAs (magic != 555501) devem ser ignoradas."""

    def setUp(self):
        from core import vt_autotrader
        vt_autotrader.state.positions.clear()
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_db = Path(self._tmpdir.name) / "vt_trades.db"
        _init_trades_schema(self.tmp_db)
        self._real_connect = sqlite3.connect
        self._patcher = patch(
            "core.vt_autotrader.sqlite3.connect",
            side_effect=self._connect_to_tmp,
        )
        self._patcher.start()

    def tearDown(self):
        from core import vt_autotrader
        vt_autotrader.state.positions.clear()
        self._patcher.stop()
        self._tmpdir.cleanup()

    def _connect_to_tmp(self, *args, **kwargs):
        if args and isinstance(args[0], str) and "vt_trades.db" in args[0]:
            return self._real_connect(str(self.tmp_db), *args[1:], **kwargs)
        return self._real_connect(*args, **kwargs)

    def test_other_magic_not_ingested(self):
        from core import vt_autotrader
        other_pos = _mt5_position(11111, "WINQ26", "BUY", magic=12345,
                                  comment="OtherEA")
        our_pos = _mt5_position(22222, "WDOQ26", "SELL", magic=555501,
                                comment="VibeTrading")
        fake_status = {
            "account": {},
            "positions": [other_pos, our_pos],
            "n_positions": 2,
        }
        with patch("core.vt_autotrader.status", return_value=fake_status), \
             patch("core.vt_autotrader.log"):
            vt_autotrader.reconcile_positions_with_mt5()

        self.assertEqual(
            len(vt_autotrader.state.positions), 1,
            f"Só nossa posição (magic 555501) deveria ser ingerida, "
            f"veio {len(vt_autotrader.state.positions)}: {dict(vt_autotrader.state.positions)}"
        )
        pos_dict = next(iter(vt_autotrader.state.positions.values()))
        self.assertEqual(pos_dict["entry_ticket"], "22222")


class TestReconcileCalledInDaemon(unittest.TestCase):
    """A função deve ser chamada a cada iteração do daemon loop."""

    def test_run_daemon_loop_calls_reconcile(self):
        from core import vt_autotrader
        src = Path(PROJECT_ROOT) / "core" / "vt_autotrader.py"
        source = src.read_text()
        self.assertIn(
            "reconcile_positions_with_mt5()",
            source,
            "reconcile_positions_with_mt5() deve ser chamada em algum ponto "
            "do run_daemon",
        )
        run_daemon_idx = source.find("def run_daemon")
        self.assertGreater(run_daemon_idx, 0, "run_daemon deve existir")
        after_daemon = source[run_daemon_idx:]
        self.assertIn(
            "reconcile_positions_with_mt5()",
            after_daemon,
            "reconcile_positions_with_mt5() deve ser chamada dentro de run_daemon",
        )


class TestReconcileDoesNotIngestOldTrades(unittest.TestCase):
    """FIX 2026-07-01 (Wave anti-lixo).

    Cenário do bug das 12:58:01: o reconcile rodou e criou objeto fake
    state.positions['WDOQ26_M5'] = {direction=SELL, entry_price=100,
    entry_ticket='22222', trade_log_id=1} sendo que trade_log_id=1 era
    WINQ26 SELL de JUNHO (3 semanas atrás, JÁ FECHADO). O reconcile NÃO
    pode:
      (a) propagar lixo do DB antigo para state.positions quando MT5 está
          vazio;
      (b) criar INSERT novo no DB com dados sintéticos de um estado fake.

    Estes testes garantem que:
      1. DB com trade VELHO FECHADO + MT5 vazio → state vazio, DB sem novo
         INSERT.
      2. MT5 com posição real + DB retornando lixo fantasma → state só com
         dados DO MT5 (entry_price=preço MT5, NÃO do DB).
    """

    def setUp(self):
        from core import vt_autotrader
        vt_autotrader.state.positions.clear()
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_db = Path(self._tmpdir.name) / "vt_trades.db"
        _init_trades_schema(self.tmp_db)
        self._real_connect = sqlite3.connect
        self._patcher = patch(
            "core.vt_autotrader.sqlite3.connect",
            side_effect=self._connect_to_tmp,
        )
        self._patcher.start()

    def tearDown(self):
        from core import vt_autotrader
        vt_autotrader.state.positions.clear()
        self._patcher.stop()
        self._tmpdir.cleanup()

    def _connect_to_tmp(self, *args, **kwargs):
        if args and isinstance(args[0], str) and "vt_trades.db" in args[0]:
            return self._real_connect(str(self.tmp_db), *args[1:], **kwargs)
        return self._real_connect(*args, **kwargs)

    def test_no_orphan_ingestion_when_mt5_empty_and_db_has_old_closed_trade(self):
        """DB tem trade de JUNHO (id=1) JÁ FECHADO + MT5 vazio.

        Esperado: state.positions fica vazio E DB NÃO ganha INSERT novo.
        """
        from core import vt_autotrader

        # Inserir trade id=1 (antigo, JÁ FECHADO) — réplica do bug
        conn = self._real_connect(str(self.tmp_db))
        conn.execute("""
            INSERT INTO trades (id, symbol, direction, volume, entry_time,
                                entry_price, entry_sl, entry_ticket, exit_time,
                                exit_price, exit_reason)
            VALUES (1, 'WINQ26', 'SELL', 1.0, '2026-06-09 11:14:38', 174395.0,
                    173000.0, '2452826444', '2026-06-09 11:17:02', 174395.0,
                    'SL_SERVIDOR')
        """)
        conn.commit()
        conn.close()

        # MT5 vazio
        fake_status = {"account": {}, "positions": [], "n_positions": 0}
        with patch("core.vt_autotrader.status", return_value=fake_status), \
             patch("core.vt_autotrader.log") as _log:
            vt_autotrader.reconcile_positions_with_mt5()

        # state DEVE estar vazio (não houve orphan no MT5)
        self.assertEqual(
            len(vt_autotrader.state.positions), 0,
            f"state.positions deveria estar vazio (MT5 vazio), "
            f"veio {len(vt_autotrader.state.positions)}: "
            f"{dict(vt_autotrader.state.positions)}"
        )

        # DB NÃO pode ter ganhado novo INSERT — só o trade id=1 original
        conn = self._real_connect(str(self.tmp_db))
        count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        conn.close()
        self.assertEqual(
            count, 1,
            f"DB deveria ter 1 trade (o antigo WINQ26 de JUNHO), veio {count}. "
            f"O reconcile NÃO pode criar INSERTs fantasma a partir de dados "
            f"do DB antigo."
        )

    def test_only_uses_mt5_data_not_db_junk(self):
        """MT5 com WSPU26 BUY ticket X → state.positions tem WSPU26 com dados
        DO MT5 (price_open=7539.25). Dados do DB JUNK (entry_price=100,
        sl_pts=0, atr=0) devem ser IGNORADOS."""
        from core import vt_autotrader

        # Inserir trade VELHO JÁ FECHADO com dados LIXO no DB
        # (entry_price=100, sem exit_time — pra simular o cenário problemático
        # onde a query antiga poderia trazer um trade fantasma)
        _today_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = self._real_connect(str(self.tmp_db))
        conn.execute("""
            INSERT INTO trades (id, symbol, direction, volume, entry_time,
                                entry_price, entry_ticket, strategy, exit_time,
                                exit_reason)
            VALUES (1, 'WINQ26', 'SELL', 1.0, '2026-06-09 11:14:38', 100.0,
                    'JUNK_TICKET_22222', 'VWAP', NULL, NULL)
        """)
        conn.commit()
        conn.close()

        # MT5 retorna WSPU26 BUY real com ticket novo e price_open=7539.25
        mt5_pos = _mt5_position(2467898858, "WSPU26", "BUY",
                                price_open=7539.25, volume=1.0)
        fake_status = {"account": {}, "positions": [mt5_pos], "n_positions": 1}
        with patch("core.vt_autotrader.status", return_value=fake_status), \
             patch("core.vt_autotrader.log") as _log:
            vt_autotrader.reconcile_positions_with_mt5()

        # state.positions DEVE ter APENAS WSPU26 BUY com dados DO MT5
        self.assertEqual(
            len(vt_autotrader.state.positions), 1,
            f"state.positions deveria ter 1 entrada (WSPU26 do MT5), "
            f"veio {len(vt_autotrader.state.positions)}: "
            f"{dict(vt_autotrader.state.positions)}"
        )

        pos_dict = next(iter(vt_autotrader.state.positions.values()))
        # entry_price DEVE vir do MT5 (7539.25), NUNCA do DB (100.0)
        self.assertEqual(
            pos_dict["entry_price"], 7539.25,
            f"entry_price deve ser o do MT5 (7539.25), não do DB (100.0). "
            f"Veio {pos_dict['entry_price']}"
        )
        # Ticket deve ser o do MT5, NÃO o 'JUNK_TICKET_22222' do DB
        self.assertEqual(
            pos_dict["entry_ticket"], "2467898858",
            f"entry_ticket deve ser o do MT5 (2467898858), "
            f"não do DB (JUNK_TICKET_22222). Veio {pos_dict['entry_ticket']}"
        )
        # Direction deve ser BUY (a direção da posição MT5)
        self.assertEqual(
            pos_dict["direction"], "BUY",
            f"direction deve ser BUY (do MT5). Veio {pos_dict['direction']}"
        )
        # DB: o INSERT original (JUNK que eu inseri na fixture) deve
        # continuar lá (não foi mexido pelo reconcile). MAS o reconcile
        # NÃO pode ter criado um INSERT novo USANDO DADOS DO JUNK
        # (ex.: outro trade com entry_price=100 ou ticket='JUNK_TICKET_22222'
        # ou strategy='RECONCILED' baseado no lixo). Pode haver 1 INSERT
        # legítimo (do MT5), mas ele nunca pode ter entry_price=100 nem
        # ticket=JUNK.
        conn = self._real_connect(str(self.tmp_db))
        conn.row_factory = sqlite3.Row
        new_rows = conn.execute("""
            SELECT id, symbol, entry_price, entry_ticket, exit_reason,
                   strategy, entry_time
            FROM trades
            WHERE id != 1
        """).fetchall()
        conn.close()
        # Pode haver 1 INSERT legítimo (do orphan WSPU26 do MT5).
        # O que NÃO pode acontecer: usar entry_price do JUNK, ou ticket
        # JUNK, ou strategy herdada do JUNK.
        for r in new_rows:
            d = dict(r)
            self.assertNotEqual(
                d.get("entry_price"), 100.0,
                f"INSERT novo usando entry_price=100 (do JUNK do DB): {d}"
            )
            self.assertNotEqual(
                str(d.get("entry_ticket", "")), "JUNK_TICKET_22222",
                f"INSERT novo usando entry_ticket=JUNK_TICKET_22222 (do DB): {d}"
            )

    def test_no_insert_when_mt5_data_invalid(self):
        """MT5 retorna posição com entry_price=0 ou volume=0 → deve ser
        IGNORADA (skip + warn), nunca persistida em state.positions nem DB."""
        from core import vt_autotrader

        # Posição MT5 com dados ZERADOS (price_open=0, volume=0) — lixo
        junk_pos = _mt5_position(99999, "WSPU26", "BUY",
                                 price_open=0.0, volume=0.0)
        fake_status = {"account": {}, "positions": [junk_pos], "n_positions": 1}
        with patch("core.vt_autotrader.status", return_value=fake_status), \
             patch("core.vt_autotrader.log") as _log:
            vt_autotrader.reconcile_positions_with_mt5()

        # state.positions DEVE ficar vazio (lixo foi skipado)
        self.assertEqual(
            len(vt_autotrader.state.positions), 0,
            f"state.positions deveria estar vazio (MT5 com lixo), "
            f"veio {len(vt_autotrader.state.positions)}: "
            f"{dict(vt_autotrader.state.positions)}"
        )

        # DB NÃO pode ter INSERT novo
        conn = self._real_connect(str(self.tmp_db))
        count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        conn.close()
        self.assertEqual(
            count, 0,
            f"DB deveria estar vazio (lixo ignorado), veio {count} linhas. "
            f"O reconcile NÃO pode inserir dados MT5 com entry_price=0/volume=0."
        )

        # Log deve ter avisado sobre skip
        log_calls = [str(c) for c in _log.call_args_list]
        any_skip_warn = any("skip ingest" in c.lower() for c in log_calls)
        self.assertTrue(
            any_skip_warn,
            f"Log deveria warn sobre skip ingest de MT5 com dados inválidos. "
            f"Logs: {[c for c in log_calls if 'skip' in c.lower()][:5]}"
        )

    def test_state_only_ghost_without_trade_log_id_does_not_create_fake_insert(self):
        """Bug original: state.positions[X] com entry_ticket='22222',
        entry_price=100, sem trade_log_id, e MT5 vazio → reconcile rodando
        virava um INSERT no DB com symbol=direction (LIXO!), entry_price=100.

        FIX: agora, sem trade_log_id, o reconcile SÓ insere se houver
        symbol coerente E entry_price > 0 E volume > 0. Sem isso, apenas
        remove do state (nenhum INSERT criado)."""
        from core import vt_autotrader

        # Simular o objeto fake que apareceu hoje no state
        vt_autotrader.state.positions["WDOQ26_M5"] = {
            "direction": "SELL",
            "entry_price": 100.0,           # ← LIXO
            "entry_ticket": "22222",         # ← LIXO
            "sl_pts": 0,                     # ← LIXO
            "atr": 0,                        # ← LIXO
            "trail_on": False,
            "best_price": 100.0,
            "bar_count": 1,
            "trade_log_id": None,             # ← causa do bug: None
            "strategy": "RECONCILED",
            "entry_time": datetime.now(),
            "volume": 1.0,
            "tf": "M5",
            "reconciled": True,
        }

        # MT5 vazio (a posição WDOQ26 já não existe no broker)
        fake_status = {"account": {}, "positions": [], "n_positions": 0}
        with patch("core.vt_autotrader.status", return_value=fake_status), \
             patch("core.vt_autotrader.log") as _log:
            vt_autotrader.reconcile_positions_with_mt5()

        # state DEVE estar vazio (ghost removido)
        self.assertEqual(
            len(vt_autotrader.state.positions), 0,
            f"state deveria estar vazio (ghost removido), "
            f"veio {len(vt_autotrader.state.positions)}: "
            f"{dict(vt_autotrader.state.positions)}"
        )

        # DB NÃO pode ter INSERT com entry_price=100/ticket=22222
        conn = self._real_connect(str(self.tmp_db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id, symbol, entry_price, entry_ticket, exit_reason
            FROM trades
        """).fetchall()
        conn.close()

        # Filtrar linhas lixo (entry_price=100 + ticket='22222' seria o fake)
        for row in rows:
            d = dict(row)
            if d.get("entry_price") == 100.0 and str(d.get("entry_ticket", "")) == "22222":
                self.fail(
                    f"DB tem INSERT fantasma com entry_price=100 e "
                    f"ticket=22222 — bug original não foi corrigido. "
                    f"Linha: {d}"
                )
        # E nenhum INSERT com strategy='GHOST' (que seria a marca do fake)
        any_ghost_insert = any(r.get("exit_reason") == "GHOST" for r in rows)
        self.assertFalse(
            any_ghost_insert,
            f"DB NÃO deveria ter INSERTs com exit_reason='GHOST' para "
            f"estado sem trade_log_id e sem dados válidos. Linhas: "
            f"{[dict(r) for r in rows]}"
        )


if __name__ == "__main__":
    unittest.main()
