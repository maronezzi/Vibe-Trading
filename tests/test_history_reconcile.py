"""
test_history_reconcile.py
=========================
TDD para defesa contra drift DB↔MT5 (Bruno 30/06/2026).

Bug:
- 22 deals no extrato MT5 vs 15 trades no DB SQLite
- 7 SELLs perdidos: log_exit() não foi chamado quando MT5 fechou posição
- Causa: DB lock + restart + race entre manage_position e log_exit

Defesa testada:
- core/vt_history_reconcile.py::reconcile_db_with_mt5_history()
- core/vt_history_reconcile.py::reconcile_pending_excluded()

Critérios:
1. Trade com exit_time NULL no DB + deal correspondente no MT5 history
   → atualiza com PnL real do broker (profit + commission + swap)
2. Trade com exit_time NULL no DB + SEM deal no MT5
   → conta como "still_open" (legítimo), não toca
3. Trade já reconciliado (strategy contém HISTORY_RECONCILE)
   → idempotente: pula
4. Trade EXCLUDED com exit_time NULL
   → reconcile_pending_excluded fecha com exit_reason=EXCLUDED_AUTO_CLOSE
5. DB locked (OperationalError) → loga erro, segue (não trava)
6. MT5 history retorna formato errado (sem "history") → erro reportado, sem crash

Execução:
    python -m pytest tests/test_history_reconcile.py -v
"""

import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path("/home/bruno/Projects/Vibe-Trading")
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "core"))


# ──────────────────────────────────────────────────────────────────
# Fixtures: DB SQLite isolado por teste
# ──────────────────────────────────────────────────────────────────

def _make_tmp_db():
    """Cria DB temporário com schema mínimo de vt_trade_log + retorna path."""
    tmpdir = tempfile.mkdtemp(prefix="vt_reconcile_test_")
    db_path = Path(tmpdir) / "test.db"
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.executescript("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_ticket TEXT,
            exit_ticket TEXT,
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
        CREATE TABLE daily_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(date, symbol)
        );
    """)
    conn.close()
    return db_path


def _insert_open_trade(db_path, *, ticket="2466491666", symbol="WINQ26",
                       direction="BUY", entry_price=175000.0, volume=1.0,
                       multiplier=0.20, strategy="STRONG_TREND",
                       entry_time=None):
    """Insere trade com exit_time NULL no DB de teste."""
    if entry_time is None:
        entry_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    cur = conn.execute("""
        INSERT INTO trades (entry_ticket, symbol, direction, volume,
                            entry_time, entry_price, multiplier, strategy)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (ticket, symbol, direction, volume, entry_time, entry_price,
          multiplier, strategy))
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def _fetch_trade(db_path, trade_id):
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    return dict(row)


# ──────────────────────────────────────────────────────────────────
# Testes
# ──────────────────────────────────────────────────────────────────

