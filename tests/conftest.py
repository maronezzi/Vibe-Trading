"""
Pytest config — injeta o root do projeto e o diretório core/ no sys.path
para que os testes consigam fazer `from core.vt_autotrader import ...`,
`from agi_tuning_17h import ...`, etc.

Mesmo padrão aplicado em monitoring/vt_daily_report.py (17/06/2026)
para corrigir ModuleNotFoundError: No module named 'vt_hermes_helper'.

Runs before any test collection.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CORE_DIR = _PROJECT_ROOT / "core"
_AGI_DIR = _PROJECT_ROOT / "optimization"  # agi_tuning_17h.py lives here
_MT5_DIR = _PROJECT_ROOT / "mt5"  # mt5_error_recovery.py lives here
_MONITORING_DIR = _PROJECT_ROOT / "monitoring"  # vt_analyst.py lives here

# Idempotente: não duplica entradas
for p in (str(_PROJECT_ROOT), str(_CORE_DIR), str(_AGI_DIR), str(_MT5_DIR), str(_MONITORING_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ─── Isolamento do config de produção (2026-06-23) ─────────────────────────
# Por padrão, TODO teste roda com CONFIG_PATH redirecionado para uma cópia
# temporária do vt_config.json, e o cache do loader (_config/_mtime) é zerado.
# Assim qualquer save_full_config/save_params durante os testes vai para o tmp,
# e o config de PRODUÇÃO nunca pode ser corrompido pelo pytest (bug histórico:
# o test_agi_memo fazia backup/restaura do config real, e quando outro teste
# corrompia o config entre setUp e tearDown, o estrago era propagado).
#
# O cache é zerado ANTES e DEPOIS de cada teste para evitar snapshot stale.
# Testes que PRECISAM do config real de fato (caso raro) podem optar out:
#     @pytest.mark.uses_real_config


@pytest.fixture(autouse=True)
def _isolate_vt_config(request, monkeypatch, tmp_path):
    """Redireciona vt_config_loader.CONFIG_PATH para tmp por padrão (fail-safe)."""
    if request.node.get_closest_marker("uses_real_config"):
        return

    import vt_config_loader

    real_path = vt_config_loader.CONFIG_PATH
    tmp_cfg = tmp_path / "vt_config_test.json"

    # Snapshot do config real (se existir) para o tmp.
    if real_path.exists():
        try:
            data = json.loads(real_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    else:
        data = {}

    # Garantir as chaves mínimas exigidas pela validação de load_config()
    # (symbols/strategy/wdo/win) para que o tmp sempre carregue, independente
    # do estado — eventualmente incompleto — do config real.
    data.setdefault("symbols", ["WIN", "WDO", "BIT", "WSP"])
    data.setdefault("strategy", {})
    data.setdefault("wdo", {})
    data.setdefault("win", {})

    tmp_cfg.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    # Redireciona path + zera cache do loader ANTES do teste.
    monkeypatch.setattr(vt_config_loader, "CONFIG_PATH", tmp_cfg)
    monkeypatch.setattr(vt_config_loader, "_config", None)
    monkeypatch.setattr(vt_config_loader, "_mtime", 0)

    yield

    # Zera cache DEPOIS para o próximo teste reler do path real (revertido
    # automaticamente pelo monkeypatch).
    vt_config_loader._config = None
    vt_config_loader._mtime = 0


# ─── Isolamento do DB de trades (2026-07-01) ───────────────────────────────
# BUG HISTÓRICO (commit dc447fd6): mt5_orchestrator._persist_close_to_db()
# escreve em TRADES_DB = PROJECT / "vt_trades.db" (path de PRODUÇÃO). Sem
# isolamento autouse no conftest, qualquer teste que chamasse close() vazava
# trades fake no DB de produção — exemplo real: test_orchestrator_close_
# updates_db.py criou o trade #2072 fake (ticket=2467899999, PnL=+R$ 200)
# que teve que ser removido manualmente.
#
# FIX: monkeypatch de TRADES_DB no módulo mt5_orchestrator para um tmp DB
# com schema mínimo (espelha _TRADES_SCHEMA do orchestrator). Como o patch
# é revertido automaticamente pelo monkeypatch ao final do teste, o path
# de produção nunca é tocado. Testes que PRECISAM do DB real (caso raro)
# podem optar out com @pytest.mark.uses_real_db.
#
# NOTA: tests/test_orchestrator_close_updates_db.py já tinha um _TmpDBMixin
# próprio, mas como o problema raiz é o orchestrator, mover o isolamento
# para o conftest torna o fix FAIL-SAFE — qualquer teste novo que chamar
# close() também fica protegido automaticamente.


@pytest.fixture(autouse=True)
def _isolate_trades_db(request, monkeypatch, tmp_path):
    """Redireciona mt5_orchestrator.TRADES_DB para tmp por padrão (fail-safe)."""
    if request.node.get_closest_marker("uses_real_db"):
        return

    from mt5 import mt5_orchestrator

    # tmp DB isolado por teste (tmp_path é único por teste)
    tmp_db = tmp_path / "vt_trades.db"

    # Schema mínimo — espelha _TRADES_SCHEMA de mt5_orchestrator.py.
    # Idempotente: testes que sobrescrevem (close já faz CREATE IF NOT EXISTS)
    # continuam funcionando.
    _TRADES_SCHEMA = """
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
    CREATE INDEX IF NOT EXISTS idx_trades_entry_ticket ON trades(entry_ticket);

    -- Wave N+1 (2026-07-08): table espelhada para isolar testes que usam
    -- core/vt_signal_journal.py (mesmo path de DB).
    CREATE TABLE IF NOT EXISTS signal_blocked_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        symbol TEXT NOT NULL,
        tf TEXT NOT NULL,
        strategy TEXT NOT NULL,
        direction TEXT,
        block_reason TEXT NOT NULL,
        hypothetical_sl_pts INTEGER,
        hypothetical_atr_pts REAL,
        regime TEXT,
        resolved INTEGER DEFAULT 0,
        outcome_win INTEGER,
        outcome_pnl_pts REAL,
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        UNIQUE(ts, symbol, tf, direction, strategy)
    );
    CREATE INDEX IF NOT EXISTS idx_blocked_sym_tf_strat_ts
        ON signal_blocked_log(symbol, tf, strategy, ts);
    CREATE INDEX IF NOT EXISTS idx_blocked_resolved_ts
        ON signal_blocked_log(resolved, ts);
    """
    conn = sqlite3.connect(str(tmp_db), timeout=30.0)
    conn.executescript(_TRADES_SCHEMA)
    conn.commit()
    conn.close()

    # Monkeypatch: redireciona TRADES_DB no módulo orchestrator.
    # monkeypatch reverte automaticamente ao final do teste — produção intocado.
    monkeypatch.setattr(mt5_orchestrator, "TRADES_DB", tmp_db)

    # Wave N+1 (2026-07-08): mesmo path para o vt_signal_journal (mesma DB).
    try:
        from core import vt_signal_journal
        monkeypatch.setattr(vt_signal_journal, "DB_PATH", tmp_db)
        vt_signal_journal.reset_buffer_for_test()
    except ImportError:
        pass  # módulo não instalado (sub-conjunto de testes) — skip

    # Wave 875 (Bruno 10/07): patchar também o DB_PATH do watchdog
    # para que tests de reconciliation/watchdog usem tmp_db em vez do
    # DB de produção. Sem isso, test_watchdog_excludes_excluded_trades
    # dependeria de poluição no DB real (anti-pattern).
    try:
        from monitoring import vt_trade_watchdog
        monkeypatch.setattr(vt_trade_watchdog, "DB_PATH", tmp_db)
    except ImportError:
        pass  # monitoring não instalado — skip

    # Wave 875+1 (Bruno 10/07): patchar também vt_trade_log.DB_PATH.
    # BUG HISTÓRICO: tests que chamam _execute_entry() (autotrader) executam
    # log_entry() (de vt_trade_log.py:217), que usa DB_PATH hardcoded para o
    # arquivo de produção. Sem este patch, test_autotrader_order_tracker_integration
    # etc. poluem o vt_trades.db real com trades fake (ticket=12345/99999 etc).
    # Comprovado: rodar 1× o test_filled_valid_ticket_opens_position adicionou
    # 1 trade real com ticket=12345 ao DB de produção (4→5).
    # FIX: redirecionar vt_trade_log.DB_PATH para tmp_db.
    try:
        from core import vt_trade_log
        monkeypatch.setattr(vt_trade_log, "DB_PATH", tmp_db)
    except ImportError:
        pass  # core não instalado — skip

    yield

    # Sem cleanup manual necessário — tmp_path é auto-cleaned pelo pytest
    # e monkeypatch reverte o atributo. Mas garantimos que o schema/registros
    # não vazem entre testes (cada teste ganha tmp_path NOVO, então já estão
    # isolados por construção).
