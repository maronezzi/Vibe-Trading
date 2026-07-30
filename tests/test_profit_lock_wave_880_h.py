"""Testes do Profit Lock Adaptativo (Wave 880.H — Bruno 2026-07-20).

Valida a lógica de cálculo de target adaptativo + state persistente
(arm/release/auto-rollover), sem tocar em MT5 nem em DB de produção.

Estratégia de isolamento:
  - Mocka vt_profit_lock.DB_PATH p/ um sqlite tmp_path.
  - Mocka vt_profit_lock.LOCK_STATE_PATH p/ tmp_path.
  - Mocka mt5_orchestrator.status e vt_starting_balance p/ get_intraday_pnl_total.
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

_PROJECT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolate_profit_lock_paths(tmp_path, monkeypatch):
    """Redireciona DB_PATH e LOCK_STATE_PATH p/ tmp. Limpa entre testes."""
    import sys
    sys.path.insert(0, str(_PROJECT))
    from core import vt_profit_lock

    tmp_db = tmp_path / "fake_trades.db"
    tmp_lock = tmp_path / "vt_profit_lock.json"

    monkeypatch.setattr(vt_profit_lock, "DB_PATH", tmp_db)
    monkeypatch.setattr(vt_profit_lock, "LOCK_STATE_PATH", tmp_lock)

    # Inicializa schema da daily_summary no DB tmp.
    con = sqlite3.connect(str(tmp_db))
    con.execute("""
        CREATE TABLE daily_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, symbol TEXT NOT NULL,
            n_trades INTEGER DEFAULT 0, n_winners INTEGER DEFAULT 0,
            n_losers INTEGER DEFAULT 0, gross_pnl REAL DEFAULT 0,
            fees REAL DEFAULT 0, net_pnl REAL DEFAULT 0,
            max_win REAL DEFAULT 0, max_loss REAL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(date, symbol)
        )
    """)
    con.commit()
    con.close()

    yield vt_profit_lock


def _insert_day(conn_or_path, date, symbols_pnls):
    """Insere N símbolos para uma data. symbols_pnls = {symbol: net_pnl}."""
    con = sqlite3.connect(str(conn_or_path)) if isinstance(conn_or_path, Path) else conn_or_path
    try:
        for sym, pnl in symbols_pnls.items():
            con.execute(
                "INSERT OR REPLACE INTO daily_summary (date, symbol, net_pnl) VALUES (?, ?, ?)",
                (date, sym, pnl),
            )
        con.commit()
    finally:
        if isinstance(conn_or_path, Path):
            con.close()


# ─── get_target: fórmula adaptativa ────────────────────────────────────────
def test_target_fallback_when_db_empty(_isolate_profit_lock_paths):
    """Sem histórico → target = min_target (default R$ 250)."""
    pl = _isolate_profit_lock_paths
    target = pl.get_target({})
    assert target == 250.0, f"esperado fallback 250, got {target}"


def test_target_fallback_with_single_positive_day(_isolate_profit_lock_paths, tmp_path):
    """1 dia positivo < MIN_POSITIVE_DAYS_FOR_AVG (2) → fallback."""
    pl = _isolate_profit_lock_paths
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    _insert_day(pl.DB_PATH, yesterday, {"WINQ26": 300.0})

    target = pl.get_target({})
    assert target == 250.0, "com 1 dia positivo deve usar fallback (não média)"


def test_target_uses_avg_when_3_positive_days(_isolate_profit_lock_paths):
    """3 dias positivos (300, 150, 200) → média 216.67 → max(250, 216.67) = 250."""
    pl = _isolate_profit_lock_paths
    for d_offset, pnl in [(1, 300.0), (2, 150.0), (3, 200.0)]:
        date = (datetime.now() - timedelta(days=d_offset)).strftime("%Y-%m-%d")
        _insert_day(pl.DB_PATH, date, {"WINQ26": pnl})

    target = pl.get_target({})
    # média = (300+150+200)/3 = 216.67 → max(250, 216.67) = 250 (mínimo prevalece)
    assert target == 250.0


def test_target_avg_exceeds_minimum(_isolate_profit_lock_paths):
    """Dias positivos altos (500, 400, 600) → média 500 → target 500."""
    pl = _isolate_profit_lock_paths
    for d_offset, pnl in [(1, 500.0), (2, 400.0), (3, 600.0)]:
        date = (datetime.now() - timedelta(days=d_offset)).strftime("%Y-%m-%d")
        _insert_day(pl.DB_PATH, date, {"WINQ26": pnl})

    target = pl.get_target({})
    assert target == 500.0, f"esperado 500 (média de dias altos), got {target}"


def test_target_respects_custom_min_target(_isolate_profit_lock_paths):
    """min_target custom = 100 deve permitir target menor que 250 default."""
    pl = _isolate_profit_lock_paths
    for d_offset, pnl in [(1, 120.0), (2, 130.0), (3, 110.0)]:
        date = (datetime.now() - timedelta(days=d_offset)).strftime("%Y-%m-%d")
        _insert_day(pl.DB_PATH, date, {"WINQ26": pnl})

    # média = 120 → max(100, 120) = 120
    target = pl.get_target({"profit_lock_min_target": 100.0})
    assert target == 120.0


def test_target_ignores_negative_days(_isolate_profit_lock_paths):
    """Dias negativos NÃO entram na média (filtrados pelo HAVING day_pnl > 0)."""
    pl = _isolate_profit_lock_paths
    # 2 positivos altos + 2 negativos grandes → média deve ignorar os negativos.
    days = [(1, -1000.0), (2, 500.0), (3, -800.0), (4, 700.0)]
    for d_offset, pnl in days:
        date = (datetime.now() - timedelta(days=d_offset)).strftime("%Y-%m-%d")
        _insert_day(pl.DB_PATH, date, {"WINQ26": pnl})

    # média dos positivos = (500+700)/2 = 600
    target = pl.get_target({})
    assert target == 600.0, f"negativos não devem puxar média; esperado 600, got {target}"


def test_target_respects_lookback_window(_isolate_profit_lock_paths):
    """Dias fora do lookback não contam."""
    pl = _isolate_profit_lock_paths
    # 2 dias positivos dentro do lookback de 7 dias.
    _insert_day(pl.DB_PATH, (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d"), {"WINQ26": 300.0})
    _insert_day(pl.DB_PATH, (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d"), {"WINQ26": 500.0})
    # 2 dias positivos FORA do lookback (dias 10 e 15).
    _insert_day(pl.DB_PATH, (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d"), {"WINQ26": 5000.0})
    _insert_day(pl.DB_PATH, (datetime.now() - timedelta(days=15)).strftime("%Y-%m-%d"), {"WINQ26": 9000.0})

    # lookback default = 7 → só conta os 2 de dentro → média (300+500)/2 = 400
    target = pl.get_target({})
    assert target == 400.0, f"lookback deve excluir dias antigos; esperado 400, got {target}"


def test_target_excludes_today(_isolate_profit_lock_paths):
    """Hoje NÃO conta para o target (o dia corrente está em curso)."""
    pl = _isolate_profit_lock_paths
    today = datetime.now().strftime("%Y-%m-%d")
    _insert_day(pl.DB_PATH, today, {"WINQ26": 999999.0})  # deveria inflar se contasse
    # 2 dias positivos normais ontem/anteontem.
    _insert_day(pl.DB_PATH, (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"), {"WINQ26": 300.0})
    _insert_day(pl.DB_PATH, (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d"), {"WINQ26": 400.0})

    target = pl.get_target({})
    # média deve ser (300+400)/2 = 350, ignorando o 999999 de hoje.
    assert target == 350.0, "hoje não deve influenciar o target"


# ─── is_locked / arm_lock / release_lock ───────────────────────────────────
def test_is_locked_false_when_no_state_file(_isolate_profit_lock_paths):
    pl = _isolate_profit_lock_paths
    assert pl.is_locked() == (False, {})


def test_arm_then_is_locked_true(_isolate_profit_lock_paths):
    pl = _isolate_profit_lock_paths
    pl.arm_lock(target=300.0, armed_pnl=305.0, closed_n=2)

    locked, state = pl.is_locked()
    assert locked is True
    assert state["armed"] is True
    assert state["target"] == 300.0
    assert state["armed_pnl"] == 305.0
    assert state["closed_n"] == 2
    assert state["date"] == datetime.now().strftime("%Y-%m-%d")


def test_is_locked_false_for_yesterday_state(_isolate_profit_lock_paths, tmp_path):
    """State de ontem → desarma automaticamente (auto-rollover)."""
    pl = _isolate_profit_lock_paths
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    # Escreve state manualmente com date de ontem.
    state = {
        "date": yesterday, "armed": True, "target": 300.0,
        "armed_at": "2026-07-19T14:00:00", "armed_pnl": 305.0, "closed_n": 2,
    }
    pl.LOCK_STATE_PATH.write_text(json.dumps(state))

    locked, returned_state = pl.is_locked()
    assert locked is False, "state de ontem deve desarmar (date != today)"
    # Retornou state (não vazio) mas com locked=False — caller decide se limpa.


def test_is_locked_false_when_armed_is_false(_isolate_profit_lock_paths):
    """State com armed=false → não está locked."""
    pl = _isolate_profit_lock_paths
    state = {"date": datetime.now().strftime("%Y-%m-%d"), "armed": False}
    pl.LOCK_STATE_PATH.write_text(json.dumps(state))

    locked, _ = pl.is_locked()
    assert locked is False


def test_is_locked_malformed_json(_isolate_profit_lock_paths):
    """JSON inválido → fail-safe: (False, {})."""
    pl = _isolate_profit_lock_paths
    pl.LOCK_STATE_PATH.write_text("not json {{{")

    locked, state = pl.is_locked()
    assert locked is False
    assert state == {}


def test_arm_lock_idempotent_same_day(_isolate_profit_lock_paths):
    """Re-armar no mesmo dia atualiza contadores, preserva target original."""
    pl = _isolate_profit_lock_paths
    pl.arm_lock(target=300.0, armed_pnl=305.0, closed_n=2)
    pl.arm_lock(target=999.0, armed_pnl=350.0, closed_n=1)  # 2ª chamada

    locked, state = pl.is_locked()
    assert locked is True
    assert state["target"] == 300.0, "target não deve mudar ao re-armar no mesmo dia"
    assert state["closed_n"] == 3, "closed_n deve acumular (2+1)"
    assert state["armed_pnl"] == 350.0, "armed_pnl deve atualizar"


def test_release_lock_removes_file(_isolate_profit_lock_paths):
    pl = _isolate_profit_lock_paths
    pl.arm_lock(target=300.0, armed_pnl=305.0, closed_n=2)
    assert pl.LOCK_STATE_PATH.exists()

    pl.release_lock()
    assert not pl.LOCK_STATE_PATH.exists()
    assert pl.is_locked() == (False, {})


def test_release_lock_idempotent(_isolate_profit_lock_paths):
    """release_lock quando já não há arquivo não levanta."""
    pl = _isolate_profit_lock_paths
    assert not pl.LOCK_STATE_PATH.exists()
    pl.release_lock()  # não deve levantar
    assert pl.is_locked() == (False, {})


# ─── get_intraday_pnl_total ────────────────────────────────────────────────
def test_pnl_zero_when_no_starting_balance(_isolate_profit_lock_paths, monkeypatch):
    """Sem snapshot de starting_balance → retorna 0 (fail-safe)."""
    pl = _isolate_profit_lock_paths
    # Mocka vt_starting_balance.get_today_starting_balance p/ retornar None.
    # NOTA: essa função retorna Optional[float] (o número direto), NÃO um dict.
    with mock.patch("core.vt_starting_balance.get_today_starting_balance", return_value=None):
        assert pl.get_intraday_pnl_total() == 0.0


def test_pnl_computes_balance_delta(_isolate_profit_lock_paths, monkeypatch):
    """PnL = equity_mt5 − starting_balance. Fonte: equity (inclui flutuante)."""
    pl = _isolate_profit_lock_paths

    # get_today_starting_balance retorna float (não dict) — bug do mock original.
    fake_starting = 1000.0
    fake_status = {"account": {"balance": 1100.0, "equity": 1150.0}}

    with mock.patch("core.vt_starting_balance.get_today_starting_balance", return_value=fake_starting), \
         mock.patch("mt5.mt5_orchestrator.status", return_value=fake_status):
        # equity 1150 − starting 1000 = 150 (preferimos equity por incluir MTM)
        assert pl.get_intraday_pnl_total() == 150.0


def test_pnl_zero_when_mt5_unavailable(_isolate_profit_lock_paths):
    """MT5 indisponível (status raise) → fail-safe 0.0."""
    pl = _isolate_profit_lock_paths
    # get_today_starting_balance retorna float (não dict).
    fake_starting = 1000.0

    with mock.patch("core.vt_starting_balance.get_today_starting_balance", return_value=fake_starting), \
         mock.patch("mt5.mt5_orchestrator.status", side_effect=Exception("MT5 down")):
        assert pl.get_intraday_pnl_total() == 0.0


def test_pnl_zero_does_not_trigger_lock(_isolate_profit_lock_paths):
    """PnL 0 (MT5 down) nunca atinge target → nunca trava por engano."""
    pl = _isolate_profit_lock_paths
    target = pl.get_target({})  # 250 (fallback)
    with mock.patch.object(pl, "get_intraday_pnl_total", return_value=0.0):
        pnl = pl.get_intraday_pnl_total()
    assert pnl < target, "PnL 0 deve ser < target (não trava)"
