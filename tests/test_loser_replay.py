"""
test_loser_replay.py — Wave N+5B (2026-07-08)

Valida monitoring/vt_loser_replay.generate_report.

Casos:
  1. Sem dados — relatório vazio com n_losing_trades=0.
  2. Losing trades + blocked setups match — gera hypotheses com
     total_would_have_saved_brl positivo.
  3. Losing trades sem blocked setups match — hypotheses vazio.
  4. Ranking ordena por impacto decrescente.
  5. Arquivo JSON escrito em reports_dir.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "monitoring"),
          str(PROJECT_ROOT / "core")):
    if p not in sys.path:
        sys.path.insert(0, p)


def _setup_db(tmp_path):
    """Cria DB com schema completo.

    Conftest autouse cria 'trades' + 'signal_blocked_log' com schema
    mínimo. Esta função DROP and RECREATE com schema necessário pelo
    loser_replay.
    """
    db = tmp_path / "vt_trades.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        DROP TABLE IF EXISTS trades;
        DROP TABLE IF EXISTS signal_blocked_log;
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            tf TEXT DEFAULT 'M5',
            timeframe TEXT DEFAULT 'M5',
            direction TEXT DEFAULT 'BUY',
            volume REAL DEFAULT 1.0,
            entry_time TEXT NOT NULL,
            exit_time TEXT,
            strategy TEXT DEFAULT 'VWAP',
            net_pnl REAL DEFAULT 0
        );
        CREATE TABLE signal_blocked_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            symbol TEXT NOT NULL,
            tf TEXT NOT NULL,
            strategy TEXT NOT NULL,
            block_reason TEXT,
            outcome_win INTEGER,
            outcome_pnl_pts REAL,
            resolved INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    conn.close()
    return db


def _insert_trade(conn, symbol, strategy, pnl, hours_ago=1, direction="BUY"):
    entry_dt = (datetime.now() - timedelta(hours=hours_ago))
    conn.execute(
        "INSERT INTO trades (symbol, strategy, direction, volume, "
        "entry_time, net_pnl) VALUES (?, ?, ?, ?, ?, ?)",
        (symbol, strategy, direction, 1.0, entry_dt.isoformat(), pnl),
    )
    conn.commit()


def _insert_blocked(conn, symbol, strategy, reason, pnl_pts, win,
                    hours_ago=2):
    ts_dt = (datetime.now() - timedelta(hours=hours_ago))
    conn.execute(
        "INSERT INTO signal_blocked_log (ts, symbol, tf, strategy, "
        "block_reason, outcome_win, outcome_pnl_pts, resolved) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
        (ts_dt.isoformat(), symbol, "M5", strategy, reason, win, pnl_pts),
    )
    conn.commit()


# ═══════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════

def test_empty_db_no_losers(tmp_path):
    """DB vazia → report sem hypotheses."""
    db = _setup_db(tmp_path)
    from monitoring.vt_loser_replay import generate_report
    out = generate_report(
        db_path=db,
        reports_dir=tmp_path / "reports",
    )
    payload = json.loads(out.read_text())
    assert payload["n_losing_trades"] == 0
    assert payload["hypotheses"] == []
    assert out.exists()


def test_loser_with_matching_blocked_setups(tmp_path):
    """1 losing trade + 3 blocked wins com pnl_pts > 0 → hipótese."""
    db = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    _insert_trade(conn, "WINQ26", "ADX_TREND", -100.0)
    # 3 blocked setups que resultaram em win com +30 pts cada (R$30 cada).
    for _ in range(3):
        _insert_blocked(conn, "WINQ26", "ADX_TREND",
                        "VOL_FILTER", 30.0, win=1)
    conn.close()

    from monitoring.vt_loser_replay import generate_report
    out = generate_report(
        db_path=db,
        reports_dir=tmp_path / "reports",
        lookback_days=1,
    )
    payload = json.loads(out.read_text())
    assert payload["n_losing_trades"] == 1
    assert len(payload["hypotheses"]) >= 1
    hyp = payload["hypotheses"][0]
    assert hyp["n_losers"] >= 1
    # 3 setups × R$30 = R$90 total would_have_saved
    assert hyp["total_would_have_saved_brl"] == 90.0


def test_no_matching_blocked_no_hypotheses(tmp_path):
    """Losing trade sem blocked setups match → hypotheses vazio."""
    db = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    _insert_trade(conn, "WINQ26", "ADX_TREND", -100.0)
    # blocked setups de outra estratégia — não matcham.
    _insert_blocked(conn, "WINQ26", "RSI_REVERSION",
                    "VOL_FILTER", 30.0, win=1)
    conn.close()

    from monitoring.vt_loser_replay import generate_report
    out = generate_report(
        db_path=db,
        reports_dir=tmp_path / "reports",
    )
    payload = json.loads(out.read_text())
    assert payload["n_losing_trades"] == 1
    assert payload["hypotheses"] == []


def test_ranking_orders_by_impact(tmp_path):
    """Estratégia com mais blocked setups/savings aparece primeiro."""
    db = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    _insert_trade(conn, "WINQ26", "ADX_TREND", -100.0)
    _insert_trade(conn, "WDON26", "VWAP", -50.0)

    # ADX_TREND: 5 wins bloqueados — alto impacto
    for _ in range(5):
        _insert_blocked(conn, "WINQ26", "ADX_TREND",
                        "VOL_FILTER", 50.0, win=1)
    # VWAP: 1 win bloqueado — baixo impacto
    for _ in range(1):
        _insert_blocked(conn, "WDON26", "VWAP",
                        "VOL_FILTER", 10.0, win=1)
    conn.close()

    from monitoring.vt_loser_replay import generate_report
    out = generate_report(
        db_path=db, reports_dir=tmp_path / "reports",
    )
    payload = json.loads(out.read_text())
    assert len(payload["hypotheses"]) == 2
    # ADX_TREND primeiro (5 × 50 = 250 > 1 × 10 = 10)
    assert payload["hypotheses"][0]["total_would_have_saved_brl"] >= 200
    assert payload["hypotheses"][1]["total_would_have_saved_brl"] <= 50


def test_report_files_written(tmp_path):
    """Arquivo JSON válido escrito em reports_dir."""
    db = _setup_db(tmp_path)
    reports_dir = tmp_path / "my_reports"
    from monitoring.vt_loser_replay import generate_report
    out = generate_report(db_path=db, reports_dir=reports_dir)
    assert out.parent == reports_dir
    assert out.suffix == ".json"
    assert json.loads(out.read_text())  # parseable


def test_handles_old_blocked_data(tmp_path):
    """Blocked setups de ontem (fora da janela lookback=1) NÃO contam."""
    db = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    _insert_trade(conn, "WINQ26", "ADX_TREND", -100.0, hours_ago=2)
    # Blocked ANTIGO (3 dias atrás) — fora do lookback
    _insert_blocked(conn, "WINQ26", "ADX_TREND",
                    "VOL_FILTER", 50.0, win=1, hours_ago=72)
    conn.close()

    from monitoring.vt_loser_replay import generate_report
    out = generate_report(
        db_path=db, reports_dir=tmp_path / "reports",
        lookback_days=1,
    )
    payload = json.loads(out.read_text())
    # Hypotheses devem ser vazias — blocking recente não tem.
    assert payload["hypotheses"] == []
