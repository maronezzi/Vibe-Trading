"""
test_watchdog_truth_layer.py
============================
FASE 4 do refactor Vibe-Trading (data/architecture_proposal_2026_07_01.md,
secao 4.4): watchdog integrado com truth layer pra detectar drift > R$ 5
entre PnL MT5 (broker-truth) e PnL DB local.

Por que este teste importa (regressao historica):
  - Antes da Fase 4, o PnL diario do bot vinha 100% do DB local
    (SELECT SUM(net_pnl) FROM trades). Isso eh fragil: trades com
    exit_time gravado com PnL=0 (GHOST/ORPHAN do orchestrator) nao
    contribuam pra receita real, mas o DB acha que contribuem. O
    broker (MT5 history) ja tem o PnL real calculado.
  - Solucao: truth layer (`core/vt_truth.py`) centraliza leitura MT5.
    Watchdog agora compara MT5-truth vs DB e alerta se drift > R$ 5/dia
    (limite de ruido operacional: comissao, swap residual).

ESTES TESTES (RED -> GREEN):
- DRIFT_THRESHOLD_REAIS constante = Decimal("5.00")
- get_db_daily_pnl() le net_pnl do DB local, fail-safe
- get_mt5_daily_pnl_truth() delega para vt_truth.get_daily_pnl()
- compute_pnl_drift() retorna dict com mt5_pnl/db_pnl/drift/drift_alert
- format_drift_alert() formata para Telegram
- run_watchdog() inclui mt5_pnl/db_pnl/drift/drift_alert no status JSON
- Status JSON marca ok=False quando drift_alert=True
- Quando MT5 indisponivel, compute_pnl_drift() ainda retorna (com zeros)
"""
import importlib.util
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path("/home/bruno/Projects/Vibe-Trading")
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "core"))


# ===== HELPERS =====