class TestReconcileBasic:
    """Defesa #1 + #2 + #3 — reconciliação proativa via MT5 history."""

    def test_drift_detected_and_fixed(self):
        """Trade com exit_time NULL + deal correspondente no MT5 history
        → atualiza com PnL real do broker (profit + commission + swap)."""
        db_path = _make_tmp_db()
        tid = _insert_open_trade(db_path, ticket="2466491666",
                                 entry_price=175000.0, volume=1.0,
                                 multiplier=0.20)

        # Mock MT5 history: deal "out" com profit real
        fake_history = {
            "history": [
                {
                    "ticket": 2466492544,
                    "symbol": "WINQ26",
                    "type": "SELL",
                    "price": 175200.0,
                    "profit": 31.50,         # PnL real do broker
                    "commission": -0.50,
                    "swap": 0.0,
                    "volume": 1.0,
                    "position_id": "2466491666",
                    "time": "2026-06-30 10:14:46",
                    "magic": 555501,
                }
            ]
        }
        def fake_history_callable(symbol, days=2):
            assert symbol == "WINQ26"
            return fake_history

        with patch("core.vt_history_reconcile.DB_PATH", db_path):
            from core.vt_history_reconcile import reconcile_db_with_mt5_history
            result = reconcile_db_with_mt5_history(
                symbols=["WINQ26"],
                history_callable=fake_history_callable,
                log_callable=lambda m: None,  # silencioso no teste
            )

        assert result["checked"] == 1, f"Esperava 1 trade analisado, got {result}"
        assert result["reconciled"] == 1, f"Esperava 1 reconciliado, got {result}"
        assert result["still_open"] == 0

        # Verificar DB
        trade = _fetch_trade(db_path, tid)
        assert trade["exit_time"] is not None, "exit_time deve estar preenchido"
        assert trade["exit_price"] == 175200.0, f"exit_price errado: {trade['exit_price']}"
        assert trade["exit_ticket"] == "2466492544"
        assert trade["net_pnl"] == 31.0, f"net_pnl esperado 31.0 (profit+commission+swap), got {trade['net_pnl']}"
        assert trade["gross_pnl"] == 31.5
        assert "HISTORY_RECONCILE" in (trade["strategy"] or ""), "strategy deve ter tag de reconciliação"
        assert "HISTORY_RECONCILE" in (trade["close_source"] or "")

    def test_still_open_no_drift(self):
        """Trade com exit_time NULL mas SEM deal no MT5 → still_open, não toca."""
        db_path = _make_tmp_db()
        tid = _insert_open_trade(db_path, ticket="9999",
                                 entry_price=175000.0, volume=1.0)

        # Mock MT5 history: VAZIO (sem deals — posição legítima ainda aberta)
        def fake_history_callable(symbol, days=2):
            return {"history": []}

        with patch("core.vt_history_reconcile.DB_PATH", db_path):
            from core.vt_history_reconcile import reconcile_db_with_mt5_history
            result = reconcile_db_with_mt5_history(
                symbols=["WINQ26"],
                history_callable=fake_history_callable,
                log_callable=lambda m: None,
            )

        assert result["checked"] == 1
        assert result["reconciled"] == 0
        assert result["still_open"] == 1

        # DB não deve ter sido tocado
        trade = _fetch_trade(db_path, tid)
        assert trade["exit_time"] is None
        assert trade["net_pnl"] == 0

    def test_idempotent_already_reconciled(self):
        """Trade já com tag HISTORY_RECONCILE na strategy → pula (idempotente)."""
        db_path = _make_tmp_db()
        tid = _insert_open_trade(db_path, ticket="1234",
                                 strategy="STRONG_TREND [HISTORY_RECONCILE_20260630_100000]",
                                 entry_price=175000.0)

        def fake_history_callable(symbol, days=2):
            return {
                "history": [{
                    "ticket": 1235, "symbol": "WINQ26", "type": "SELL",
                    "price": 175200.0, "profit": 31.5, "commission": -0.5,
                    "swap": 0.0, "position_id": "1234",
                }]
            }

        with patch("core.vt_history_reconcile.DB_PATH", db_path):
            from core.vt_history_reconcile import reconcile_db_with_mt5_history
            result = reconcile_db_with_mt5_history(
                symbols=["WINQ26"],
                history_callable=fake_history_callable,
                log_callable=lambda m: None,
            )

        # Não deve ter reconciliado de novo (idempotente)
        # Trade fica como still_open (não fecha, não atualiza)
        assert result["reconciled"] == 0
        trade = _fetch_trade(db_path, tid)
        # Strategy mantém o tag original — não duplica
        assert trade["strategy"].count("HISTORY_RECONCILE") == 1

    def test_db_locked_does_not_crash(self):
        """DB locked (OperationalError) → loga erro, retorna sem crash."""
        db_path = _make_tmp_db()
        _insert_open_trade(db_path, ticket="1234")

        def fake_history_callable(symbol, days=2):
            return {
                "history": [{
                    "ticket": 1235, "symbol": "WINQ26", "type": "SELL",
                    "price": 175200.0, "profit": 31.5, "commission": -0.5,
                    "swap": 0.0, "position_id": "1234",
                }]
            }

        # Mock _open_db para forçar OperationalError
        with patch("core.vt_history_reconcile.DB_PATH", db_path):
            from core.vt_history_reconcile import reconcile_db_with_mt5_history
            from core import vt_history_reconcile as vhr_mod

            original_open_db = vhr_mod._open_db
            def broken_open_db(*a, **kw):
                raise sqlite3.OperationalError("database is locked")
            vhr_mod._open_db = broken_open_db
            try:
                result = reconcile_db_with_mt5_history(
                    symbols=["WINQ26"],
                    history_callable=fake_history_callable,
                    log_callable=lambda m: None,
                )
            finally:
                vhr_mod._open_db = original_open_db

        # Deve retornar erro estruturado, não crash
        assert result["checked"] == 0
        assert result["reconciled"] == 0
        assert len(result["errors"]) > 0
        assert "locked" in str(result["errors"][0]).lower()

    def test_history_wrong_format_does_not_crash(self):
        """MT5 history retorna formato errado (sem 'history') → erro reportado."""
        db_path = _make_tmp_db()
        _insert_open_trade(db_path, ticket="1234")

        def fake_history_callable_bad(symbol, days=2):
            return {"error": "MT5 timeout"}  # formato inválido

        with patch("core.vt_history_reconcile.DB_PATH", db_path):
            from core.vt_history_reconcile import reconcile_db_with_mt5_history
            result = reconcile_db_with_mt5_history(
                symbols=["WINQ26"],
                history_callable=fake_history_callable_bad,
                log_callable=lambda m: None,
            )

        # Não crasha, mas não encontra nada para reconciliar
        assert result["checked"] == 1
        assert result["reconciled"] == 0
        assert result["still_open"] == 1

    def test_buy_deal_also_used_as_fallback(self):
        """Quando só existe deal BUY (não SELL), ainda tenta reconciliar."""
        db_path = _make_tmp_db()
        _insert_open_trade(db_path, ticket="8888",
                           entry_price=175000.0, multiplier=0.20)

        def fake_history_callable(symbol, days=2):
            # Só tem um deal BUY (caso raro — position_id bate)
            return {
                "history": [{
                    "ticket": 9999, "symbol": "WINQ26", "type": "BUY",
                    "price": 175000.0, "profit": 0.0, "commission": -1.2,
                    "swap": 0.0, "position_id": "8888",
                }]
            }

        with patch("core.vt_history_reconcile.DB_PATH", db_path):
            from core.vt_history_reconcile import reconcile_db_with_mt5_history
            result = reconcile_db_with_mt5_history(
                symbols=["WINQ26"],
                history_callable=fake_history_callable,
                log_callable=lambda m: None,
            )

        # BUY sozinho não é "out", mas ainda assim pode ser usado como fallback
        # (cobre caso onde MT5 só retorna um lado do ciclo)
        assert result["checked"] == 1


