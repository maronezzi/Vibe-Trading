"""
test_watchdog_excludes_excluded_trades.py
=========================================
Wave 875 (Bruno 10/07) — REFACTOR: tests usam DB isolado (tmp_path via
conftest._isolate_trades_db) em vez de depender de poluição no DB real.

ANTES (Wave 1C.1, 2026-07-02): tests conectavam ao vt_trades.db de produção
e exigiam 28 trades com ticket=12345/99999 marcados [EXCLUDED_TEST_2026_07_02]
existirem lá. Isso era uma bomba-relógio — qualquer cleanup manual dos
trades-teste quebrava os 5 testes. Foi o que aconteceu em 10/07 quando
os trades órfãos foram limpos (close_source=ORPHAN_MANUAL_CLEANUP_2026-07-10).

AGORA: cada test recebe tmp_path (pytest) e cria seu proprio DB isolado
com 28 trades [EXCLUDED_TEST_2026_07_02] + 3 trades live. A fixture
autouse _isolate_trades_db (conftest.py) patcha watchdog.DB_PATH para o
mesmo tmp_db. Tests fazem behavioral verification (chamam
get_db_open_trades() e check_trade_log() de verdade) em vez de só
inspecionar source.

Pitfall #12 no-vt-config-write-safety: garantir que o watchdog NAO alarma
sobre esses trades como "fantasmas".

Tests:
- test_watchdog_filters_excluded_from_db_query: filtra DB query (source)
- test_watchdog_filters_excluded_from_get_db_open_trades: filtra get_db_open_trades (source)
- test_watchdog_filters_excluded_from_diff_query: filtra check_trade_log (source)
- test_get_db_open_trades_excludes_excluded: BEHAVIORAL — funcao retorna so live
- test_check_trade_log_excludes_excluded: BEHAVIORAL — diff nao inclui [EXCLUDED]
- test_fixture_has_28_excluded_trades: sanity check da fixture
"""
import os

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"


def _seed_trades(db_path, n_excluded=28, n_live=3):
    """Insere n_excluded trades [EXCLUDED_TEST] + n_live live no DB isolado."""
    import sqlite3
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.executescript(
        """
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
        """
    )
    for i in range(n_excluded):
        ticket = "12345" if i % 2 == 0 else "99999"
        conn.execute(
            "INSERT INTO trades "
            "(entry_ticket, symbol, direction, volume, entry_time, entry_price, "
            " exit_time, strategy, notes, close_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ticket,
                "WINQ26",
                "BUY",
                1.0,
                "2026-07-02 09:00:00",
                175000.0,
                None,
                "VWAP [EXCLUDED_TEST_2026_07_02]",
                f"Wave 1C.1 fixture (id={i})",
                "TEST_FIXTURE",
            ),
        )
    for i in range(n_live):
        conn.execute(
            "INSERT INTO trades "
            "(entry_ticket, symbol, direction, volume, entry_time, entry_price, "
            " exit_time, strategy, notes, close_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"{2000000000 + i}",
                "WINQ26",
                "SELL",
                1.0,
                "2026-07-02 09:01:00",
                175500.0,
                None,
                "BOLLINGER",
                f"live trade {i}",
                None,
            ),
        )
    conn.commit()
    conn.close()


# ─── SOURCE INSPECTION (anti-regressão: regex no source) ────────────────────


def test_watchdog_filters_excluded_from_db_query():
    """Query SELECT ... FROM trades em vt_trade_watchdog.py DEVE filtrar [EXCLUDED]."""
    import re
    src_path = os.path.join(PROJECT_ROOT, "monitoring", "vt_trade_watchdog.py")
    src = open(src_path).read()
    bad_pattern = re.compile(
        r"FROM trades WHERE\s*\(exit_time IS NULL OR exit_time = .*?\)",
        re.DOTALL,
    )
    matches = list(bad_pattern.finditer(src))
    assert len(matches) >= 1, "Query SELECT ... FROM trades WHERE (exit_time IS NULL ...) nao encontrada"
    for m in matches:
        region = src[m.start():m.start() + 600]
        assert "[EXCLUDED" in region, (
            f"Query em offset {m.start()} SEM filtro [EXCLUDED]. "
            "Pitfall #12: trades [EXCLUDED] serao alarmados como fantasmas. "
            "Fix: adicionar 'AND (strategy IS NULL OR INSTR(strategy, '[EXCLUDED') = 0)'"
        )


