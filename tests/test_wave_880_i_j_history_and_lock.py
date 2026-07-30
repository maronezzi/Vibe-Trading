"""Testes das Waves 880.I (position= plumbing) e 880.J (PROFIT_LOCK storm).

Wave 880.I (C-1/C-2): restaurado o parâmetro position= em mt5_orchestrator.history()
e mt5_executor.cmd_history(). O reconcile ghost (vt_autotrader.py:4003) dependia
disso — sem position=, levantava TypeError silenciosamente engolido pelo except
largo, deixando 20/20 GHOSTs sem PnL broker-truth.

Wave 880.J (D): PROFIT_LOCK usava lock_pts=-1 (1pt do entry) — sempre rejeitado
pelo MT5 por violar trade_stops_level. Falha não persistia entre ciclos, então
re-disparava a cada 30s, gerando storm de INVALID_STOPS.

Testes são estruturais (análise do source) + unitários em funções isoladas,
sem tocar em MT5/DB real.
"""
import inspect
from pathlib import Path
from unittest import mock

_PROJECT = Path(__file__).resolve().parent.parent


def _read(path: str) -> str:
    return (_PROJECT / path).read_text(encoding="utf-8")


# ─── Wave 880.I: position= plumbing ─────────────────────────────────────────
def test_orchestrator_history_accepts_position_param():
    """mt5_orchestrator.history() deve aceitar position= (restaurado Wave 880.I)."""
    import sys
    sys.path.insert(0, str(_PROJECT))
    try:
        # Importa o módulo — mt5_orchestrator não é host-coupled (só define funções).
        from mt5 import mt5_orchestrator
        sig = inspect.signature(mt5_orchestrator.history)
        assert "position" in sig.parameters, (
            "history() deve ter parâmetro position (Wave 880.I restaurou)"
        )
        assert sig.parameters["position"].default is None
    finally:
        sys.path.remove(str(_PROJECT))


def test_orchestrator_history_routes_position_to_run_wine():
    """Quando position= informado, chama _run_wine com ('history', ticket)."""
    src = _read("mt5/mt5_orchestrator.py")
    # Confirma a rota: if position: return _run_wine(EXECUTOR_WIN, "history", str(position), ...)
    assert "if position:" in src
    assert '_run_wine(EXECUTOR_WIN, "history", str(position)' in src


def test_executor_cmd_history_accepts_position_param():
    """cmd_history() no executor deve aceitar position=."""
    src = _read("mt5/mt5_executor.py")
    assert "def cmd_history(symbol=None, days=7, position=None)" in src, (
        "cmd_history deve ter parâmetro position (Wave 880.I)"
    )
    # Deve chamar mt5.history_deals_get(position=int(position)) no branch position.
    assert "mt5.history_deals_get(position=int(position))" in src


def test_executor_cli_dispatches_digit_arg_as_position():
    """CLI: se argv[2] é numérico, roteia p/ position (não symbol)."""
    src = _read("mt5/mt5_executor.py")
    assert "sym.isdigit()" in src, (
        "CLI dispatch deve detectar ticket numérico e rotear p/ position="
    )
    assert "cmd_history(position=sym)" in src


def test_truth_get_position_history_accepts_position():
    """vt_truth.get_position_history() deve repassar position= ao adapter."""
    src = _read("core/vt_truth.py")
    assert "position: Optional[str] = None" in src
    # Cache key deve incluir position (evita cache hit entre calls diferentes).
    assert "position or 'NOPOS'" in src


def test_truth_mt5_history_adapter_passes_position():
    """_mt5_history() deve repassar position= para _mt5_history_raw."""
    src = _read("core/vt_truth.py")
    assert "_mt5_history_raw(position=position)" in src


def test_reconcile_ghost_except_is_narrow():
    """Wave 880.I: except do reconcile ghost deve ser estreito (não Exception).

    Antes capturava Exception genérico, escondendo TypeError de signature drift.
    Agora captura só erros esperados do MT5/Wine.
    """
    src = _read("core/vt_autotrader.py")
    # Localiza o bloco do reconcile ghost.
    marker = "history(position=ticket_str)"
    idx = src.find(marker)
    assert idx != -1, "bloco do reconcile ghost não encontrado"
    # O except logo depois deve ser estreito.
    block_end = src.find("except", idx)
    assert block_end != -1
    except_line = src[block_end:block_end + 100]
    assert "Exception" not in except_line.split("\n")[0], (
        f"except ainda é largo: {except_line.split(chr(10))[0]}"
    )
    assert "OSError" in except_line or "ValueError" in except_line, (
        "except deve capturar erros esperados (OSError/ValueError/KeyError)"
    )


