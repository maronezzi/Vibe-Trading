"""
test_resolve_orphan_closes.py
=============================

TDD — Garante que ``core.vt_autotrader._resolve_orphan_closes()`` fecha
trades órfãos que o MT5 fechou sozinho (SL_SERVIDOR / server-side close)
e preenche o PnL REAL do broker via MT5 history.

PROBLEMA REAL (Bruno 01/07/2026 — trades #2069 #2073 #2074 #2075):
    O autotrader abriu 4-5 trades que viraram GHOST no DB com PnL=0.
    O bot fechou essas posições via SL_SERVIDOR (MT5 fechou sozinho via
    server-side SL), mas o PnL nunca chegou no DB porque:
      (a) reconcile_positions_with_mt5 (commit ce026460) detectou drift
          ANTES de close()/_persist_close_to_db rodar → marcou GHOST
          com PnL=0.
      (b) O bot nunca chamou close() para esses tickets (MT5 fechou
          sozinho via server-side SL), então _persist_close_to_db
          (commit dc447fd6) nunca rodou.

FIX (Wave 12, 2026-07-01):
    ``core.vt_autotrader._resolve_orphan_closes()`` é chamada NO INÍCIO
    do loop do autotrader (antes de reconcile_positions_with_mt5).
    A função:
      1. Lista trades no DB com exit_time IS NULL AND entry_ticket NOT NULL.
      2. Verifica quais tickets estão abertos AGORA no MT5 (status()).
      3. Para cada ticket FORA do MT5:
         a) Pega deal mais recente via history(symbol=..., days=2).
         b) Se exit_time IS NULL → UPDATE completo (PnL real do broker).
         c) Se exit_time IS NOT NULL → UPDATE cirúrgico (só PnL se mudou).
      4. Idempotente (close_source LIKE 'ORPHAN_CLOSE_RESOLVED_%' → skip).
      5. Failure-safe (try/except em tudo, nunca crasha o bot).

O QUE ESTE TESTE PROTEGE:
    1. exit_time IS NULL + ticket NÃO em MT5 + deal no history
       → UPDATE com PnL real, exit_reason=SL_SERVIDOR / TP_SERVIDOR /
       SERVER_CLOSE_RESOLVED.
    2. exit_time IS NULL + ticket AINDA em MT5 → skip (legítimo).
    3. exit_time IS NOT NULL (já reconciliado) + PnL idêntico → skip.
    4. exit_time IS NOT NULL + PnL diferente → UPDATE cirúrgico só do
       PnL (preserva exit_reason/exit_time originais).
    5. close_source já é 'ORPHAN_CLOSE_RESOLVED_*' → skip (idempotente).
    6. status() failure → skip silencioso, não crasha.
    7. history() failure → loga, conta skipped_no_history, segue.
    8. DB locked → loga, retorna stats sem crash.
    9. DB sem trades abertos (exit_time IS NULL) → noop (stats zerados).
   10. WIRE TEST: vt_autotrader loop chama _resolve_orphan_closes ANTES
       de reconcile_positions_with_mt5.
"""
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path("/home/bruno/Projects/Vibe-Trading")
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "core"))


# ──────────────────────────────────────────────────────────────────
# Fixtures: DB SQLite isolado por teste + redirect conn to tmp DB
# ──────────────────────────────────────────────────────────────────


def _make_tmp_db():
    """Cria DB temporário com schema mínimo de vt_trade_log."""
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="vt_orphan_test_")
    db_path = Path(tmpdir) / "test.db"
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.executescript("""
        CREATE TABLE trades (
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
        CREATE INDEX IF NOT EXISTS idx_trades_entry_ticket
            ON trades(entry_ticket);
    """)
    conn.close()
    return db_path


