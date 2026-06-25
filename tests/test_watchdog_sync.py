"""
Testes do watchdog de sincronização MT5 ↔ DB.
Cobre 3 cenários de drift + migração de schema.
"""
import sys
import os
import sqlite3
import tempfile
import json
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/home/bruno/Projects/Vibe-Trading")

# Redirecionar DB_PATH para um tmpdb antes de importar
import core.vt_watchdog as wd


def make_tmp_db():
    """Cria DB temporário com schema de trades (não lê DB binário)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript("""
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_ticket TEXT,
    exit_ticket TEXT,
    magic_number INTEGER DEFAULT 555501,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('BUY', 'SELL')),
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
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);
""")
    conn.commit()
    conn.close()
    return path


def test_migrate_close_source_adds_column():
    """Cenário: primeira execução. Coluna close_source deve ser adicionada."""
    path = make_tmp_db()
    conn = sqlite3.connect(path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()]
    assert "close_source" not in cols, "coluna não deve existir antes da migração"

    added = wd.migrate_close_source(conn)
    assert added is True, "primeira execução deve retornar True"

    cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()]
    assert "close_source" in cols, "coluna deve existir após migração"
    conn.close()
    os.unlink(path)


def test_migrate_close_source_is_idempotent():
    """Cenário: rodar migrate 2x. Segunda não pode falhar."""
    path = make_tmp_db()
    conn = sqlite3.connect(path)
    first = wd.migrate_close_source(conn)
    second = wd.migrate_close_source(conn)
    assert first is True
    assert second is False, "segunda execução deve ser no-op"
    conn.close()
    os.unlink(path)


def test_reconcile_inserts_position_missing_from_db():
    """Cenário A: MT5 tem posição, DB não tem. Deve inserir."""
    path = make_tmp_db()
    wd.DB_PATH = Path(path)

    mt5_positions = [{
        "ticket": 2462062917,
        "symbol": "BITM26",
        "type": "BUY",
        "volume": 1.0,
        "price_open": 323080.0,
        "price_current": 322920.0,
        "sl": 322440.0,
        "tp": 0.0,
        "profit": -1.6,
        "time": int(datetime.now().timestamp()),
        "comment": "VibeTrading",
    }]

    result = wd.reconcile_with_mt5(mt5_positions)

    assert len(result["inserted"]) == 1, f"deveria inserir 1: {result}"
    ins = result["inserted"][0]
    assert ins["ticket"] == "2462062917"
    assert ins["symbol"] == "BITM26"
    assert ins["direction"] == "BUY"

    # Verificar que foi persistido
    conn = sqlite3.connect(path)
    rows = conn.execute("SELECT * FROM trades WHERE entry_ticket = '2462062917'").fetchall()
    assert len(rows) == 1
    assert rows[0][4] == "BITM26"  # symbol
    assert rows[0][5] == "BUY"     # direction
    conn.close()
    os.unlink(path)


def test_reconcile_closes_position_missing_from_mt5():
    """Cenário B: DB tem posição aberta, MT5 não tem. Deve fechar com PnL=0."""
    path = make_tmp_db()
    wd.DB_PATH = Path(path)

    # Inserir posição aberta no DB
    conn = sqlite3.connect(path)
    conn.execute("""
        INSERT INTO trades (symbol, direction, volume, entry_time, entry_price,
                          entry_ticket, timeframe, strategy, multiplier)
        VALUES ('WINQ26', 'BUY', 1.0, '2026-06-23 11:00:00', 120000.0,
                '99999999', 'M5', 'PIVOT_POINTS', 0.20)
    """)
    conn.commit()
    conn.close()

    # MT5 não tem essa posição
    result = wd.reconcile_with_mt5([])

    assert len(result["closed"]) == 1, f"deveria fechar 1: {result}"
    assert result["closed"][0]["reason"] == "mt5_missing"
    assert result["closed"][0]["pnl"] == 0.0

    # Verificar que foi fechada no DB
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT exit_time, exit_reason, close_source FROM trades WHERE entry_ticket='99999999'").fetchone()
    assert row[0] is not None, "exit_time deve estar preenchido"
    assert row[1] == "MT5_MISSING"
    assert row[2] == "MT5_MISSING"
    conn.close()
    os.unlink(path)


def test_reconcile_skips_non_vibetrading_comments():
    """Cenário: posição no MT5 com comment diferente de 'VibeTrading'. Deve pular."""
    path = make_tmp_db()
    wd.DB_PATH = Path(path)

    mt5_positions = [{
        "ticket": 123456,
        "symbol": "WINQ26",
        "type": "BUY",
        "volume": 1.0,
        "price_open": 120000.0,
        "time": int(datetime.now().timestamp()),
        "comment": "OutroBot",  # Não é VibeTrading
    }]

    result = wd.reconcile_with_mt5(mt5_positions)
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["reason"] == "not_vibetrading"
    assert len(result["inserted"]) == 0
    os.unlink(path)


def test_reconcile_logs_divergence_but_no_op():
    """Cenário C: ambos têm a posição mas com entry_price divergente. Loga, não corrige."""
    path = make_tmp_db()
    wd.DB_PATH = Path(path)

    conn = sqlite3.connect(path)
    conn.execute("""
        INSERT INTO trades (symbol, direction, volume, entry_time, entry_price,
                          entry_ticket, timeframe, strategy, multiplier)
        VALUES ('WINQ26', 'BUY', 1.0, '2026-06-23 11:00:00', 120000.0,
                '11111111', 'M5', 'PIVOT_POINTS', 0.20)
    """)
    conn.commit()
    conn.close()

    mt5_positions = [{
        "ticket": 11111111,
        "symbol": "WINQ26",
        "type": "BUY",
        "volume": 1.0,
        "price_open": 120050.0,  # Diferente do DB (120000)
        "time": int(datetime.now().timestamp()),
        "comment": "VibeTrading",
    }]

    result = wd.reconcile_with_mt5(mt5_positions)

    assert len(result["divergences"]) == 1
    assert result["divergences"][0]["ticket"] == "11111111"
    assert result["divergences"][0]["diff"] == 50.0
    assert len(result["inserted"]) == 0
    assert len(result["closed"]) == 0
    os.unlink(path)


def test_reconcile_handles_mt5_unavailable():
    """Cenário: DB não existe. Deve retornar erro gracefully, não crashar."""
    wd.DB_PATH = Path("/tmp/inexistente_vt_trades.db")
    if wd.DB_PATH.exists():
        wd.DB_PATH.unlink()

    result = wd.reconcile_with_mt5([{"ticket": 1, "comment": "VibeTrading"}])
    assert "db_not_found" in result["errors"]


def test_reconcile_full_scenario_a_b_c():
    """Cenário completo: 3 tickets — 1 só MT5, 1 só DB, 1 ambos divergentes."""
    path = make_tmp_db()
    wd.DB_PATH = Path(path)

    # DB tem 2 posições abertas: 100 e 200
    conn = sqlite3.connect(path)
    conn.executescript("""
        INSERT INTO trades (symbol, direction, volume, entry_time, entry_price,
                          entry_ticket, timeframe, strategy, multiplier)
        VALUES ('WINQ26', 'BUY', 1.0, '2026-06-23 11:00:00', 120000.0,
                '100', 'M5', 'PIVOT_POINTS', 0.20);
        INSERT INTO trades (symbol, direction, volume, entry_time, entry_price,
                          entry_ticket, timeframe, strategy, multiplier)
        VALUES ('WDOQ26', 'SELL', 1.0, '2026-06-23 11:00:00', 5226.0,
                '200', 'M5', 'PIVOT_POINTS', 10.0);
    """)
    conn.commit()
    conn.close()

    # MT5 tem 2 posições: 100 (igual) e 300 (nova)
    mt5_positions = [
        {"ticket": 100, "symbol": "WINQ26", "type": "BUY", "volume": 1.0,
         "price_open": 120000.0, "time": int(datetime.now().timestamp()),
         "comment": "VibeTrading"},
        {"ticket": 300, "symbol": "BITM26", "type": "BUY", "volume": 1.0,
         "price_open": 323080.0, "time": int(datetime.now().timestamp()),
         "comment": "VibeTrading"},
    ]

    result = wd.reconcile_with_mt5(mt5_positions)

    # ticket 200 sumiu do MT5 → fechou
    assert len(result["closed"]) == 1
    assert result["closed"][0]["ticket"] == "200"
    # ticket 300 só no MT5 → inseriu
    assert len(result["inserted"]) == 1
    assert result["inserted"][0]["ticket"] == "300"
    # ticket 100 está em ambos com mesmo preço → sem divergência
    assert len(result["divergences"]) == 0
    os.unlink(path)


def test_diff_db_vs_mt5_no_modification():
    """diff_db_vs_mt5 deve ser read-only, não alterar DB."""
    path = make_tmp_db()
    wd.DB_PATH = Path(path)

    conn = sqlite3.connect(path)
    conn.execute("""
        INSERT INTO trades (symbol, direction, volume, entry_time, entry_price,
                          entry_ticket, timeframe, strategy, multiplier)
        VALUES ('WINQ26', 'BUY', 1.0, '2026-06-23 11:00:00', 120000.0,
                '500', 'M5', 'PIVOT_POINTS', 0.20)
    """)
    conn.commit()
    conn.close()

    mt5_positions = [
        {"ticket": 600, "symbol": "BITM26", "type": "BUY", "volume": 1.0,
         "price_open": 323080.0, "time": 0, "comment": "VibeTrading"},
        {"ticket": 500, "symbol": "WINQ26", "type": "BUY", "volume": 1.0,
         "price_open": 120000.0, "time": 0, "comment": "VibeTrading"},
    ]

    diff = wd.diff_db_vs_mt5(mt5_positions)
    assert "600" in diff["mt5_only"]
    assert "500" in diff["both_match"]
    assert len(diff["db_only"]) == 0

    # DB não foi modificado
    conn = sqlite3.connect(path)
    rows = conn.execute("SELECT exit_time FROM trades WHERE entry_ticket='500'").fetchall()
    assert rows[0][0] is None, "diff não deve fechar posições"
    conn.close()
    os.unlink(path)


def test_get_open_positions_from_db():
    """get_open_positions_from_db deve retornar só posições com exit_time IS NULL."""
    path = make_tmp_db()
    wd.DB_PATH = Path(path)

    conn = sqlite3.connect(path)
    conn.executescript("""
        INSERT INTO trades (symbol, direction, volume, entry_time, entry_price,
                          entry_ticket, timeframe, strategy, multiplier)
        VALUES ('WINQ26', 'BUY', 1.0, '2026-06-23 11:00:00', 120000.0,
                '700', 'M5', 'PIVOT_POINTS', 0.20);
        INSERT INTO trades (symbol, direction, volume, entry_time, entry_price,
                          entry_ticket, timeframe, strategy, multiplier, exit_time)
        VALUES ('WDOQ26', 'SELL', 1.0, '2026-06-23 11:00:00', 5226.0,
                '800', 'M5', 'PIVOT_POINTS', 10.0, '2026-06-23 12:00:00');
    """)
    conn.commit()
    conn.close()

    open_pos = wd.get_open_positions_from_db(str(path))
    assert len(open_pos) == 1
    assert open_pos[0]["entry_ticket"] == "700"
    os.unlink(path)
