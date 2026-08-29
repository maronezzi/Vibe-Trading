# -*- coding: utf-8 -*-
"""Tests Wave 883.B2 (29/08/2026) — swap_scorecard em modo observação.

Confere o "conferidor de recibos" do AGI v4: para cada swap do journal
com idade >= N pregões, o PnL entregue (live + shadow na janela da troca)
é comparado contra o pnl_claimed. Nenhum gate/quarentena nesta wave —
só reporte.

Hermético: DB sintético em tmp_path, journal sintético via monkeypatch.
Nenhum MT5, nenhum config/DB real.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from optimization.agi_v4 import swap_scorecard as sc  # noqa: E402


def _make_db(tmp_path: Path) -> Path:
    """Semeia o DB isolado do conftest (autouse já cria `trades` em
    tmp_path/vt_trades.db — espelha o schema de produção)."""
    db = tmp_path / "vt_trades.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE IF NOT EXISTS forward_sim_trades (
        id INTEGER PRIMARY KEY, symbol TEXT, timeframe TEXT,
        entry_time TEXT, net_pnl_brl REAL)""")
    # 6 pregões distintos (idade suficiente p/ MIN_PREGOES=5)
    live = [
        ("WINV26", "M30", "2026-08-21 10:00:00", "2026-08-21 10:30:00", "MT5_SERVER_SL", -50.0),
        ("WINV26", "M30", "2026-08-22 11:00:00", "2026-08-22 11:20:00", "TRAILING", 120.0),
        ("WINV26", "M30", "2026-08-25 09:30:00", "2026-08-25 10:00:00", "GHOST", -999.0),
        ("WINV26", "M15", "2026-08-25 09:30:00", "2026-08-25 10:00:00", "MT5_SERVER_SL", -10.0),
        ("WINV26", "M30", "2026-08-26 09:30:00", "2026-08-26 10:00:00", "TRAILING", 30.0),
        ("WINV26", "M30", "2026-08-27 09:30:00", "2026-08-27 10:00:00", "TRAILING", 0.0),
        ("WINV26", "M30", "2026-08-28 09:30:00", "2026-08-28 10:00:00", "TRAILING", 0.0),
    ]
    conn.executemany(
        "INSERT INTO trades (symbol, direction, volume, timeframe, entry_time, "
        "entry_price, exit_time, exit_reason, net_pnl) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [(r[0], "BUY", 1.0, r[1], r[2], 100.0, r[3], r[4], r[5]) for r in live])
    conn.executemany(
        "INSERT INTO forward_sim_trades (symbol, timeframe, entry_time, net_pnl_brl) "
        "VALUES (?,?,?,?)",
        [("WINV26", "M30", "2026-08-22 12:00:00", 15.0),
         ("WINV26", "M30", "2026-08-26 12:00:00", 35.0)])
    conn.commit()
    conn.close()
    return db


def _make_journal(tmp_path: Path, events: list) -> Path:
    jp = tmp_path / "pair_change_journal.json"
    jp.write_text(json.dumps(events), encoding="utf-8")
    return jp


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    # _db_path prioriza o _resolve_db_path do stage1 (que aponta pro DB real
    # de produção) — patcheamos a função inteira para isolar no DB sintético.
    monkeypatch.setattr(sc, "_db_path", lambda cfg: db)
    monkeypatch.setattr(sc, "JOURNAL_PATH", tmp_path / "pair_change_journal.json")
    return tmp_path


def test_ratio_entregue_vs_alegado(wired):
    _make_journal(wired, [
        {"ts": "2026-08-21T17:00:00", "kind": "swap", "pair": "WIN_M30",
         "pnl_claimed": 300.0},
    ])
    r = sc.run({"config": {}})
    assert r["n_scored"] == 1
    s = r["swaps"][0]
    # live: -50+120+30 (GHOST -999 excluído, M15 fora do par, zeros somam 0)
    # shadow: +15+35 = +50 → entregue 150/300 = 0.5
    assert s["live"] == pytest.approx(100.0)
    assert s["shadow"] == pytest.approx(50.0)
    assert s["delivered"] == pytest.approx(150.0)
    assert s["ratio"] == pytest.approx(0.5)


def test_swap_jovo_excluido(wired):
    _make_journal(wired, [
        {"ts": "2026-08-28T17:00:00", "kind": "swap", "pair": "WIN_M30",
         "pnl_claimed": 500.0},
    ])
    r = sc.run({"config": {}})
    assert r["n_scored"] == 0  # só 1 pregão de idade < mínimo 5


def test_janela_termina_no_proximo_evento_do_par(wired):
    _make_journal(wired, [
        {"ts": "2026-08-21T17:00:00", "kind": "swap", "pair": "WIN_M30",
         "pnl_claimed": 300.0},
        {"ts": "2026-08-26T17:00:00", "kind": "swap", "pair": "WIN_M30",
         "pnl_claimed": 100.0},
    ])
    r = sc.run({"config": {}})
    # swap 1: janela [21/08, 26/08) → live -50+120=70, shadow +15 → ratio 85/300
    # swap 2: idade insuficiente → excluído
    assert r["n_scored"] == 1
    s = r["swaps"][0]
    assert s["shadow"] == pytest.approx(15.0)


def test_journal_ausente_ou_sem_claim(wired):
    r = sc.run({"config": {}})
    assert r["n_scored"] == 0
    _make_journal(wired, [
        {"ts": "2026-08-21T17:00:00", "kind": "live_kill", "pair": "WDO_M15",
         "pnl": -405.0},
    ])
    r = sc.run({"config": {}})
    assert r["n_scored"] == 0  # live_kill não tem pnl_claimed — não é recibo


def test_telegram_line(wired):
    _make_journal(wired, [
        {"ts": "2026-08-21T17:00:00", "kind": "swap", "pair": "WIN_M30",
         "pnl_claimed": 300.0},
    ])
    line = sc.telegram_line(sc.run({"config": {}}))
    assert line and "Scorecard" in line and "WIN_M30" in line
    assert sc.telegram_line({"n_scored": 0}) is None


def test_env_desliga(monkeypatch):
    monkeypatch.setenv("VT_AGI_SCORECARD", "0")
    assert sc.enabled() is False
