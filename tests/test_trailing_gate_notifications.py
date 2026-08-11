"""Wave 1111 (Bruno 2026-08-11): gate de entradas com trailing ativo + notificações.

Decisão Bruno (11/08): com o trailing profit lock engajado (PnL >= 50% do
target), NÃO abrir novas entradas — o trailing protege o lucro acumulado e
uma entrada nova é o vetor de risco que pode derrubar o PnL abaixo do floor
(virando BREACH e fechando tudo). Antes, o trailing só ratcheteava o piso e
entradas seguiam liberadas (WDOU26 abriu 2x com trailing ativo em 11/08).

Também corrige gaps de notificação Telegram:
  1. Ativação do trailing (1º TIGHTEN do dia) — antes só log local.
  2. Abertura de posição (FILLED) — antes só log [SINAL]/[TRACKER].

Estratégia: unitários puros para a lógica nova (is_active) + inspeção
estrutural do wiring no autotrader (padrão test_wave880_profit_lock.py —
mockar manage_position/check_and_trade tem dezenas de deps MT5/Wine).
"""
import sys
from pathlib import Path

import pytest

_PROJECT = Path(__file__).resolve().parent.parent
if str(_PROJECT) not in sys.path:
    sys.path.insert(0, str(_PROJECT))

_AUTOTRADER = _PROJECT / "core" / "vt_autotrader.py"


def _read_autotrader() -> str:
    return _AUTOTRADER.read_text(encoding="utf-8")


# ─── is_active() em vt_trailing_profit_lock (unitário puro) ────────────────

@pytest.fixture(autouse=True)
def _isolate_tpl_state(tmp_path, monkeypatch):
    import core.vt_trailing_profit_lock as tpl
    state_file = tmp_path / "vt_trailing_profit_lock.json"
    monkeypatch.setattr("core.vt_trailing_profit_lock.STATE_PATH", state_file)
    return state_file


def _write_tpl_state(path: Path, data: dict):
    import json
    path.write_text(json.dumps(data), encoding="utf-8")


def _today_str() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d")


def test_is_active_false_without_state(_isolate_tpl_state):
    from core.vt_trailing_profit_lock import is_active
    assert is_active() is False


def test_is_active_true_when_activated_today(_isolate_tpl_state):
    from core.vt_trailing_profit_lock import is_active
    _write_tpl_state(_isolate_tpl_state, {"date": _today_str(), "activated": True})
    assert is_active() is True


def test_is_active_false_when_not_activated(_isolate_tpl_state):
    from core.vt_trailing_profit_lock import is_active
    _write_tpl_state(_isolate_tpl_state, {"date": _today_str(), "activated": False})
    assert is_active() is False


def test_is_active_false_when_old_date(_isolate_tpl_state):
    from datetime import datetime, timedelta
    from core.vt_trailing_profit_lock import is_active
    state = {
        "date": (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"),
        "activated": True,
    }
    _write_tpl_state(_isolate_tpl_state, state)
    assert is_active() is False


# ─── Gate no check_and_trade (estrutural) ──────────────────────────────────

def test_gate_blocks_entries_when_trailing_active():
    """check_and_trade deve retornar cedo se o trailing profit lock está ativo."""
    src = _read_autotrader()
    assert "_tpl_is_active()" in src, (
        "check_and_trade deve consultar is_active() do trailing profit lock "
        "(Wave 1111 — bloquear novas entradas com trailing engajado)"
    )
    # O gate deve logar o estado (pico/floor) igual ao gate do profit lock full.
    assert "TRAILING PROFIT LOCK ativo desde" in src


def test_gate_keeps_managing_open_positions():
    """O gate bloqueia ENTRADAS mas não pode impedir gerenciamento de abertas."""
    src = _read_autotrader()
    # O gate deve ficar no início de check_and_trade (antes do loop de TFs),
    # onde posições abertas são gerenciadas por manage_position() depois.
    assert "novas entradas bloqueadas" in src.lower()
    assert "manage_position" in src


# ─── Notificação de ativação do trailing (estrutural) ──────────────────────

def test_trailing_activation_notifies_telegram():
    """1º TIGHTEN do dia deve notificar Telegram (antes: só log local)."""
    src = _read_autotrader()
    assert "Trailing Profit Lock ATIVADO" in src, (
        "Bloco TIGHTEN deve notificar Telegram na primeira ativação do dia "
        "(Wave 1111 — Bruno não recebia nada quando o trailing engajava)"
    )
    # Deve capturar o estado ativo ANTES do update_trailing para detectar a transição.
    assert "_tpl_was_active" in src


# ─── Notificação de abertura de posição (estrutural) ───────────────────────

def test_position_open_notifies_telegram():
    """_execute_entry (FILLED) deve notificar abertura no Telegram."""
    src = _read_autotrader()
    assert "🎫 Ticket" in src, (
        "_execute_entry deve notificar abertura com ticket no Telegram "
        "(Wave 1111 — antes só log [SINAL]/[TRACKER])"
    )
