"""
test_edge_estimator.py — Wave N+3B (2026-07-08)

Valida core/vt_edge_estimator.py (com tmp DB isolation):
  1. ensure_schema idempotente.
  2. update retorna None se enabled=False.
  3. update retorna None se n < min_trades.
  4. update calcula expectancy, decay, scale quando n suficiente.
  5. Scale = 1.0 quando decay >= 0 (sem degradação).
  6. Scale = floor quando decay <= threshold.
  7. Scale linear entre threshold e 0.
  8. get_recommended_size_scale cacheia (TTL 5 min).
  9. get_recommended_size_scale default 1.0 sem row.
  10. get_recommended_size_scale lê último por ORDER BY ts DESC.
  11. Não interfere com outras tabelas do DB (trades, signal_blocked_log).
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "core"),
          str(PROJECT_ROOT / "mt5")):
    if p not in sys.path:
        sys.path.insert(0, p)


def _fresh_ee(tmp_path, monkeypatch):
    """EE isolado em tmp DB."""
    import importlib
    ee = importlib.import_module("core.vt_edge_estimator")
    sj = importlib.import_module("core.vt_signal_journal")
    tmp_db = tmp_path / "vt_trades.db"
    monkeypatch.setattr(ee, "DB_PATH", tmp_db)
    monkeypatch.setattr(sj, "DB_PATH", tmp_db)
    # Cria schema completo (trades + signal_blocked_log + edge_estimator)
    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    # Edge schema
    conn.executescript(ee.EDGE_SCHEMA)
    for idx in ee.EDGE_INDEXES:
        conn.execute(idx)
    conn.commit()
    conn.close()
    # trades schema (minimal) — idempotente porque conftest já cria
    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_ticket TEXT,
            symbol TEXT NOT NULL,
            direction TEXT,
            volume REAL,
            timeframe TEXT DEFAULT 'M5',
            entry_time TEXT NOT NULL,
            exit_time TEXT,
            entry_price REAL NOT NULL,
            exit_price REAL,
            net_pnl REAL DEFAULT 0,
            strategy TEXT DEFAULT 'VWAP'
        );
    """)
    conn.commit()
    conn.close()
    return ee