def _load_watchdog_module():
    """Carrega monitoring/vt_trade_watchdog.py como modulo isolado.

    Mesmo padrao usado em test_watchdog_pending_notifications.py e
    test_watchdog_uses_truth_helper.py: evita efeitos colaterais de
    import direto (ex: modulo ja em sys.modules com state antigo).
    """
    spec = importlib.util.spec_from_file_location(
        "_watchdog_truth_layer_test",
        PROJECT_ROOT / "monitoring" / "vt_trade_watchdog.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_trades_db(tmp_path: Path, trades: list) -> Path:
    """Cria um SQLite com a tabela `trades` e popula com trades fornecidos.

    Cada trade eh dict com chaves: entry_time, exit_time, net_pnl.

    Nota: tmp_path no pytest ja vem com `vt_trades.db` criado pelo fixture
    autouse _isolate_trades_db do conftest (com schema do orchestrator).
    Usamos nome distinto pra evitar conflito de schema entre os dois.
    """
    db = tmp_path / "vt_watchdog_truth_layer_test.db"
    conn = sqlite3.connect(str(db), timeout=5.0)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_time TEXT NOT NULL,
            exit_time TEXT,
            net_pnl REAL DEFAULT 0
        );
    """)
    for t in trades:
        conn.execute(
            "INSERT INTO trades (entry_time, exit_time, net_pnl) VALUES (?, ?, ?)",
            (t["entry_time"], t.get("exit_time"), t.get("net_pnl", 0.0)),
        )
    conn.commit()
    conn.close()
    return db


# ===== TESTES =====

def test_watchdog_imports_truth_layer():
    """O modulo watchdog deve importar vt_truth (truth layer centralizado)."""
    module = _load_watchdog_module()
    assert hasattr(module, "vt_truth"), (
        "monitoring/vt_trade_watchdog.py deve importar `from core import vt_truth` "
        "para acessar PnL MT5-truth (Fase 4, architecture_proposal 4.4)."
    )
    # O modulo importado deve ser o mesmo da core
    from core import vt_truth as core_truth
    assert module.vt_truth is core_truth


def test_watchdog_uses_mt5_truth_for_daily_pnl():
    """get_mt5_daily_pnl_truth() deve delegar para vt_truth.get_daily_pnl()."""
    module = _load_watchdog_module()
    with patch.object(module.vt_truth, "get_daily_pnl", return_value=Decimal("123.45")) as mock_get:
        result = module.get_mt5_daily_pnl_truth(date_iso="2026-07-01")
    assert result == Decimal("123.45"), f"esperado Decimal('123.45'), got {result}"
    mock_get.assert_called_once_with(date_iso="2026-07-01")


def test_watchdog_no_drift_when_mt5_equals_db():
    """compute_pnl_drift() retorna drift=0 quando MT5 == DB."""
    module = _load_watchdog_module()
    with patch.object(module, "get_mt5_daily_pnl_truth", return_value=Decimal("100.00")), \
         patch.object(module, "get_db_daily_pnl", return_value=Decimal("100.00")):
        info = module.compute_pnl_drift(date_iso="2026-07-01")

    assert info["mt5_pnl"] == Decimal("100.00")
    assert info["db_pnl"] == Decimal("100.00")
    assert info["drift"] == Decimal("0.00")
    assert info["drift_alert"] is False
    assert info["date_iso"] == "2026-07-01"
    assert info["source"] == "TRUTH_LAYER"


def test_watchdog_drift_above_threshold_alerts():
    """Drift > R$ 5 -> drift_alert=True (dessincronizacao real)."""
    module = _load_watchdog_module()
    # MT5 = +150, DB = +100 -> drift = R$ 50 (acima do limite de R$ 5)
    with patch.object(module, "get_mt5_daily_pnl_truth", return_value=Decimal("150.00")), \
         patch.object(module, "get_db_daily_pnl", return_value=Decimal("100.00")):
        info = module.compute_pnl_drift(date_iso="2026-07-01")

    assert info["drift"] == Decimal("50.00")
    assert info["drift_alert"] is True
    assert info["threshold"] == Decimal("5.00")


def test_watchdog_drift_negative_direction_also_alerts():
    """Drift eh valor absoluto: |mt5 - db|, entao sinais opostos tbm alertam."""
    module = _load_watchdog_module()
    # MT5 = -100, DB = +50 -> drift = R$ 150
    with patch.object(module, "get_mt5_daily_pnl_truth", return_value=Decimal("-100.00")), \
         patch.object(module, "get_db_daily_pnl", return_value=Decimal("50.00")):
        info = module.compute_pnl_drift(date_iso="2026-07-01")

    assert info["drift"] == Decimal("150.00")
    assert info["drift_alert"] is True


def test_watchdog_no_alert_below_threshold():
    """Drift <= R$ 5 -> drift_alert=False (ruido operacional aceitavel)."""
    module = _load_watchdog_module()
    # MT5 = 102, DB = 100 -> drift = R$ 2 (abaixo do limite)
    with patch.object(module, "get_mt5_daily_pnl_truth", return_value=Decimal("102.00")), \
         patch.object(module, "get_db_daily_pnl", return_value=Decimal("100.00")):
        info = module.compute_pnl_drift(date_iso="2026-07-01")

    assert info["drift"] == Decimal("2.00")
    assert info["drift_alert"] is False


def test_watchdog_drift_threshold_constant():
    """DRIFT_THRESHOLD_REAIS = Decimal('5.00') — limite explicito."""
    module = _load_watchdog_module()
    assert hasattr(module, "DRIFT_THRESHOLD_REAIS"), (
        "watchdog deve expor constante DRIFT_THRESHOLD_REAIS."
    )
    assert module.DRIFT_THRESHOLD_REAIS == Decimal("5.00"), (
        f"limite esperado Decimal('5.00'), got {module.DRIFT_THRESHOLD_REAIS}"
    )


def test_watchdog_falls_back_to_zero_when_mt5_unavailable():
    """Se MT5-truth levanta excecao, get_mt5_daily_pnl_truth retorna 0.00 (fail-safe)."""
    module = _load_watchdog_module()
    with patch.object(module.vt_truth, "get_daily_pnl", side_effect=RuntimeError("wine down")):
        result = module.get_mt5_daily_pnl_truth(date_iso="2026-07-01")
    assert result == Decimal("0.00"), (
        f"esperado Decimal('0.00') em fallback, got {result}"
    )


def test_watchdog_format_drift_alert_includes_values():
    """format_drift_alert() produz string Telegram com mt5/db/diff/limite."""
    module = _load_watchdog_module()
    drift_info = {
        "mt5_pnl": Decimal("150.00"),
        "db_pnl": Decimal("100.00"),
        "drift": Decimal("50.00"),
        "drift_alert": True,
        "threshold": Decimal("5.00"),
        "date_iso": "2026-07-01",
        "source": "TRUTH_LAYER",
    }
    msg = module.format_drift_alert(drift_info)

    assert "DRIFT" in msg, "mensagem deve mencionar DRIFT"
    assert "150" in msg, "mensagem deve incluir MT5 PnL"
    assert "100" in msg, "mensagem deve incluir DB PnL"
    assert "50" in msg, "mensagem deve incluir drift (diff)"
    assert "5.00" in msg, "mensagem deve incluir threshold"


def test_watchdog_status_json_includes_mt5_pnl_db_pnl_drift(tmp_path, monkeypatch):
    """run_watchdog() deve incluir mt5_pnl/db_pnl/drift no status retornado."""
    module = _load_watchdog_module()

    # Isola DB_PATH para tmp (evita tocar vt_trades.db de producao)
    tmp_db = tmp_path / "vt_trades.db"
    _make_trades_db(tmp_path, [{"entry_time": "2026-07-01 10:00:00", "exit_time": "2026-07-01 11:00:00", "net_pnl": 100.0}])
    monkeypatch.setattr(module, "DB_PATH", tmp_db)

    # Mocka tudo que run_watchdog() chama para nao depender de MT5 real
    fake_truth = {
        "balance": 1000000.0, "equity": 1000000.0, "margin_free": 1000000.0,
        "positions_open": [], "n_positions": 0, "pnl_flutuante": 0.0,
        "ts": "2026-07-01T14:00:00", "ok": True, "error": None,
    }
    with patch.object(module, "get_truth_from_mt5", return_value=fake_truth, create=True), \
         patch.object(module, "get_bot_positions", return_value={}), \
         patch.object(module, "find_discrepancies", return_value=([], [], [])), \
         patch.object(module, "check_account", return_value=(1000000.0, 1000000.0, 1000000.0, [])), \
         patch.object(module, "check_trade_log", return_value=[]), \
         patch.object(module, "save_status", return_value=None), \
         patch.object(module, "notify_telegram", return_value=None), \
         patch.object(module, "load_config", return_value={"resolved_symbols": {}, "magic": 555501}), \
         patch.object(module, "get_mt5_daily_pnl_truth", return_value=Decimal("150.00")), \
         patch.object(module, "get_db_daily_pnl", return_value=Decimal("100.00")), \
         patch("builtins.print"):
        status = module.run_watchdog(json_only=True)

    # Campos de drift presentes no status JSON
    assert "mt5_pnl" in status, "status deve ter campo mt5_pnl"
    assert "db_pnl" in status, "status deve ter campo db_pnl"
    assert "drift" in status, "status deve ter campo drift"
    assert "drift_threshold" in status, "status deve ter campo drift_threshold"
    assert "drift_date" in status, "status deve ter campo drift_date"
    assert "drift_source" in status, "status deve ter campo drift_source"

    # Valores consistentes
    assert status["mt5_pnl"] == Decimal("150.00")
    assert status["db_pnl"] == Decimal("100.00")
    assert status["drift"] == Decimal("50.00")
    assert status["drift_threshold"] == Decimal("5.00")
    assert status["drift_source"] == "TRUTH_LAYER"


def test_watchdog_status_includes_drift_alert_boolean():
    """status JSON deve ter drift_alert como bool (True quando drift > threshold)."""
    module = _load_watchdog_module()

    fake_truth = {
        "balance": 1000000.0, "equity": 1000000.0, "margin_free": 1000000.0,
        "positions_open": [], "n_positions": 0, "pnl_flutuante": 0.0,
        "ts": "2026-07-01T14:00:00", "ok": True, "error": None,
    }
    with patch.object(module, "get_truth_from_mt5", return_value=fake_truth, create=True), \
         patch.object(module, "get_bot_positions", return_value={}), \
         patch.object(module, "find_discrepancies", return_value=([], [], [])), \
         patch.object(module, "check_account", return_value=(1000000.0, 1000000.0, 1000000.0, [])), \
         patch.object(module, "check_trade_log", return_value=[]), \
         patch.object(module, "save_status", return_value=None), \
         patch.object(module, "notify_telegram", return_value=None), \
         patch.object(module, "load_config", return_value={"resolved_symbols": {}, "magic": 555501}), \
         patch.object(module, "get_mt5_daily_pnl_truth", return_value=Decimal("200.00")), \
         patch.object(module, "get_db_daily_pnl", return_value=Decimal("50.00")), \
         patch("builtins.print"):
        status = module.run_watchdog(json_only=True)

    assert "drift_alert" in status, "status deve ter campo drift_alert"
    assert isinstance(status["drift_alert"], bool), (
        f"drift_alert deve ser bool, got {type(status['drift_alert']).__name__}"
    )
    assert status["drift_alert"] is True, "drift=R$150 deve disparar alerta"


def test_watchdog_drift_count_in_status_when_alerting():
    """Quando drift_alert=True, status['ok'] deve ser False (watchdog reporta problema)."""
    module = _load_watchdog_module()

    fake_truth = {
        "balance": 1000000.0, "equity": 1000000.0, "margin_free": 1000000.0,
        "positions_open": [], "n_positions": 0, "pnl_flutuante": 0.0,
        "ts": "2026-07-01T14:00:00", "ok": True, "error": None,
    }
    with patch.object(module, "get_truth_from_mt5", return_value=fake_truth, create=True), \
         patch.object(module, "get_bot_positions", return_value={}), \
         patch.object(module, "find_discrepancies", return_value=([], [], [])), \
         patch.object(module, "check_account", return_value=(1000000.0, 1000000.0, 1000000.0, [])), \
         patch.object(module, "check_trade_log", return_value=[]), \
         patch.object(module, "save_status", return_value=None), \
         patch.object(module, "notify_telegram", return_value=None), \
         patch.object(module, "load_config", return_value={"resolved_symbols": {}, "magic": 555501}), \
         patch.object(module, "get_mt5_daily_pnl_truth", return_value=Decimal("300.00")), \
         patch.object(module, "get_db_daily_pnl", return_value=Decimal("100.00")), \
         patch("builtins.print"):
        status = module.run_watchdog(json_only=True)

    assert status["drift_alert"] is True
    assert status["ok"] is False, (
        "status['ok'] deve ser False quando drift_alert=True (problema reportado)"
    )


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