def test_sl_servidor_fallback_uses_position_when_ticket_available():
    """Wave 880.I: fallback SL_SERVIDOR deve passar position=entry_ticket."""
    src = _read("core/vt_autotrader.py")
    # Localiza o bloco de fallback SL_SERVIDOR (com _profit_source).
    marker = '_profit_source = "fallback local"'
    idx = src.find(marker)
    assert idx != -1
    block = src[idx:idx + 1200]
    assert "position=" in block, (
        "fallback SL_SERVIDOR deve usar position= quando entry_ticket disponível"
    )
    assert "_entry_ticket if _entry_ticket else None" in block


def test_close_all_and_report_iterates_tickets_for_history():
    """Wave 880.I: close_all_and_report deve iterar tickets via history(position=)."""
    src = _read("core/vt_autotrader.py")
    marker = "Importar deals reais do MT5"
    idx = src.find(marker)
    assert idx != -1
    block = src[idx:idx + 1500]
    assert "history(position=_tk)" in block, (
        "deve iterar tickets chamando history(position=) em vez de bulk history()"
    )
    # Não deve mais chamar _run_wine(EXECUTOR_WIN, "history") bulk neste bloco.
    assert '_run_wine(EXECUTOR_WIN, "history")' not in block, (
        "bulk history() sem args foi removido (retorna [] no Wine MT5)"
    )


# ─── Wave 880.J: PROFIT_LOCK storm fix ──────────────────────────────────────
def test_profit_lock_uses_stops_level_not_minus_one():
    """Wave 880.J: lock_pts deve respeitar trade_stops_level (não -1 hardcoded)."""
    src = _read("core/vt_autotrader.py")
    marker = "be_applied = False"
    idx = src.find(marker)
    assert idx != -1
    block = src[idx:idx + 2500]
    # O cálculo antigo -max(1, int(1 / point_val)) deve estar ausente.
    assert "lock_pts = -max(1, int(1 / point_val))" not in block, (
        "cálculo antigo lock_pts=-1 ainda presente (bug Wave D)"
    )
    # Deve consultar trade_stops_level via info().
    assert "trade_stops_level" in block
    assert "from mt5.mt5_orchestrator import info" in block


def test_profit_lock_attempted_persistence():
    """Wave 880.J: gate deve checar pos['profit_lock_attempted'] e marcar antes do modify."""
    src = _read("core/vt_autotrader.py")
    marker = "be_applied = False"
    idx = src.find(marker)
    assert idx != -1
    block = src[idx:idx + 2500]
    # Gate no if externo.
    assert "not pos.get(\"profit_lock_attempted\")" in block, (
        "gate deve bloquear re-tentativa de PROFIT_LOCK no mesmo ciclo"
    )
    # Marca ANTES do modify (para falha também parar o storm).
    assert 'pos["profit_lock_attempted"] = True' in block
    # A marcação deve vir ANTES do safe_modify_sl_with_emergency_close.
    mark_idx = block.find('pos["profit_lock_attempted"] = True')
    modify_idx = block.find("safe_modify_sl_with_emergency_close")
    assert mark_idx != -1 and modify_idx != -1
    assert mark_idx < modify_idx, (
        "profit_lock_attempted deve ser setado ANTES do modify (para falha contar)"
    )


def test_probe_script_unchanged_and_present():
    """O probe de diagnóstico Wave 880.H (w14_8) ainda existe p/ auditoria."""
    assert (_PROJECT / "scripts" / "w14_8_probe_mt5_history.py").exists()


# ─── Smoke: position= plumbing funciona end-to-end (mockado) ────────────────
def test_history_position_call_reaches_run_wine_correctly():
    """Simula a chamada history(position='123') e verifica o _run_wine correto."""
    import sys
    sys.path.insert(0, str(_PROJECT))
    try:
        from mt5 import mt5_orchestrator
        with mock.patch.object(mt5_orchestrator, "_run_wine",
                               return_value={"history": [], "info": "test"}) as m:
            # Com position= — deve chamar _run_wine(EXECUTOR_WIN, "history", "123", timeout=60)
            mt5_orchestrator.history(position="123")
            args = m.call_args.args
            assert args[1] == "history", f"args[1] deve ser 'history', got {args[1]}"
            assert args[2] == "123", f"args[2] deve ser o ticket, got {args[2]}"

            # Sem position=, com symbol — não deve incluir ticket numérico como 2o arg.
            m.reset_mock()
            mt5_orchestrator.history(symbol="WINQ26", days=1)
            args = m.call_args.args
            # args = (EXECUTOR_WIN, "history", "WINQ26", "1")
            assert "WINQ26" in args
            assert "123" not in args  # ticket não deve aparecer no caminho symbol
    finally:
        sys.path.remove(str(_PROJECT))
