"""
test_vt_truth.py
=================
Testes integrados para core/vt_truth.py (truth layer centralizado).

Fase 2.5 introduziu o modulo (refactor de acesso MT5).
Fase 3.5 corrigiu bug latente em get_open_positions / get_position_history
  onde `str(p.get("type", "") or "")` colapsava `type=0` (int BUY) para "".
  Helper _normalize_direction() agora trata int 0/1, str "0"/"1"/"BUY"/"SELL",
  None, False, e "" de forma explicita.

Este arquivo foca em testes de INTEGRACAO: verifica que as funcoes publicas
(get_open_positions, get_position_history) aplicam a normalizacao corretamente
em cada posicao/deal retornado.

Testes unitarios do helper estao em test_vt_truth_direction_normalize.py.
"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path("/home/bruno/Projects/Vibe-Trading")
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "core"))


# ===== HELPERS =====

def _load_vt_truth_module():
    """Carrega core/vt_truth.py como modulo isolado.

    Como spec_from_file_location usa nome unico, cada chamada retorna
    modulo NOVO com caches TTL zerados — sem vazamento entre testes.

    O modulo eh registrado em sys.modules ANTES de exec_module para que
    os type annotations dos dataclasses (Position, Deal) sejam resolvidos
    via `sys.modules[cls.__module__].__dict__`. Sem isso, dataclass falha
    com 'NoneType' object has no attribute '__dict__'.
    """
    spec = importlib.util.spec_from_file_location(
        "_vt_truth_test",
        PROJECT_ROOT / "core" / "vt_truth.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module.__name__] = module
    spec.loader.exec_module(module)
    return module


# ===== TESTES DE INTEGRACAO (Fase 3.5) =====

def test_get_open_positions_handles_int_type_0_as_buy():
    """get_open_positions() deve mapear type=0 (int) para direction='BUY'.

    Bug latente original: str(p.get("type", "") or "") colapsava 0 -> ""
    porque `0 or ""` -> "". Com o helper _normalize_direction(), o int
    0 agora chega como 'BUY' canônico.

    Setup: mock _mt5_status() retornando uma posicao com type=0 (int).
    """
    vt = _load_vt_truth_module()

    fake_raw = {
        "positions": [
            {
                "ticket": 12345,
                "symbol": "WINM26",
                "type": 0,  # int BUY — caminho do bug
                "volume": 1.0,
                "price_open": 5000.0,
                "price_current": 5005.0,
                "sl": 4990.0,
                "tp": 0.0,
                "profit": 5.0,
                "swap": 0.0,
                "magic": vt.MAGIC_VIBETRADING,
                "time": "1719840300",
                "comment": "test_buy_int",
                "identifier": 999,
            }
        ]
    }

    with patch.object(vt, "_mt5_status", return_value=fake_raw):
        positions = vt.get_open_positions()

    assert len(positions) == 1, f"Esperava 1 posicao, recebi {len(positions)}"
    assert positions[0].direction == "BUY", (
        f"BUG: type=0 (int) deveria virar 'BUY', recebi {positions[0].direction!r}"
    )
    assert positions[0].symbol == "WINM26"
    assert positions[0].ticket == 12345


def test_get_open_positions_handles_int_type_1_as_sell():
    """get_open_positions() deve mapear type=1 (int) para direction='SELL'."""
    vt = _load_vt_truth_module()

    fake_raw = {
        "positions": [
            {
                "ticket": 67890,
                "symbol": "WDON26",
                "type": 1,  # int SELL
                "volume": 2.0,
                "price_open": 5500.0,
                "price_current": 5495.0,
                "sl": 5510.0,
                "tp": 0.0,
                "profit": 10.0,
                "swap": 0.0,
                "magic": vt.MAGIC_VIBETRADING,
                "time": "1719840400",
                "comment": "test_sell_int",
                "identifier": 888,
            }
        ]
    }

    with patch.object(vt, "_mt5_status", return_value=fake_raw):
        positions = vt.get_open_positions()

    assert len(positions) == 1
    assert positions[0].direction == "SELL", (
        f"BUG: type=1 (int) deveria virar 'SELL', recebi {positions[0].direction!r}"
    )


def test_get_position_history_handles_int_type_0_as_buy():
    """get_position_history() deve mapear type=0 (int) para direction='BUY'.

    Mesma logica do bug original, aplicada ao loop de deals (nao so
    positions). Garante que PnL diario e reconciliacao funcionam
    quando o broker devolve type como int (caminho hipotetico do
    mt5_orchestrator mudar de format).
    """
    vt = _load_vt_truth_module()

    fake_raw = {
        "history": [
            {
                "ticket": 11111,
                "symbol": "WINM26",
                "type": 0,  # int BUY
                "volume": 1.0,
                "price": 5000.0,
                "profit": 5.0,
                "commission": -0.5,
                "swap": 0.0,
                "fee": 0.0,
                "time": "1719840300",
                "position_id": 11111,
                "reason": 3,
                "magic": vt.MAGIC_VIBETRADING,
                "comment": "test_buy_history",
            }
        ],
        "count": 1,
    }

    with patch.object(vt, "_mt5_history", return_value=fake_raw):
        deals = vt.get_position_history(symbol="WINM26", days=1)

    assert len(deals) == 1
    assert deals[0].direction == "BUY", (
        f"BUG: type=0 (int) em deal deveria virar 'BUY', recebi {deals[0].direction!r}"
    )
    assert deals[0].symbol == "WINM26"
    assert deals[0].ticket == 11111


# ===== TESTES EXTRAS (defesa em profundidade, regressao Fase 2.5) =====

def test_get_open_positions_filters_by_magic():
    """get_open_positions() deve filtrar posicoes por magic number."""
    vt = _load_vt_truth_module()

    fake_raw = {
        "positions": [
            {
                "ticket": 1,
                "symbol": "WINM26",
                "type": "BUY",
                "volume": 1.0,
                "price_open": 5000.0,
                "price_current": 5005.0,
                "sl": 4990.0,
                "tp": 0.0,
                "profit": 5.0,
                "swap": 0.0,
                "magic": vt.MAGIC_VIBETRADING,  # 555501
                "time": "1719840300",
                "comment": "ours",
                "identifier": 0,
            },
            {
                "ticket": 2,
                "symbol": "WDON26",
                "type": "SELL",
                "volume": 1.0,
                "price_open": 5500.0,
                "price_current": 5505.0,
                "sl": 5510.0,
                "tp": 0.0,
                "profit": -5.0,
                "swap": 0.0,
                "magic": 999999,  # outro bot
                "time": "1719840400",
                "comment": "other_bot",
                "identifier": 0,
            },
        ]
    }

    with patch.object(vt, "_mt5_status", return_value=fake_raw):
        positions = vt.get_open_positions(magic_filter=vt.MAGIC_VIBETRADING)

    # So a posicao com magic 555501 deve passar.
    assert len(positions) == 1
    assert positions[0].ticket == 1
    assert positions[0].comment == "ours"


def test_get_open_positions_empty_on_mt5_error():
    """get_open_positions() deve retornar [] se MT5 indisponivel (fail-safe)."""
    vt = _load_vt_truth_module()

    with patch.object(vt, "_mt5_status", side_effect=RuntimeError("wine down")):
        positions = vt.get_open_positions()

    assert positions == []