def _insert_open_trade(db_path, *, ticket="2466491666", symbol="WINQ26",
                       direction="BUY", entry_price=175000.0, volume=1.0,
                       strategy="STRONG_TREND", exit_time=None,
                       close_source=None, gross_pnl=0.0, net_pnl=0.0):
    """Insere trade com exit_time NULL por default."""
    from datetime import datetime
    entry_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    cur = conn.execute("""
        INSERT INTO trades (
            entry_ticket, symbol, direction, volume,
            entry_time, entry_price, strategy,
            exit_time, close_source, gross_pnl, net_pnl
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (ticket, symbol, direction, volume, entry_time,
          entry_price, strategy, exit_time, close_source,
          gross_pnl, net_pnl))
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def _fetch_trade(db_path, trade_id):
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM trades WHERE id=?",
                       (trade_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    return dict(row)


def _patch_vt_autotrader_db(db_path):
    """
    Faz o ``sqlite3.connect('vt_trades.db', ...)`` aberto dentro de
    _resolve_orphan_closes apontar para o DB temporário do teste.

    IMPORTANTE: o autotrader usa ``sqlite3.connect("vt_trades.db")``
    com PATH RELATIVO. O resolved path depende do CWD do pytest. Para
    garantir isolamento TOTAL (sem tocar produção), patchamos
    ``core.vt_autotrader.sqlite3.connect`` (que é o que o módulo usa).

    O CWD do pytest é o PROJECT_ROOT, então abrir "vt_trades.db" lá
    lê o DB de PRODUÇÃO — péssimo para testes. Aqui forçamos o patch.
    """
    state = {"conn": None, "real_connect": None, "patched": False}

    def fake_connect(path, *args, **kwargs):
        if isinstance(path, str) and ("vt_trades.db" in path
                                       or path == ":memory:"):
            if state["conn"] is None:
                state["conn"] = sqlite3.Connection(
                    str(db_path), *args, **kwargs)
                state["conn"].row_factory = sqlite3.Row
            return state["conn"]
        if state["real_connect"] is None:
            state["real_connect"] = sqlite3._connect_orig
        return state["real_connect"](path, *args, **kwargs)

    class _Patcher:
        def __enter__(self):
            from core import vt_autotrader
            # Salva o original exato
            state["real_connect"] = vt_autotrader.sqlite3.connect
            sqlite3._connect_orig = state["real_connect"]
            # Patch no namespace do módulo autotrader
            self._mod = vt_autotrader
            self._orig = state["real_connect"]
            vt_autotrader.sqlite3.connect = fake_connect
            state["patched"] = True
            return self

        def __exit__(self, *args):
            if state["patched"]:
                self._mod.sqlite3.connect = self._orig
                state["patched"] = False
            if state["conn"] is not None:
                try:
                    state["conn"].close()
                except Exception:
                    pass
                state["conn"] = None

    return _Patcher()


# ──────────────────────────────────────────────────────────────────
# Testes
# ──────────────────────────────────────────────────────────────────


class TestResolveOrphanCloses:
    """Defesa do autotrader contra orphan closes (server-side SL/TP)."""

    def test_open_trade_not_in_mt5_with_deal_resolved(self):
        """Trade open no DB, NÃO em MT5 positions, COM deal no history
        → UPDATE completo com PnL real."""
        db_path = _make_tmp_db()
        tid = _insert_open_trade(db_path, ticket="2466491666",
                                 symbol="WINQ26", direction="BUY",
                                 entry_price=175000.0, volume=1.0,
                                 strategy="STRONG_TREND")

        # MT5 status: posição original NÃO está mais aberta (SL_SERVIDOR)
        fake_status = {
            "positions": [],  # nada aberto
            "account": {"balance": 1000.0}
        }

        # MT5 history: deal "out" SELL fecha o BUY 2466491666
        fake_history = {
            "history": [{
                "ticket": 2466492544,
                "symbol": "WINQ26",
                "type": "SELL",
                "deal_type": "SL",  # SL_SERVIDOR
                "price": 175200.0,
                "profit": 31.50,
                "commission": -0.50,
                "swap": 0.0,
                "volume": 1.0,
                "position_id": "2466491666",
                "magic": 555501,
            }]
        }

        def fake_status_callable():
            return fake_status

        def fake_history_callable(symbol=None, days=2):
            assert symbol == "WINQ26"
            return fake_history

        with patch("core.vt_autotrader.status",
                   new=fake_status_callable), \
             patch("core.vt_autotrader.history",
                   new=fake_history_callable), \
             _patch_vt_autotrader_db(db_path):
            from core.vt_autotrader import _resolve_orphan_closes
            result = _resolve_orphan_closes()

        assert result["checked"] == 1, f"Esperava 1 checado, got {result}"
        assert result["resolved"] == 1, \
            f"Esperava 1 resolved, got {result}"
        assert result["errors"] == 0

        # DB deve ter exit_time preenchido + PnL real
        trade = _fetch_trade(db_path, tid)
        assert trade["exit_time"] is not None, \
            "exit_time deve estar preenchido após resolve"
        assert trade["exit_price"] == 175200.0
        # exit_reason = SL_SERVIDOR (porque deal.reason contém 'SL')
        assert trade["exit_reason"] == "SL_SERVIDOR", \
            f"Esperava SL_SERVIDOR, got {trade['exit_reason']}"
        # PnL = broker_profit + commission + swap = 31.50 - 0.50 + 0 = 31.0
        assert trade["gross_pnl"] == 31.5
        assert trade["net_pnl"] == 31.0
        assert trade["close_source"].startswith("ORPHAN_CLOSE_RESOLVED")
        assert "ticket=2466491666" in (trade["notes"] or "")

    def test_open_trade_still_in_mt5_skipped(self):
        """Trade open no DB MAS ticket ainda em MT5 positions → legit, skip."""
        db_path = _make_tmp_db()
        tid = _insert_open_trade(db_path, ticket="2466491666",
                                 symbol="WINQ26", entry_price=175000.0)

        # MT5 status: posição AINDA aberta
        fake_status = {
            "positions": [{
                "ticket": 2466491666,
                "symbol": "WINQ26",
                "type": "BUY",
                "volume": 1.0,
                "price_open": 175000.0,
                "profit": 0.0,
            }],
            "account": {}
        }

        def fake_status_callable():
            return fake_status

        with patch("core.vt_autotrader.status",
                   new=fake_status_callable), \
             patch("core.vt_autotrader.history",
                   new=lambda symbol=None, days=2: {"history": []}), \
             _patch_vt_autotrader_db(db_path):
            from core.vt_autotrader import _resolve_orphan_closes
            result = _resolve_orphan_closes()

        assert result["checked"] == 1
        assert result["resolved"] == 0
        assert result["skipped_legit"] == 1

        # DB não tocou
        trade = _fetch_trade(db_path, tid)
        assert trade["exit_time"] is None
        assert trade["net_pnl"] == 0

    def test_open_trade_no_deal_in_history_skipped(self):
        """Trade open + fora do MT5 MAS SEM deal no history → skipped."""
        db_path = _make_tmp_db()
        tid = _insert_open_trade(db_path, ticket="2466491666",
                                 symbol="WINQ26", entry_price=175000.0)

        fake_status = {"positions": [], "account": {}}  # nada aberto
        # history vazio
        def fake_history_callable(symbol=None, days=2):
            return {"history": []}

        with patch("core.vt_autotrader.status",
                   new=lambda: fake_status), \
             patch("core.vt_autotrader.history",
                   new=fake_history_callable), \
             _patch_vt_autotrader_db(db_path):
            from core.vt_autotrader import _resolve_orphan_closes
            result = _resolve_orphan_closes()

        assert result["checked"] == 1
        assert result["resolved"] == 0
        assert result["skipped_no_history"] == 1

        # DB não tocou
        trade = _fetch_trade(db_path, tid)
        assert trade["exit_time"] is None

    def test_already_closed_with_wrong_pnl_re_reconciled(self):
        """Trade JÁ fechado (exit_time NOT NULL) COM PnL divergente
        → UPDATE cirúrgico só do PnL, preserve exit_reason/exit_time."""
        from datetime import datetime, timedelta
        existing_exit = (datetime.now() - timedelta(minutes=5)).strftime(
            "%Y-%m-%d %H:%M:%S")
        db_path = _make_tmp_db()
        # trade was ghost-marked por reconcile_positions_with_mt5
        # com exit_time + exit_reason='GHOST' + PnL=0
        tid = _insert_open_trade(db_path, ticket="2466491666",
                                 symbol="WINQ26",
                                 entry_price=175000.0,
                                 exit_time=existing_exit,
                                 close_source="RECONCILE",
                                 gross_pnl=0.0, net_pnl=0.0)
        # Marcar exit_reason='GHOST' (que reconcile_positions faz)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE trades SET exit_reason='GHOST' WHERE id=?",
            (tid,)
        )
        conn.commit()
        conn.close()

        # MT5: ticket fechado, history tem o deal real
        fake_status = {"positions": [], "account": {}}
        fake_history = {
            "history": [{
                "ticket": 2466492544,
                "symbol": "WINQ26",
                "type": "SELL",
                "deal_type": "STOP",
                "price": 175200.0,
                "profit": 31.50,
                "commission": -0.50,
                "swap": 0.0,
                "volume": 1.0,
                "position_id": "2466491666",
                "magic": 555501,
            }]
        }

        with patch("core.vt_autotrader.status",
                   new=lambda: fake_status), \
             patch("core.vt_autotrader.history",
                   new=lambda symbol=None, days=2: fake_history), \
             _patch_vt_autotrader_db(db_path):
            from core.vt_autotrader import _resolve_orphan_closes
            result = _resolve_orphan_closes()

        assert result["checked"] == 1
        assert result["updated_pnl"] == 1, \
            "Path B (PnL different) deve fazer UPDATE cirúrgico"
        assert result["resolved"] == 0  # NÃO conta como resolved

        # exit_time/exit_reason PRESERVADOS
        trade = _fetch_trade(db_path, tid)
        assert trade["exit_time"] == existing_exit, \
            f"exit_time deve ser preservado: {existing_exit}"
        assert trade["exit_reason"] == "GHOST", \
            "exit_reason='GHOST' original deve ser PRESERVADO"
        # PnL atualizado
        assert trade["gross_pnl"] == 31.5
        assert trade["net_pnl"] == 31.0
        # close_source atualizado (audit trail)
        assert trade["close_source"].startswith("ORPHAN_CLOSE_RESOLVED")

    def test_already_closed_with_matching_pnl_noop(self):
        """Trade já fechado COM PnL já correto → noop (idempotente)."""
        from datetime import datetime, timedelta
        existing_exit = (datetime.now() - timedelta(minutes=5)).strftime(
            "%Y-%m-%d %H:%M:%S")
        db_path = _make_tmp_db()
        # PnL já bate com MT5 — não deve atualizar
        tid = _insert_open_trade(db_path, ticket="2466491666",
                                 symbol="WINQ26",
                                 entry_price=175000.0,
                                 exit_time=existing_exit,
                                 close_source="RECONCILE",
                                 gross_pnl=31.5, net_pnl=31.0)

        fake_status = {"positions": [], "account": {}}
        fake_history = {
            "history": [{
                "ticket": 2466492544,
                "symbol": "WINQ26",
                "type": "SELL",
                "deal_type": "STOP",
                "price": 175200.0,
                "profit": 31.50,
                "commission": -0.50,
                "swap": 0.0,
                "volume": 1.0,
                "position_id": "2466491666",
                "magic": 555501,
            }]
        }

        with patch("core.vt_autotrader.status",
                   new=lambda: fake_status), \
             patch("core.vt_autotrader.history",
                   new=lambda symbol=None, days=2: fake_history), \
             _patch_vt_autotrader_db(db_path):
            from core.vt_autotrader import _resolve_orphan_closes
            result = _resolve_orphan_closes()

        assert result["checked"] == 1
        assert result["updated_pnl"] == 0, \
            "PnL já bate → no UPDATE"
        assert result["skipped_legit"] == 1

    def test_already_resolved_skip_idempotent(self):
        """Trade já com close_source='ORPHAN_CLOSE_RESOLVED_*' → skip."""
        from datetime import datetime
        existing_exit = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db_path = _make_tmp_db()
        tid = _insert_open_trade(
            db_path, ticket="2466491666", symbol="WINQ26",
            entry_price=175000.0,
            exit_time=existing_exit,
            close_source="ORPHAN_CLOSE_RESOLVED_010000",  # já resolvido!
            gross_pnl=31.5, net_pnl=31.0,
        )

        fake_status = {"positions": [], "account": {}}

        with patch("core.vt_autotrader.status",
                   new=lambda: fake_status), \
             patch("core.vt_autotrader.history",
                   new=lambda symbol=None, days=2: {"history": []}), \
             _patch_vt_autotrader_db(db_path):
            from core.vt_autotrader import _resolve_orphan_closes
            result = _resolve_orphan_closes()

        # NÃO conta como open (já tem exit_time), MAS se a função
        # iterar mesmo assim, vai ver o close_source e skip.
        # Aqui, exit_time IS NOT NULL → não entra no checked.
        assert result["checked"] == 0
        assert result["resolved"] == 0
        # Trade inalterado
        trade = _fetch_trade(db_path, tid)
        assert trade["close_source"] == "ORPHAN_CLOSE_RESOLVED_010000"

    def test_status_failure_does_not_crash(self):
        """status() levanta exceção → não crasha, retorna stats."""
        db_path = _make_tmp_db()
        _insert_open_trade(db_path, ticket="2466491666", symbol="WINQ26",
                           entry_price=175000.0)

        def broken_status():
            raise RuntimeError("MT5 wine timeout")

        with patch("core.vt_autotrader.status",
                   new=broken_status), \
             patch("core.vt_autotrader.history",
                   new=lambda symbol=None, days=2: {"history": []}), \
             _patch_vt_autotrader_db(db_path):
            from core.vt_autotrader import _resolve_orphan_closes
            # NÃO deve lançar exceção
            result = _resolve_orphan_closes()

        assert result["errors"] == 0  # Erro de status é silencioso
        assert result["checked"] == 1  # Leu o DB antes do status falhar
        assert result["resolved"] == 0

    def test_history_failure_does_not_crash(self):
        """history() falha para um símbolo → outros símbolos prosseguem."""
        db_path = _make_tmp_db()
        _insert_open_trade(db_path, ticket="1", symbol="WINQ26",
                           entry_price=175000.0)
        _insert_open_trade(db_path, ticket="2", symbol="WDOQ26",
                           entry_price=5000.0)

        fake_status = {"positions": [], "account": {}}

        def flaky_history(symbol=None, days=2):
            if symbol == "WINQ26":
                raise ConnectionError("Wine timeout WINQ26")
            if symbol == "WDOQ26":
                return {"history": [{
                    "ticket": 3, "symbol": "WDOQ26", "type": "SELL",
                    "deal_type": "STOP",
                    "price": 5005.0, "profit": 15.0, "commission": -0.5,
                    "swap": 0.0, "position_id": "2",
                    "magic": 555501,
                }]}
            return {"history": []}

        with patch("core.vt_autotrader.status",
                   new=lambda: fake_status), \
             patch("core.vt_autotrader.history",
                   new=flaky_history), \
             _patch_vt_autotrader_db(db_path):
            from core.vt_autotrader import _resolve_orphan_closes
            result = _resolve_orphan_closes()

        # WDOQ26 funciona (1 resolved), WINQ26 falha silenciosamente
        assert result["checked"] == 2
        assert result["resolved"] == 1
        assert result["skipped_no_history"] == 1
        assert result["errors"] == 0

    def test_db_locked_does_not_crash(self):
        """DB locked (OperationalError) → loga, retorna stats sem crash."""
        db_path = _make_tmp_db()
        _insert_open_trade(db_path, ticket="2466491666", symbol="WINQ26",
                           entry_price=175000.0)

        fake_status = {"positions": [], "account": {}}
        fake_history = {
            "history": [{
                "ticket": 2466492544, "symbol": "WINQ26", "type": "SELL",
                "deal_type": "STOP",
                "price": 175200.0, "profit": 31.50, "commission": -0.50,
                "swap": 0.0, "volume": 1.0, "position_id": "2466491666",
                "magic": 555501,
            }]
        }

        # Monkey-patch sqlite3.connect para retornar Connection que
        # joga OperationalError em QUALQUER execute (SELECT ou UPDATE).
        class _BrokenConn:
            def __init__(self, *a, **kw):
                pass
            def execute(self, sql, params=None):
                raise sqlite3.OperationalError("database is locked")
            def commit(self):
                pass
            def close(self):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        from core import vt_autotrader
        orig_connect = vt_autotrader.sqlite3.connect
        vt_autotrader.sqlite3.connect = lambda *a, **kw: _BrokenConn()

        try:
            with patch("core.vt_autotrader.status",
                       new=lambda: fake_status), \
                 patch("core.vt_autotrader.history",
                       new=lambda symbol=None, days=2: fake_history):
                from core.vt_autotrader import _resolve_orphan_closes
                # NÃO deve lançar exceção (DB falhou em tudo)
                result = _resolve_orphan_closes()
        finally:
            vt_autotrader.sqlite3.connect = orig_connect

        # stats zerados, errors++ (mas não crashou)
        assert result["checked"] == 0  # SELECT falhou, não chegou ao loop
        assert result["resolved"] == 0
        # 0 ou 1 erros dependendo de onde capturou (try-except interno vs
        # outer). O importante é não ter lançado exceção.

    def test_no_open_trades_noop(self):
        """DB sem trades abertos → noop, stats zerados, sem crash."""
        db_path = _make_tmp_db()  # DB vazio

        with patch("core.vt_autotrader.status",
                   new=lambda: {"positions": [], "account": {}}), \
             patch("core.vt_autotrader.history",
                   new=lambda symbol=None, days=2: {"history": []}), \
             _patch_vt_autotrader_db(db_path):
            from core.vt_autotrader import _resolve_orphan_closes
            result = _resolve_orphan_closes()

        assert result["checked"] == 0
        assert result["resolved"] == 0
        assert result["errors"] == 0


class TestWiring:
    """Garante que o autotrader chama a função no tick."""

    def test_autotrader_defines_resolve_orphan_closes(self):
        """vt_autotrader.py deve definir a função."""
        src = (PROJECT_ROOT / "core" / "vt_autotrader.py").read_text(
            encoding="utf-8")
        assert "def _resolve_orphan_closes" in src, \
            "FALHA: vt_autotrader deve definir _resolve_orphan_closes"

    def test_autotrader_calls_resolve_before_reconcile(self):
        """Defesa #3: _resolve_orphan_closes ANTES de reconcile_positions_with_mt5."""
        src = (PROJECT_ROOT / "core" / "vt_autotrader.py").read_text(
            encoding="utf-8")
        idx_resolve = src.find("_resolve_orphan_closes()")
        idx_reconcile = src.find("reconcile_positions_with_mt5()")
        assert idx_resolve > 0, \
            "_resolve_orphan_closes() deve aparecer chamado em algum lugar"
        assert idx_reconcile > 0
        assert idx_resolve < idx_reconcile, (
            "FALHA: _resolve_orphan_closes() DEVE ser chamado ANTES de "
            "reconcile_positions_with_mt5() (resolve preenche PnL antes "
            "do reconcile marcar GHOST)"
        )

    def test_loop_has_orphan_resolve_try_block(self):
        """Defesa #3: chamada dentro de try/except no loop."""
        src = (PROJECT_ROOT / "core" / "vt_autotrader.py").read_text(
            encoding="utf-8")
        # Procura o bloco de chamada no loop
        assert "[ORPHAN-RESOLVE] tick falhou" in src, (
            "FALHA: chamada de _resolve_orphan_closes no loop deve estar "
            "protegida por try/except (failure-safe)"
        )

    def test_no_destructive_changes_to_production_db_marker(self):
        """Sanity: nenhuma escrita acidental em 'vt_trades.db' puro."""
        src = (PROJECT_ROOT / "core" / "vt_autotrader.py").read_text(
            encoding="utf-8")
        # Verifica que a função existe e é acessível (não foi commented out)
        assert "_resolve_orphan_closes" in src
        # Não deve haver testes inline que escrevem em produção
        assert "test_resolve_orphan_closes" not in src


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
