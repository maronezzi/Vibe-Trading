"""Testes do gate de pause (Wave 880.F — Bruno 2026-07-20).

Valida a checagem real de `data/autotrader.paused`:
- `_is_paused()` reflete a existência do arquivo.
- Robustez: OSError vira False (não crasha o daemon).
- O portão existe dentro de `check_and_trade()` no caminho de nova entrada
  (posição fechada) e NÃO no caminho de manage_position (posição aberta) —
  garantia estrutural de que pause bloqueia só novas, gerencia as abertas.
- Notificação Telegram one-shot: transições detectadas em run_daemon().

Host-coupling: `core.vt_autotrader` constrói estado global e lê MT5 no import.
Para evitar isso, estes testes mockam `PAUSE_FILE` e `_last_pause_state` sem
invocar `check_and_trade()`/`run_daemon()` por inteiro — focam nas unidades.
"""
import os
from unittest import mock

# Caminho absoluto do pause file esperado pelo daemon (data/autotrader.paused
# relativo à raiz do repo, derivado de Path(__file__).parent.parent no módulo).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Garantias estruturais (sem importar o módulo host-coupled) — via source.
# ---------------------------------------------------------------------------
def _read_autotrader_source() -> str:
    path = os.path.join(_PROJECT_ROOT, "core", "vt_autotrader.py")
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_pause_file_constant_points_to_data_dir():
    """PAUSE_FILE deve apontar para <repo>/data/autotrader.paused."""
    src = _read_autotrader_source()
    assert 'PAUSE_FILE' in src
    assert '"data"' in src or "'data'" in src
    assert "autotrader.paused" in src


def test_is_paused_helper_exists_and_is_safe():
    """_is_paused() deve existir, chamar .exists() e engolir OSError."""
    src = _read_autotrader_source()
    assert "def _is_paused()" in src
    assert ".exists()" in src
    assert "OSError" in src
    assert "return False" in src


def test_gate_blocks_new_entries_but_not_management():
    """O portão _is_paused() deve estar DENTRO do `else` (sem posição aberta),
    depois do `manage_position` e ANTES dos safety checks de entrada.

    Ordem esperada (Wave 880.F):
        pos = state.positions.get(...)
        if pos:
            manage_position(...)        # ← pause NÃO afeta
        else:
            if _is_paused(): continue   # ← pause bloqueia aqui
            # ... safety checks de nova entrada
    """
    src = _read_autotrader_source()
    pos_block = src.find('pos = state.positions.get(f"{symbol}_{tf}")')
    assert pos_block != -1, "bloco pos/state.positions não encontrado"

    manage_pos = src.find("manage_position(symbol, tf, pos, atr, strategy, params)", pos_block)
    assert manage_pos != -1, "manage_position não encontrado após pos"

    else_kw = src.find("else:", manage_pos)
    assert else_kw != -1, "else após manage_position não encontrado"

    pause_gate = src.find("if _is_paused():", else_kw)
    assert pause_gate != -1, "portão _is_paused() não está no else de nova entrada"

    # O `continue` do pause deve vir logo após (mesmo bloco)
    continue_after_pause = src.find("continue", pause_gate)
    assert continue_after_pause != -1 and (continue_after_pause - pause_gate) < 200, (
        "continue do gate _is_paused() não encontrado logo após"
    )

    # O portão NÃO deve aparecer antes do manage_position (isto é, no caminho
    # de gerenciamento de posição aberta).
    assert src.find("if _is_paused():", 0, manage_pos) == -1, (
        "portão _is_paused() encontrado ANTES do manage_position — pausaria gerência de abertas"
    )


def test_run_daemon_has_pause_transition_notification():
    """run_daemon() deve detectar borda e notificar Telegram uma única vez."""
    src = _read_autotrader_source()
    assert "_last_pause_state" in src
    assert "_now_paused" in src
    assert "!= _last_pause_state" in src
    assert "notify_telegram" in src


def test_run_daemon_declares_global_last_pause_state():
    """Atribuição a _last_pause_state em run_daemon exige declaração global."""
    src = _read_autotrader_source()
    assert "global _last_pause_state" in src


# ---------------------------------------------------------------------------
# Testes comportamentais do helper — isolam PAUSE_FILE sem importar o módulo
# inteiro (que é host-coupled: lê MT5/DB no top-level).
# ---------------------------------------------------------------------------
def _load_module_with_pause_file(tmp_pause_path):
    """Carrega vt_autotrader injetando um PAUSE_FILE sob nosso controle.

    Cria um módulo falsificado `core.vt_autotrader` suficiente para testar
    `_is_paused` sem disparar imports pesados. Faz isso importando só o
    trecho de código que interessa via exec, isolado do top-level.
    """
    # Stub mínimo dos símbolos que o helper não depende — só PAUSE_FILE/_last.
    namespace = {"__name__": "vt_autotrader_pause_test"}
    src = _read_autotrader_source()

    # Extrai só a definição de PAUSE_FILE, _last_pause_state e _is_paused.
    start = src.find("# ===== PAUSE FILE")
    assert start != -1
    end = src.find("# ===== CONFIGURAÇÃO =====", start)
    assert end != -1
    snippet = src[start:end]

    # Substitui a derivacão do Path por nosso path controlado (mantém como Path).
    snippet = snippet.replace(
        'PAUSE_FILE = Path(__file__).parent.parent / "data" / "autotrader.paused"',
        f'PAUSE_FILE = Path({tmp_pause_path!r})  # injetado pelo teste',
    )

    # Precisa de Path no namespace.
    from pathlib import Path
    namespace["Path"] = Path
    from typing import Optional
    namespace["Optional"] = Optional

    exec(snippet, namespace)
    return namespace


def test_is_paused_returns_false_when_file_absent(tmp_path):
    pause = tmp_path / "autotrader.paused"
    ns = _load_module_with_pause_file(str(pause))
    assert ns["_is_paused"]() is False


def test_is_paused_returns_true_when_file_present(tmp_path):
    pause = tmp_path / "autotrader.paused"
    pause.write_text("paused by test")
    ns = _load_module_with_pause_file(str(pause))
    assert ns["_is_paused"]() is True


def test_is_paused_handles_oserror(tmp_path):
    """Se o path levanta OSError (ex: permissão), retorna False sem propagar."""
    pause = tmp_path / "autotrader.paused"
    ns = _load_module_with_pause_file(str(pause))

    bad = mock.MagicMock()
    bad.exists.side_effect = OSError("permission denied")
    # Troca PAUSE_FILE por um objeto cujo .exists sempre levanta OSError.
    ns["PAUSE_FILE"] = bad
    assert ns["_is_paused"]() is False


def test_is_paused_reflects_runtime_creation_removal(tmp_path):
    """Criação/remoção do arquivo em runtime muda o retorno do helper."""
    pause = tmp_path / "autotrader.paused"
    ns = _load_module_with_pause_file(str(pause))

    assert ns["_is_paused"]() is False
    pause.write_text("x")
    assert ns["_is_paused"]() is True
    pause.unlink()
    assert ns["_is_paused"]() is False
