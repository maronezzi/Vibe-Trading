"""
TDD — Garante que ``log_exit()`` grava ``close_source`` explícito em todos
os paths de fechamento de posição, conforme taxonomia definida no docstring
de ``core.vt_trade_log.log_exit``.

BUG LATENTE (forense arquitetura_audit seção 9.6, 2026-07-01):
    Trade #2068 fechou com exit_reason='SL_SERVIDOR' e close_source=None
    porque manage_position() em core/vt_autotrader.py chamava log_exit() sem
    o argumento. Sem close_source, queries SQL futuras não conseguiam
    distinguir "fechado pelo bot" de "fechado pelo servidor MT5".

Este test cobre:
  1. log_exit() default é "UNKNOWN" (não None) — para nunca gravar NULL no DB.
  2. log_exit() loga WARNING se chamado sem close_source explícito
     (detector de callsite que esquece o argumento).
  3. Cada call site conhecido de log_exit() em core/vt_autotrader.py PASSA
     close_source correto na chamada (inspeção estática via AST ou runtime).
  4. log_exit() aceita e grava o close_source custom no DB.
  5. close_source canônicos da taxonomia são aceitos e gravados.
"""
import ast
import logging
import sqlite3
import sys
from pathlib import Path

import pytest


# ─── Setup de DB isolado por teste (espelha vt_trade_log.get_db) ──────────