def test_watchdog_filters_excluded_from_get_db_open_trades():
    """get_db_open_trades() em vt_trade_watchdog.py DEVE filtrar [EXCLUDED]."""
    src_path = os.path.join(PROJECT_ROOT, "monitoring", "vt_trade_watchdog.py")
    src = open(src_path).read()
    func_start = src.find("def get_db_open_trades")
    assert func_start > 0, "Funcao get_db_open_trades() nao encontrada"
    body = src[func_start:func_start + 1500]
    assert "[EXCLUDED" in body, (
        "get_db_open_trades() SEM filtro [EXCLUDED]! "
        "Trades [EXCLUDED] serao alarmados como fantasmas."
    )


def test_watchdog_filters_excluded_from_diff_query():
    """check_trade_log() em vt_trade_watchdog.py DEVE filtrar [EXCLUDED]."""
    src_path = os.path.join(PROJECT_ROOT, "monitoring", "vt_trade_watchdog.py")
    src = open(src_path).read()
    func_start = src.find("def check_trade_log")
    assert func_start > 0, "Funcao check_trade_log() nao encontrada"
    body = src[func_start:func_start + 2000]
    assert "[EXCLUDED" in body, (
        "check_trade_log() SEM filtro [EXCLUDED]! "
        "Pitfall #12: trades [EXCLUDED] serao diff_reportados como fantasmas."
    )


# ─── BEHAVIORAL (chamam a função de verdade) ────────────────────────────────


def test_get_db_open_trades_excludes_excluded(tmp_path):
    """get_db_open_trades() retorna APENAS live, nunca [EXCLUDED]."""
    from monitoring import vt_trade_watchdog

    # A fixture autouse _isolate_trades_db ja fez monkeypatch do watchdog.DB_PATH
    # para tmp_db = tmp_path / "vt_trades.db" (mesmo path que tmp_path fixture).
    db_path = tmp_path / "vt_trades.db"
    _seed_trades(db_path, n_excluded=28, n_live=3)

    result = vt_trade_watchdog.get_db_open_trades()

    assert len(result) == 3, (
        f"get_db_open_trades() retornou {len(result)} trades; esperado 3 (so live). "
        "Filtro [EXCLUDED] nao esta funcionando."
    )
    for ticket in result.keys():
        assert ticket not in ("12345", "99999"), (
            f"Ticket EXCLUDED {ticket} vazou para o resultado do watchdog."
        )
    for i in range(3):
        assert str(2000000000 + i) in result, (
            f"Trade live {i} (ticket={2000000000 + i}) nao encontrado no resultado."
        )


def test_check_trade_log_excludes_excluded(tmp_path):
    """check_trade_log(mt5_positions) NAO inclui [EXCLUDED] no diff."""
    from monitoring import vt_trade_watchdog

    db_path = tmp_path / "vt_trades.db"
    _seed_trades(db_path, n_excluded=28, n_live=3)

    # MT5 nao conhece nenhum dos 31 trades (estado "todos fantasma no broker").
    # Sem o filtro do watchdog, o diff retornaria 31 (todos como "ghost").
    # Com o filtro, retorna apenas os 3 live.
    mt5_positions = []

    diffs = vt_trade_watchdog.check_trade_log(mt5_positions)

    assert len(diffs) == 3, (
        f"check_trade_log() retornou {len(diffs)} diffs; esperado 3 (so live). "
        "Filtro [EXCLUDED] nao esta sendo aplicado na query do diff."
    )
    for d in diffs:
        ticket = str(d.get("entry_ticket", ""))
        assert ticket not in ("12345", "99999"), (
            f"Ticket EXCLUDED {ticket} vazou no diff de check_trade_log()."
        )


# ─── FIXTURE SELF-CHECK ─────────────────────────────────────────────────────


def test_fixture_has_28_excluded_trades(tmp_path):
    """Pre-condicao: a fixture cria 28 trades marcadas [EXCLUDED_TEST]."""
    import sqlite3

    db_path = tmp_path / "vt_trades.db"
    _seed_trades(db_path, n_excluded=28, n_live=3)

    conn = sqlite3.connect(str(db_path), timeout=5)
    n_excluded = conn.execute(
        "SELECT COUNT(*) FROM trades "
        "WHERE (entry_ticket IN ('12345','99999') OR exit_ticket IN ('12345','99999')) "
        "AND INSTR(strategy, '[EXCLUDED') > 0"
    ).fetchone()[0]
    n_live = conn.execute(
        "SELECT COUNT(*) FROM trades "
        "WHERE entry_ticket IN ('2000000000', '2000000001', '2000000002')"
    ).fetchone()[0]
    conn.close()

    assert n_excluded == 28, f"Fixture: esperado 28 [EXCLUDED], achou {n_excluded}"
    assert n_live == 3, f"Fixture: esperado 3 live, achou {n_live}"
