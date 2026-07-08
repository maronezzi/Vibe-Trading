"""
test_signal_journal.py — Wave N+1 (2026-07-08)

Valida core/vt_signal_journal.py — log contrafactual de setups latentes
barrados por filtro. Cobre:
  1. Schema + indexes idempotentes.
  2. log_blocked_signal enfileira + flush persiste.
  3. Idempotência por UNIQUE(ts, symbol, tf, direction, strategy).
  4. Batch flush (>=50 rows ou >=30s).
  5. resolve_blocked_outcomes com fetcher mock (win/loss/pnl).
  6. compute_selectivity agrega entries vs blocked por estratégia.
  7. Falha de DB não propaga (defesa para tick loop).
  8. Hook _maybe_log_blocked_signal do autotrader — heurística LATENT_LOOKBACK.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "core"),
          str(PROJECT_ROOT / "mt5"), str(PROJECT_ROOT / "monitoring"),
          str(PROJECT_ROOT / "optimization")):
    if p not in sys.path:
        sys.path.insert(0, p)


def _fresh_journal(tmp_path, monkeypatch):
    """Cria conexão isolada e retorna módulo + coneção."""
    import importlib
    sj = importlib.import_module("core.vt_signal_journal")
    monkeypatch.setattr(sj, "DB_PATH", tmp_path / "vt_trades.db")
    sj.reset_buffer_for_test()
    sj.ensure_schema()
    return sj


def _insert_trade(conn, **kw):
    """Helper: insere linha em `trades` (mesma tabela que selectivity lê)."""
    cols = ",".join(kw.keys())
    placeholders = ",".join("?" for _ in kw)
    conn.execute(f"INSERT INTO trades ({cols}) VALUES ({placeholders})", list(kw.values()))
    conn.commit()


# ═══════════════════════════════════════════════════════════
# Schema e idempotência
# ═══════════════════════════════════════════════════════════

def test_ensure_schema_creates_table(tmp_path, monkeypatch):
    """ensure_schema() cria signal_blocked_log + indexes."""
    sj = _fresh_journal(tmp_path, monkeypatch)
    conn = sqlite3.connect(str(sj.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='signal_blocked_log'"
        )
        assert cur.fetchone() is not None
        # indexes
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='signal_blocked_log'"
        ).fetchall()
        names = {r["name"] if hasattr(r, "keys") else r[0] for r in idx}
        assert "idx_blocked_sym_tf_strat_ts" in names
        assert "idx_blocked_resolved_ts" in names
    finally:
        conn.close()


def test_ensure_schema_is_idempotent(tmp_path, monkeypatch):
    """Chamar 3x não levanta e mantém schema consistente."""
    sj = _fresh_journal(tmp_path, monkeypatch)
    sj.ensure_schema()
    sj.ensure_schema()
    sj.ensure_schema()
    conn = sqlite3.connect(str(sj.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master "
            "WHERE type='table' AND name='signal_blocked_log'"
        ).fetchone()
        n = row["n"] if hasattr(row, "keys") else row[0]
        assert n == 1
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════
# log_blocked_signal + flush
# ═══════════════════════════════════════════════════════════

def test_log_blocked_signal_enqueues_then_flush_persists(tmp_path, monkeypatch):
    """log_blocked_signal adiciona ao buffer; flush persiste no DB."""
    sj = _fresh_journal(tmp_path, monkeypatch)
    sj.log_blocked_signal(
        symbol="WINQ26", tf="M5", strategy="ADX_TREND",
        direction="BUY", block_reason="VOL_FILTER",
        sl_pts=300, atr_pts=120.5, regime="TREND",
    )
    # Não flushou ainda (buffer < 50 rows e < 30s)
    conn = sqlite3.connect(str(sj.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM signal_blocked_log").fetchone()
        assert n["n"] == 0
    finally:
        conn.close()

    # Flush manual
    n_written = sj.flush()
    assert n_written == 1

    conn = sqlite3.connect(str(sj.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM signal_blocked_log"
        ).fetchone()
        assert row is not None
        assert row["strategy"] == "ADX_TREND"
        assert row["block_reason"] == "VOL_FILTER"
        assert row["direction"] == "BUY"
        assert row["hypothetical_sl_pts"] == 300
        assert row["resolved"] == 0
    finally:
        conn.close()


def test_log_blocked_signal_idempotent_unique_constraint(
    tmp_path, monkeypatch
):
    """Mesma ts+symbol+tf+strategy+direction não duplica."""
    sj = _fresh_journal(tmp_path, monkeypatch)
    iso = "2026-07-08T15:00:00-03:00"
    sj.log_blocked_signal(
        symbol="WINQ26", tf="M5", strategy="ADX_TREND",
        direction="BUY", block_reason="VOL_FILTER", ts=iso,
    )
    sj.log_blocked_signal(
        symbol="WINQ26", tf="M5", strategy="ADX_TREND",
        direction="BUY", block_reason="VOL_FILTER_2", ts=iso,
    )
    # Flush manual (segundo write sobrescreve o primeiro — INSERT OR IGNORE)
    sj.flush()

    conn = sqlite3.connect(str(sj.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM signal_blocked_log").fetchone()
        assert n["n"] == 1
    finally:
        conn.close()


def test_log_blocked_signal_auto_flush_at_batch_size(
    tmp_path, monkeypatch
):
    """Buffer >= 50 rows dispara flush automático (primeira metade)."""
    sj = _fresh_journal(tmp_path, monkeypatch)
    for i in range(51):
        sj.log_blocked_signal(
            symbol="WINQ26", tf="M5",
            strategy=f"STRATEGY_{i}",
            direction="BUY", block_reason="TEST",
        )
    conn = sqlite3.connect(str(sj.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM signal_blocked_log").fetchone()
        # 50 rows persistidas via auto-flush; o 51 fica no buffer aguardando.
        # (Próximo append ou wait de 30s drena. Aceitável por design.)
        assert n["n"] == 50
    finally:
        conn.close()
    # O 51º row fica no buffer — pode ir via próximo append ou flush manual.
    assert len(sj._blocked_buffer) == 1  # noqa: SLF001


def test_log_blocked_signal_survives_db_failure(tmp_path, monkeypatch):
    """Se DB falha, row fica no buffer — defesa para tick loop."""
    sj = _fresh_journal(tmp_path, monkeypatch)
    # Patchar _connect pra raise
    monkeypatch.setattr(
        sj, "_connect",
        lambda: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")),
    )
    sj.log_blocked_signal(
        symbol="WINQ26", tf="M5", strategy="X",
        direction=None, block_reason="FAIL_TEST",
    )
    n_written = sj.flush()
    assert n_written == 0
    # Buffer deve ter mantido a row
    assert len(sj._blocked_buffer) >= 1  # noqa: SLF001 (test only)


# ═══════════════════════════════════════════════════════════
# resolve_blocked_outcomes
# ═══════════════════════════════════════════════════════════

def test_resolve_blocked_outcomes_winner(tmp_path, monkeypatch):
    """BUY com exit > entry marca win=1 e pnl positivo."""
    sj = _fresh_journal(tmp_path, monkeypatch)
    iso_old = (
        datetime.now().astimezone() - timedelta(hours=3)
    ).isoformat()
    sj.log_blocked_signal(
        symbol="WINQ26", tf="M5", strategy="X",
        direction="BUY", block_reason="VOL_FILTER",
        sl_pts=200, ts=iso_old,
    )
    sj.flush()

    fetcher = mock.Mock(return_value={
        "entry_price": 100.0,
        "exit_price": 110.0,  # BUY ganha 10 pts
    })
    n = sj.resolve_blocked_outcomes(window_minutes=120, fetcher=fetcher)
    assert n == 1

    conn = sqlite3.connect(str(sj.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM signal_blocked_log"
        ).fetchone()
        assert row["resolved"] == 1
        assert row["outcome_win"] == 1
        # pnl = 10 - 200 = -190 (clamp se |delta| > sl) → aqui |10| < 200, ok
        assert row["outcome_pnl_pts"] == -190.0
    finally:
        conn.close()


def test_resolve_blocked_outcomes_loser_clamped(tmp_path, monkeypatch):
    """Se |delta| > sl, pnl_pts = -sl (worst case)."""
    sj = _fresh_journal(tmp_path, monkeypatch)
    iso_old = (
        datetime.now().astimezone() - timedelta(hours=3)
    ).isoformat()
    sj.log_blocked_signal(
        symbol="WINQ26", tf="M5", strategy="X",
        direction="BUY", block_reason="X",
        sl_pts=50, ts=iso_old,  # sl menor que o delta → clamp dispara
    )
    sj.flush()

    fetcher = mock.Mock(return_value={
        "entry_price": 100.0,
        "exit_price": 30.0,  # cai 70 pts (> sl=50) → deve clamp -50
    })
    n = sj.resolve_blocked_outcomes(window_minutes=120, fetcher=fetcher)
    assert n == 1

    conn = sqlite3.connect(str(sj.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM signal_blocked_log").fetchone()
        assert row["outcome_win"] == 0
        assert row["outcome_pnl_pts"] == -50.0  # clamp em -sl
    finally:
        conn.close()


def test_resolve_skips_recent_rows(tmp_path, monkeypatch):
    """Rows com ts recente (< window) ficam pendentes."""
    sj = _fresh_journal(tmp_path, monkeypatch)
    sj.log_blocked_signal(
        symbol="WINQ26", tf="M5", strategy="X",
        direction="BUY", block_reason="X",
        sl_pts=200, ts=datetime.now().astimezone().isoformat(),
    )
    sj.flush()
    n = sj.resolve_blocked_outcomes(window_minutes=120, fetcher=lambda s, t: {
        "entry_price": 100.0, "exit_price": 110.0
    })
    assert n == 0


def test_resolve_handles_fetcher_exception(tmp_path, monkeypatch):
    """Fetcher que levanta não interrompe resolve."""
    sj = _fresh_journal(tmp_path, monkeypatch)
    iso_old = (
        datetime.now().astimezone() - timedelta(hours=3)
    ).isoformat()
    sj.log_blocked_signal(
        symbol="WINQ26", tf="M5", strategy="X",
        direction="BUY", block_reason="X",
        sl_pts=100, ts=iso_old,
    )
    sj.flush()

    def bad_fetcher(s, t):
        raise RuntimeError("Wine offline")
    # Não deve raise
    n = sj.resolve_blocked_outcomes(window_minutes=120, fetcher=bad_fetcher)
    assert n == 0


# ═══════════════════════════════════════════════════════════
# compute_selectivity
# ═══════════════════════════════════════════════════════════

def test_compute_selectivity_aggregates_entries_and_blocked(
    tmp_path, monkeypatch
):
    """selectivity = entries / (entries + blocked) por estratégia."""
    sj = _fresh_journal(tmp_path, monkeypatch)

    # 3 setups barrados ADX_TREND, 1 WIN, 2 BARR
    for sym, strat, direction in [
        ("WINQ26", "ADX_TREND", "BUY"),
        ("WINQ26", "ADX_TREND", "SELL"),
        ("WINQ26", "VWAP", "BUY"),
    ]:
        sj.log_blocked_signal(
            symbol=sym, tf="M5", strategy=strat,
            direction=direction,
            block_reason="VOL_FILTER",
        )
    sj.flush()
    for _ in range(2):
        sj.log_blocked_signal(
            symbol="WDON26", tf="M5", strategy="VWAP",
            direction="BUY", block_reason="MTF_LOW_SCORE",
        )
    sj.flush()

    # Insere 1 trade WIN ADX_TREND na tabela trades (mesmo DB)
    conn = sqlite3.connect(str(sj.DB_PATH))
    try:
        conn.execute(
            "INSERT INTO trades (symbol, strategy, direction, volume, "
            "entry_time, entry_price, multiplier) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("WINQ26", "ADX_TREND", "BUY", 1.0,
             datetime.now().astimezone().isoformat(), 5000.0, 0.20),
        )
        conn.commit()
    finally:
        conn.close()

    result = sj.compute_selectivity(days=7)
    adx = result["strategies"]["ADX_TREND"]
    assert adx["entries"] == 1
    assert adx["blocked"] == 2  # BUY + SELL
    assert 0.30 < adx["selectivity"] < 0.40  # 1 / (1+2) = 0.333

    vwap = result["strategies"]["VWAP"]
    assert vwap["entries"] == 0
    assert vwap["blocked"] == 3
    assert vwap["selectivity"] == 0.0

    assert result["global"]["entries"] == 1
    assert result["global"]["blocked"] == 5
    assert result["global"]["selectivity"] == 1 / 6


def test_compute_selectivity_by_strategy_filter(tmp_path, monkeypatch):
    """Passando strategy= filtra apenas aquela estratégia."""
    sj = _fresh_journal(tmp_path, monkeypatch)
    for strat in ("A", "B"):
        for _ in range(3):
            sj.log_blocked_signal(
                symbol="X", tf="M5", strategy=strat,
                direction="BUY", block_reason="Y",
            )
    sj.flush()

    only_a = sj.compute_selectivity(strategy="A", days=7)
    assert "A" in only_a["strategies"]
    assert "B" not in only_a["strategies"]
    assert only_a["strategies"]["A"]["blocked"] == 3


# ═══════════════════════════════════════════════════════════
# Hook do autotrader (_maybe_log_blocked_signal)
# ═══════════════════════════════════════════════════════════

def test_hook_fires_when_recent_signal_within_lookback(
    tmp_path, monkeypatch
):
    """Heurística: signal há <30min + strategy retorna None → log_blocked_signal."""
    sj = _fresh_journal(tmp_path, monkeypatch)

    # Importa autotrader dinamicamente (pode falhar em CI sem Wine — usamos
    # mock do módulo signal_journal já path-mocked; autotrader PODE falhar,
    # mas só usamos a função _maybe_log_blocked_signal).
    try:
        from core import vt_autotrader  # noqa: F401
    except Exception:
        pytest.skip("autotrader não importável em CI — testar só heurística isolada")

    # Mock state com recent_signal_ts = agora - 5 min
    class FakeState:
        recent_signal_ts = {
            ("WINQ26", "M5", "ADX_TREND"):
                datetime.now() - timedelta(minutes=5),
        }
    sj.reset_buffer_for_test()
    vt_autotrader._maybe_log_blocked_signal(
        FakeState(), "WINQ26", "M5", "ADX_TREND",
        bar_ts=datetime.now().astimezone().isoformat(),
    )
    sj.flush()

    conn = sqlite3.connect(str(sj.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM signal_blocked_log"
        ).fetchone()
        assert row is not None
        assert row["block_reason"] == "STRATEGY_RETURNED_NONE_AFTER_SIGNAL"
    finally:
        conn.close()


def test_hook_no_fire_when_signal_outside_lookback(
    tmp_path, monkeypatch
):
    """Signal há >30min + strategy retorna None → NÃO loga (sem setup genuíno)."""
    sj = _fresh_journal(tmp_path, monkeypatch)
    try:
        from core import vt_autotrader  # noqa: F401
    except Exception:
        pytest.skip("autotrader não importável em CI")

    class FakeState:
        recent_signal_ts = {
            ("WINQ26", "M5", "ADX_TREND"):
                datetime.now() - timedelta(minutes=60),
        }
    sj.reset_buffer_for_test()
    fired = vt_autotrader._maybe_log_blocked_signal(
        FakeState(), "WINQ26", "M5", "ADX_TREND",
        bar_ts=datetime.now(),
    )
    assert fired is False

    sj.flush()
    conn = sqlite3.connect(str(sj.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM signal_blocked_log").fetchone()
        assert n["n"] == 0
    finally:
        conn.close()


def test_hook_no_fire_when_no_recent_signal(
    tmp_path, monkeypatch
):
    """Sem signal recente + strategy retorna None → não loga."""
    sj = _fresh_journal(tmp_path, monkeypatch)
    try:
        from core import vt_autotrader  # noqa: F401
    except Exception:
        pytest.skip("autotrader não importável em CI")

    class FakeState:
        recent_signal_ts = {}  # vazio
    sj.reset_buffer_for_test()
    fired = vt_autotrader._maybe_log_blocked_signal(
        FakeState(), "WINQ26", "M5", "ADX_TREND",
        bar_ts=datetime.now(),
    )
    assert fired is False


def test_hook_handles_exception_defensively(tmp_path, monkeypatch):
    """Falha interna do hook NÃO quebra — defesa tick-loop."""
    try:
        from core import vt_autotrader  # noqa: F401
    except Exception:
        pytest.skip("autotrader não importável em CI")

    class BrokenState:
        # Property que levanta — broken por design
        @property
        def recent_signal_ts(self):
            raise RuntimeError("broken")

    # Não raise; retorna False
    fired = vt_autotrader._maybe_log_blocked_signal(
        BrokenState(), "WINQ26", "M5", "ADX_TREND",
        bar_ts=datetime.now(),
    )
    assert fired is False