@pytest.fixture
def tmp_trades_db(monkeypatch, tmp_path):
    """Cria DB SQLite temporário com schema mínimo e redireciona get_db()."""
    import core.vt_trade_log as vtl
    tmp_db = tmp_path / "vt_trades.db"
    conn = sqlite3.connect(str(tmp_db), timeout=30.0)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            volume REAL NOT NULL,
            entry_time TEXT NOT NULL,
            entry_price REAL NOT NULL,
            entry_sl REAL,
            exit_time TEXT,
            exit_price REAL,
            exit_reason TEXT,
            exit_ticket TEXT,
            exit_sl_price REAL,
            gross_pnl REAL DEFAULT 0,
            fees REAL DEFAULT 0,
            swap REAL DEFAULT 0,
            net_pnl REAL DEFAULT 0,
            multiplier REAL DEFAULT 0.20,
            strategy TEXT DEFAULT 'TEST',
            notes TEXT,
            close_source TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT DEFAULT (datetime('now', 'localtime'))
        );
        CREATE TABLE IF NOT EXISTS daily_summary (
            date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            n_trades INTEGER DEFAULT 0,
            n_winners INTEGER DEFAULT 0,
            n_losers INTEGER DEFAULT 0,
            gross_pnl REAL DEFAULT 0,
            fees REAL DEFAULT 0,
            net_pnl REAL DEFAULT 0,
            max_win REAL DEFAULT 0,
            max_loss REAL DEFAULT 0,
            PRIMARY KEY (date, symbol)
        );
    """)
    conn.commit()
    conn.close()

    # Monkeypatch get_db() para usar tmp_db (não polui vt_trades.db produção)
    def _fake_get_db():
        c = sqlite3.connect(str(tmp_db), timeout=30.0)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(vtl, "get_db", _fake_get_db)
    return tmp_db


def _open_trade(vtl, symbol="WINQ26", direction="BUY", entry_price=120000.0):
    """Helper: abre trade para teste."""
    return vtl.log_entry(
        symbol=symbol, direction=direction, volume=1.0,
        entry_price=entry_price, entry_sl=entry_price - 100.0,
        strategy="TEST", entry_ticket="12345",
    )


# ─── 1. Default "UNKNOWN" + warning quando caller esquece ────────────────

class TestLogExitDefaultAndWarning:
    """Garante que log_exit() nunca grava NULL no DB e detecta callers
    que esquecem de passar close_source."""

    def test_default_close_source_is_unknown_not_none(self, tmp_trades_db):
        """Default DEVE ser 'UNKNOWN' (string), não None (era o bug latente)."""
        import inspect
        from core.vt_trade_log import log_exit
        sig = inspect.signature(log_exit)
        default = sig.parameters["close_source"].default
        assert default == "UNKNOWN", (
            f"Default de close_source deveria ser 'UNKNOWN', é {default!r}. "
            f"Se for None, o bug latente (forense 9.6) pode voltar."
        )

    def test_log_exit_warns_when_close_source_not_passed(
        self, tmp_trades_db, caplog
    ):
        """Calls sem close_source explícito DEVEM logar WARNING (detector)."""
        import core.vt_trade_log as vtl
        trade_id = _open_trade(vtl)

        with caplog.at_level(logging.WARNING, logger="vt_trade_log"):
            vtl.log_exit(
                trade_id=trade_id,
                exit_price=120100.0,
                exit_reason="TEST_FORGOT_CLOSE_SOURCE",
            )

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) >= 1, (
            "log_exit() deveria logar WARNING quando chamado sem "
            "close_source explícito (default 'UNKNOWN')."
        )
        assert "close_source" in warnings[0].getMessage().lower()

    def test_log_exit_does_not_warn_when_close_source_passed(
        self, tmp_trades_db, caplog
    ):
        """Calls COM close_source explícito NÃO devem logar warning."""
        import core.vt_trade_log as vtl
        trade_id = _open_trade(vtl)

        with caplog.at_level(logging.WARNING, logger="vt_trade_log"):
            vtl.log_exit(
                trade_id=trade_id,
                exit_price=120100.0,
                exit_reason="TRAILING",
                close_source="TRAIL_STOP",
            )

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 0, (
            f"log_exit() NÃO deveria logar WARNING quando close_source "
            f"explícito é passado. Recebido: {[r.getMessage() for r in warnings]}"
        )


# ─── 2. log_exit grava close_source no DB ────────────────────────────────

class TestLogExitPersistsCloseSource:
    """Garante que close_source chega na coluna do DB."""

    @pytest.mark.parametrize("close_source", [
        "MT5_SERVER_SL",
        "TRAIL_STOP",
        "TIME_TRAIL",
        "EMERGENCY_CLOSE",
        "USER_CLOSE",
        "EOD_CLOSE",
        "HARD_EXIT",
        "BREAKEVEN",
        "RECONCILE",
        "ORPHAN_CLOSE_RESOLVED_143000",
        "mt5_orchestrator_close",
    ])
    def test_log_exit_persists_close_source_canonical(
        self, tmp_trades_db, close_source
    ):
        """Cada valor canônico da taxonomia é gravado no DB."""
        import core.vt_trade_log as vtl
        trade_id = _open_trade(vtl, symbol=f"T{close_source[:6]}")
        vtl.log_exit(
            trade_id=trade_id,
            exit_price=120050.0,
            exit_reason="TEST",
            close_source=close_source,
        )
        conn = sqlite3.connect(str(tmp_trades_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT close_source FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
        conn.close()
        assert row["close_source"] == close_source

    def test_log_exit_persists_unknown_when_default_used(self, tmp_trades_db):
        """Default 'UNKNOWN' é gravado (não None) — Defesa contra regressão."""
        import core.vt_trade_log as vtl
        trade_id = _open_trade(vtl, symbol="UNKN")
        vtl.log_exit(
            trade_id=trade_id,
            exit_price=120050.0,
            exit_reason="TEST",
        )
        conn = sqlite3.connect(str(tmp_trades_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT close_source FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
        conn.close()
        assert row["close_source"] == "UNKNOWN"
        assert row["close_source"] is not None, (
            "DB NUNCA deve ter close_source=NULL — esse é o bug latente 9.6."
        )


# ─── 3. Inspeção estática dos call sites em vt_autotrader.py ─────────────

class TestCallSitesPassCloseSource:
    """Inspeção AST de core/vt_autotrader.py: cada chamada de log_exit() deve
    passar close_source= como keyword argument. Pega regressão no nível de
    código-fonte (não só runtime)."""

    AUTOTRADER = Path(__file__).resolve().parent.parent / "core" / "vt_autotrader.py"

    def _find_log_exit_calls(self):
        """Retorna lista de Call nodes onde func é log_exit."""
        src = self.AUTOTRADER.read_text(encoding="utf-8")
        tree = ast.parse(src)
        calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = None
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                if func_name == "log_exit":
                    calls.append(node)
        return calls

    def test_autotrader_has_log_exit_calls(self):
        """Sanity: deve haver pelo menos 2 calls (SL_SERVIDOR + EOD_16:45)."""
        calls = self._find_log_exit_calls()
        assert len(calls) >= 2, (
            f"Esperado >= 2 calls de log_exit em vt_autotrader.py, "
            f"encontrado {len(calls)}."
        )

    def test_every_log_exit_call_passes_close_source_kwarg(self):
        """Cada call de log_exit() em vt_autotrader.py DEVE ter
        keyword argument close_source=<valor-explicito>."""
        calls = self._find_log_exit_calls()
        offenders = []
        for call in calls:
            kwargs = {kw.arg for kw in call.keywords}
            if "close_source" not in kwargs:
                offenders.append(call.lineno)

        assert not offenders, (
            f"Calls de log_exit() em vt_autotrader.py SEM close_source kwarg "
            f"nas linhas {offenders}. Isso é o bug latente 9.6 — cada call "
            f"deve passar close_source= explicitamente."
        )


# ─── 4. Bug latente 9.6 — cenário end-to-end ──────────────────────────────

class TestBugLatente9_6:
    """Replica o cenário do trade #2068: posição aberta, MT5 fecha sozinho
    (detectado em manage_position), log_exit() deve gravar close_source."""

    def test_trade_2068_scenario_sl_servidor(self, tmp_trades_db, caplog):
        """Simula o que aconteceu com trade #2068 e garante que agora
        close_source='MT5_SERVER_SL' chega no DB."""
        import core.vt_trade_log as vtl
        trade_id = _open_trade(vtl, symbol="WSPU26", direction="BUY",
                               entry_price=100.0)
        # Sem caplog warning (close_source explícito)
        with caplog.at_level(logging.WARNING, logger="vt_trade_log"):
            result = vtl.log_exit(
                trade_id=trade_id,
                exit_price=99.5,
                exit_reason="SL_SERVIDOR",
                exit_ticket="server",
                exit_sl_price=98.0,
                notes="FECHADO PELO SERVIDOR | PnL real: R$-1.44",
                close_source="MT5_SERVER_SL",
            )
        assert result is not None
        assert result["net_pnl"] < 0  # loss

        # DB deve ter close_source correto
        conn = sqlite3.connect(str(tmp_trades_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT close_source, exit_reason, net_pnl FROM trades WHERE id = ?",
            (trade_id,),
        ).fetchone()
        conn.close()
        assert row["close_source"] == "MT5_SERVER_SL", (
            f"close_source deveria ser 'MT5_SERVER_SL', é {row['close_source']!r}. "
            f"Esse é o bug latente 9.6: trade #2068 gravou None."
        )
        assert row["exit_reason"] == "SL_SERVIDOR"

        # E nenhum warning foi emitido (close_source foi explícito)
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 0

    def test_eod_close_path_passes_explicit_source(self, tmp_trades_db):
        """Cenário close_all_and_report(): log_exit() com EOD_16:45
        deve gravar close_source='EOD_CLOSE'."""
        import core.vt_trade_log as vtl
        trade_id = _open_trade(vtl, symbol="WDOQ26", direction="SELL",
                               entry_price=5000.0)
        vtl.log_exit(
            trade_id=trade_id,
            exit_price=5005.0,
            exit_reason="EOD_16:45",
            exit_ticket="eod",
            notes="Fechamento obrigatório de intraday",
            close_source="EOD_CLOSE",
        )
        conn = sqlite3.connect(str(tmp_trades_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT close_source, exit_reason FROM trades WHERE id = ?",
            (trade_id,),
        ).fetchone()
        conn.close()
        assert row["close_source"] == "EOD_CLOSE"
        assert row["exit_reason"] == "EOD_16:45"


# ─── 5. Taxonomia documentada — valores aceitos ──────────────────────────

class TestTaxonomyDocumentation:
    """Sanity: a taxonomia do docstring bate com o que o sistema usa."""

    def test_taxonomy_includes_mt5_server_sl(self):
        """Taxonomia deve incluir MT5_SERVER_SL (cobre forense 9.6)."""
        from core.vt_trade_log import log_exit
        doc = log_exit.__doc__ or ""
        assert "MT5_SERVER_SL" in doc

    def test_taxonomy_includes_emergency_close(self):
        from core.vt_trade_log import log_exit
        doc = log_exit.__doc__ or ""
        assert "EMERGENCY_CLOSE" in doc

    def test_taxonomy_includes_eod_close(self):
        from core.vt_trade_log import log_exit
        doc = log_exit.__doc__ or ""
        assert "EOD_CLOSE" in doc

    def test_taxonomy_includes_reconcile(self):
        from core.vt_trade_log import log_exit
        doc = log_exit.__doc__ or ""
        assert "RECONCILE" in doc

    def test_taxonomy_includes_orphan_resolve(self):
        from core.vt_trade_log import log_exit
        doc = log_exit.__doc__ or ""
        assert "ORPHAN_CLOSE_RESOLVED" in doc

    def test_docstring_references_audit_9_6(self):
        """Docstring deve referenciar a forense 9.6 para contexto histórico."""
        from core.vt_trade_log import log_exit
        doc = log_exit.__doc__ or ""
        assert "9.6" in doc