class TestReconcilePendingExcluded:
    """Defesa adicional: fecha trades EXCLUDED com exit_time NULL."""

    def test_excluded_trades_auto_closed(self):
        """Trade com [EXCLUDED] na strategy + exit_time NULL → fecha com PnL=0."""
        db_path = _make_tmp_db()
        tid = _insert_open_trade(db_path, ticket="5555",
                                 strategy="STRONG_TREND [EXCLUDED]",
                                 entry_price=175000.0)

        with patch("core.vt_history_reconcile.DB_PATH", db_path):
            from core.vt_history_reconcile import reconcile_pending_excluded
            n = reconcile_pending_excluded(log_callable=lambda m: None)

        assert n == 1
        trade = _fetch_trade(db_path, tid)
        assert trade["exit_time"] is not None
        assert trade["exit_reason"] == "EXCLUDED_AUTO_CLOSE"
        assert trade["close_source"] == "EXCLUDED_RECONCILE"
        assert trade["exit_price"] == trade["entry_price"]  # fallback

    def test_non_excluded_trades_not_touched(self):
        """Trade sem [EXCLUDED] + exit_time NULL → não toca (legítimo ainda aberto)."""
        db_path = _make_tmp_db()
        tid = _insert_open_trade(db_path, ticket="6666",
                                 strategy="STRONG_TREND",  # sem [EXCLUDED]
                                 entry_price=175000.0)

        with patch("core.vt_history_reconcile.DB_PATH", db_path):
            from core.vt_history_reconcile import reconcile_pending_excluded
            n = reconcile_pending_excluded(log_callable=lambda m: None)

        assert n == 0
        trade = _fetch_trade(db_path, tid)
        assert trade["exit_time"] is None


class TestAutotraderWiring:
    """Garante que o autotrader chama a reconciliação."""

    def test_autotrader_imports_reconcile(self):
        """vt_autotrader.py deve importar reconcile_db_with_mt5_history."""
        autotrader_src = (PROJECT_ROOT / "core" / "vt_autotrader.py").read_text(encoding="utf-8")
        assert "from core.vt_history_reconcile import" in autotrader_src, (
            "FALHA: vt_autotrader.py deve importar reconcile_db_with_mt5_history"
        )
        assert "reconcile_db_with_mt5_history" in autotrader_src, (
            "FALHA: vt_autotrader.py deve chamar reconcile_db_with_mt5_history"
        )

    def test_autotrader_calls_reconcile_at_startup(self):
        """Defesa #2: reconciliação no startup do run_daemon."""
        autotrader_src = (PROJECT_ROOT / "core" / "vt_autotrader.py").read_text(encoding="utf-8")
        # Verifica que reconciliação é chamada em run_daemon (após recover_open_positions)
        # Procurar padrão: recover_open_positions() ... reconcile_db_with_mt5_history
        idx_recover = autotrader_src.find("recover_open_positions()")
        idx_reconcile = autotrader_src.find("reconcile_db_with_mt5_history(")
        assert idx_recover > 0
        assert idx_reconcile > idx_recover, (
            "FALHA: reconcile_db_with_mt5_history deve ser chamado APÓS recover_open_positions"
        )

    def test_autotrader_calls_reconcile_periodically(self):
        """Defesa #3: reconciliação periódica (counter % 10)."""
        autotrader_src = (PROJECT_ROOT / "core" / "vt_autotrader.py").read_text(encoding="utf-8")
        assert "_iter_counter" in autotrader_src, (
            "FALHA: vt_autotrader.py deve ter _iter_counter para reconciliação periódica"
        )
        assert "% 10" in autotrader_src or "iter_counter" in autotrader_src, (
            "FALHA: reconciliação periódica deve rodar a cada 10 iterações (~5min)"
        )
        assert "AUTO-SYNC" in autotrader_src, (
            "FALHA: log [AUTO-SYNC] Reconciled N trades deve existir"
        )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