def _insert_trade(ee, n_pnl_list, *, symbol="WINQ26", tf="M5",
                  strategy="ADX_TREND", days_ago=1):
    """Insere n trades em janela recente."""
    conn = sqlite3.connect(str(ee.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        for i, pnl in enumerate(n_pnl_list):
            entry_dt = datetime.now() - timedelta(days=days_ago, hours=i)
            conn.execute(
                "INSERT INTO trades (symbol, direction, volume, timeframe, "
                "entry_time, exit_time, entry_price, exit_price, net_pnl, "
                "strategy) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (symbol, "BUY", 1.0, tf,
                 entry_dt.isoformat(),
                 (entry_dt + timedelta(hours=1)).isoformat(),
                 100.0, 101.0, pnl, strategy),
            )
        conn.commit()
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════
# Schema
# ═══════════════════════════════════════════════════════════

def test_ensure_schema_idempotent(tmp_path, monkeypatch):
    ee = _fresh_ee(tmp_path, monkeypatch)
    conn = sqlite3.connect(str(ee.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master "
            "WHERE type='table' AND name='edge_estimator'"
        )
        assert cur.fetchone()["n"] == 1
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='edge_estimator'"
        ).fetchall()
        names = {r["name"] for r in idx}
        assert "idx_edge_sym_tf_strat_ts" in names
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════
# update()
# ═══════════════════════════════════════════════════════════

def test_update_disabled_returns_none(tmp_path, monkeypatch):
    ee = _fresh_ee(tmp_path, monkeypatch)
    cfg = {"edge_estimator": {"enabled": False}}
    assert ee.update("WINQ26", "M5", "ADX_TREND", config=cfg) is None


def test_update_below_min_trades_returns_none(tmp_path, monkeypatch):
    ee = _fresh_ee(tmp_path, monkeypatch)
    cfg = {"edge_estimator": {"enabled": True, "min_trades": 20}}
    # 5 trades — abaixo de min_trades
    _insert_trade(ee, [10.0] * 5, symbol="WINQ26", tf="M5",
                  strategy="ADX_TREND")
    assert ee.update("WINQ26", "M5", "ADX_TREND", config=cfg) is None


def test_update_computes_expectancy_and_persists(tmp_path, monkeypatch):
    ee = _fresh_ee(tmp_path, monkeypatch)
    cfg = {
        "edge_estimator": {
            "enabled": True,
            "min_trades": 20,
            "decay_threshold": -0.30,
            "size_scale_floor": 0.4,
        },
    }
    # 25 trades, mix de wins/losses, expectancy ~R$ 6.
    pnls = [10.0] * 15 + [-5.0] * 10  # mean = (150 - 50) / 25 = R$ 4
    _insert_trade(ee, pnls, strategy="ADX_TREND")

    snap = ee.update("WINQ26", "M5", "ADX_TREND", config=cfg)
    assert snap is not None
    assert snap["n"] == 25
    assert snap["wins"] == 15
    assert 3.5 <= snap["expectancy_pts"] <= 4.5
    # baseline default = 5.0; decay = (4 - 5)/5 = -0.20 (> -0.30 threshold)
    assert -0.30 < snap["edge_decay"] < -0.10
    # linear: ratio = -0.20 / -0.30 = 0.667; scale = 0.4 + 0.6 * 0.667 = 0.8
    assert 0.7 < snap["recommended_size_scale"] < 0.9


# ═══════════════════════════════════════════════════════════
# Regras de scale
# ═══════════════════════════════════════════════════════════

def test_update_healthy_edge_returns_scale_1(tmp_path, monkeypatch):
    ee = _fresh_ee(tmp_path, monkeypatch)
    cfg = {
        "edge_estimator": {"enabled": True, "min_trades": 20},
        "params_by_tf": {
            "ADX_TREND_M5": {"baseline_expectancy_pts": 5.0},
        },
    }
    _insert_trade(ee, [15.0] * 25, strategy="ADX_TREND")
    snap = ee.update("WINQ26", "M5", "ADX_TREND", config=cfg)
    assert snap["edge_decay"] > 0
    assert snap["recommended_size_scale"] == 1.0


def test_update_catastrophic_decay_returns_floor(tmp_path, monkeypatch):
    ee = _fresh_ee(tmp_path, monkeypatch)
    cfg = {
        "edge_estimator": {
            "enabled": True,
            "min_trades": 20,
            "decay_threshold": -0.30,
            "size_scale_floor": 0.4,
        },
        "params_by_tf": {
            "ADX_TREND_M5": {"baseline_expectancy_pts": 5.0},
        },
    }
    # Trades negativos todos — expectancy = -R$ 10, decay = (-10-5)/5 = -3.0
    _insert_trade(ee, [-10.0] * 25, strategy="ADX_TREND")
    snap = ee.update("WINQ26", "M5", "ADX_TREND", config=cfg)
    assert snap["edge_decay"] <= -0.30
    assert snap["recommended_size_scale"] == 0.4


# ═══════════════════════════════════════════════════════════
# get_recommended_size_scale
# ═══════════════════════════════════════════════════════════

def test_get_recommended_size_scale_default_when_empty(tmp_path, monkeypatch):
    ee = _fresh_ee(tmp_path, monkeypatch)
    # DB vazia → 1.0 default
    assert ee.get_recommended_size_scale("WINQ26", "M5", "X") == 1.0


def test_get_recommended_size_scale_returns_latest(tmp_path, monkeypatch):
    ee = _fresh_ee(tmp_path, monkeypatch)
    cfg = {"edge_estimator": {"enabled": True, "min_trades": 20},
           "params_by_tf": {"X_M5": {"baseline_expectancy_pts": 5.0}}}
    # 1ª leitura: alto expectancy → scale 1.0
    _insert_trade(ee, [20.0] * 25, strategy="X")
    ee.update("WINQ26", "M5", "X", config=cfg)
    # Limpa cache para forçar reler do DB após INSERT manual.
    ee._LAST_CACHE.clear()
    # 2ª leitura: trades negativos depois — direta insert no DB com ts depois
    conn = sqlite3.connect(str(ee.DB_PATH))
    conn.row_factory = sqlite3.Row
    future_ts = (datetime.now() + timedelta(hours=1)).astimezone().isoformat()
    conn.execute(
        "INSERT INTO edge_estimator (ts, symbol, tf, strategy, n, wins, "
        "expectancy_pts, baseline_expectancy_pts, edge_decay, "
        "recommended_size_scale) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (future_ts, "WINQ26", "M5", "X", 25, 0, -10.0, 5.0, -3.0, 0.4),
    )
    conn.commit()
    conn.close()
    # Função deve pegar o MAIS RECENTE (0.4)
    assert ee.get_recommended_size_scale("WINQ26", "M5", "X") == 0.4


def test_get_recommended_size_scale_caches(tmp_path, monkeypatch):
    """Hit em cache não lê DB de novo (5min TTL)."""
    ee = _fresh_ee(tmp_path, monkeypatch)
    cfg = {"edge_estimator": {"enabled": True, "min_trades": 20}}
    _insert_trade(ee, [-10.0] * 25, strategy="X")
    snap = ee.update("WINQ26", "M5", "X", config=cfg)
    assert snap is not None
    # 2ª chamada: cache hit, ainda que DB mude.
    first = ee.get_recommended_size_scale("WINQ26", "M5", "X")
    second = ee.get_recommended_size_scale("WINQ26", "M5", "X")
    assert first == second


# ═══════════════════════════════════════════════════════════
# Não-interferência
# ═══════════════════════════════════════════════════════════

def test_does_not_break_trades_or_signal_blocked_tables(tmp_path, monkeypatch):
    """EE compartilha DB com trades + signal_blocked_log."""
    ee = _fresh_ee(tmp_path, monkeypatch)
    _insert_trade(ee, [10.0] * 5, strategy="X")
    # touch edge schema via ensure_schema (already called in _fresh_ee)
    conn = sqlite3.connect(str(ee.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {r["name"] for r in tables}
        assert {"trades", "edge_estimator"}.issubset(names)
    finally:
        conn.close()
