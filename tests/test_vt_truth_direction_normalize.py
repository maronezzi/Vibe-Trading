"""
test_vt_truth_direction_normalize.py
=====================================
FASE 3.5 do refactor Vibe-Trading (Fase 3.5 normalizacao de direcao).

Bug latente corrigido:
  Antes: `direction = str(p.get("type", "") or "")` colapsava `type=0`
  (int BUY) para string vazia, porque `0 or ""` -> `""`.
  Embora MT5 real retorne string hoje ("BUY"/"SELL"), o codigo era
  fragil: se um helper interno esquecesse de mapear int->str, a
  direcao era silenciosamente perdida.

Helper criado em core/vt_truth.py:
  _normalize_direction(type_value) -> str

Estes testes verificam o contrato do helper de forma exaustiva,
incluindo entradas int, str, None, False, "" e casos nao-canonicos.
"""
import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path("/home/bruno/Projects/Vibe-Trading")
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "core"))


# ===== HELPERS =====

def _load_vt_truth_module():
    """Carrega core/vt_truth.py como modulo isolado.

    Mesmo padrao usado em test_watchdog_truth_layer.py: evita efeitos
    colaterais de import direto (ex: modulo ja em sys.modules com state
    antigo de cache TTL).

    Importante: o modulo eh registrado em sys.modules com o nome do
    location (ex: '_vt_truth_direction_normalize_test') para que os
    dataclasses consigam resolver seus type annotations via
    `sys.modules[cls.__module__].__dict__`. Sem isso, dataclass falha
    com 'NoneType' object has no attribute '__dict__'.
    """
    spec = importlib.util.spec_from_file_location(
        "_vt_truth_direction_normalize_test",
        PROJECT_ROOT / "core" / "vt_truth.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registra em sys.modules ANTES de exec_module para que os type
    # annotations dos dataclasses (Position, Deal) sejam resolvidos
    # corretamente.
    sys.modules[module.__name__] = module
    spec.loader.exec_module(module)
    return module


def _fresh_module():
    """Retorna instancia fresca do vt_truth (cache zerado).

    Importante: o modulo tem caches TTL (_positions_cache, _history_cache,
    _pnl_cache) que persistem entre imports via sys.modules. Como
    _load_vt_truth_module() faz spec_from_file_location com nome unico,
    cada chamada retorna modulo NOVO, e seus caches sao independentes.
    """
    return _load_vt_truth_module()


# ===== TESTES DO HELPER _normalize_direction =====

def test_normalize_int_0_is_buy():
    """type=0 (int) deve mapear para 'BUY'. Caso principal do bug."""
    vt = _fresh_module()
    assert vt._normalize_direction(0) == "BUY"


def test_normalize_int_1_is_sell():
    """type=1 (int) deve mapear para 'SELL'."""
    vt = _fresh_module()
    assert vt._normalize_direction(1) == "SELL"


def test_normalize_string_buy():
    """type='BUY' (str, canonico) passa direto."""
    vt = _fresh_module()
    assert vt._normalize_direction("BUY") == "BUY"


def test_normalize_string_sell():
    """type='SELL' (str, canonico) passa direto."""
    vt = _fresh_module()
    assert vt._normalize_direction("SELL") == "SELL"


def test_normalize_string_0_is_buy():
    """type='0' (str numerica) deve mapear para 'BUY' (mesma intencao de 0)."""
    vt = _fresh_module()
    assert vt._normalize_direction("0") == "BUY"


def test_normalize_string_1_is_sell():
    """type='1' (str numerica) deve mapear para 'SELL' (mesma intencao de 1)."""
    vt = _fresh_module()
    assert vt._normalize_direction("1") == "SELL"


def test_normalize_none_returns_empty():
    """type=None deve retornar '' (caller decide o que fazer)."""
    vt = _fresh_module()
    assert vt._normalize_direction(None) == ""


def test_normalize_empty_returns_empty():
    """type='' deve retornar '' (string vazia -> string vazia)."""
    vt = _fresh_module()
    assert vt._normalize_direction("") == ""


# ===== TESTES EXTRAS (defesa em profundidade) =====

def test_normalize_string_buy_lowercase_normalized():
    """type='buy' (lowercase) deve normalizar para 'BUY' (case-insensitive)."""
    vt = _fresh_module()
    assert vt._normalize_direction("buy") == "BUY"


def test_normalize_string_sell_lowercase_normalized():
    """type='sell' (lowercase) deve normalizar para 'SELL'."""
    vt = _fresh_module()
    assert vt._normalize_direction("sell") == "SELL"


def test_normalize_false_returns_empty():
    """type=False (bool, embora improvavel) deve retornar '' e nao 'BUY'."""
    vt = _fresh_module()
    # bool eh subclasse de int, mas False != 0 semantico de direcao.
    # O contrato: direcao vazia (NAO "BUY" via True==1, NAO "SELL" via False==0).
    result = vt._normalize_direction(False)
    assert result == ""


def test_normalize_int_out_of_range_returns_stringified():
    """type=2 (int fora do range BUY/SELL) deve retornar str(2) (nao quebrar)."""
    vt = _fresh_module()
    # Nao levanta excecao; passa adiante como string pra nao perder a info.
    assert vt._normalize_direction(2) == "2"


def test_normalize_unknown_string_passes_through():
    """type='XYZ' (str desconhecida) passa adiante (logado como WARN, mas retorna)."""
    vt = _fresh_module()
    # Nao engole typos do broker — passa o que recebeu.
    assert vt._normalize_direction("XYZ") == "XYZ"


def test_normalize_strips_whitespace():
    """type='  BUY  ' (com whitespace) deve normalizar para 'BUY'."""
    vt = _fresh_module()
    assert vt._normalize_direction("  BUY  ") == "BUY"
