"""
test_watchdog_get_bot_positions_uses_truth.py
=============================================

FASE 3+4 (2026-07-08): get_bot_positions() do watchdog NAO deve mais
ler /tmp/vt_autotrader_state.json (legado, descontinuado desde 01/07
no autotrader). Deve usar truth layer (core.vt_truth.get_open_positions)
como fonte autoritativa — mesma fonte que o autotrader ja usa internamente.

Regressao coberta: watchdog cron vt-trade-watchdog (a55449e2c025) vinha
logando [STATE ERRO] [Errno 2] No such file or directory a cada 2min
porque /tmp/vt_autotrader_state.json nao eh mais escrito pelo autotrader
(Fase 3: state virou projecao em memoria, fonte de verdade = truth layer).

Este teste:
1. NAO toca disco em /tmp/vt_autotrader_state.json
2. Mocka core.vt_truth.get_open_positions pra retornar 2 Position
3. Verifica que get_bot_positions() retorna dict keyed by ticket
4. Verifica shape esperado por find_discrepancies():
   {entry_ticket, symbol, direction, volume, entry_price, entry_time}
5. Verifica que get_bot_positions() retorna {} (fail-safe) se truth layer
   levanta excecao — em vez de crashar com [Errno 2]

RED -> GREEN: este teste falha ANTES do fix (porque get_bot_positions
chama open() no arquivo legado). Passa apos o patch.
"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path("/home/bruno/Projects/Vibe-Trading")
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "core"))
sys.path.insert(0, str(PROJECT_ROOT / "monitoring"))


def _load_watchdog_module():
    """Carrega monitoring/vt_trade_watchdog.py como modulo isolado.

    Padrao ja usado em test_watchdog_truth_layer.py — evita efeitos
    colaterais de import direto quando o modulo ja esta em sys.modules.
    """
    spec = importlib.util.spec_from_file_location(
        "_watchdog_get_bot_positions_test",
        PROJECT_ROOT / "monitoring" / "vt_trade_watchdog.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _make_fake_positions():
    """Constroi 2 Position-like dataclass (fake, nao precisa importar core.vt_truth)."""
    # Evita importar core.vt_truth aqui — pode falhar fora do venv
    from types import SimpleNamespace
    return [
        SimpleNamespace(
            ticket=2472719457, symbol="WINQ26", direction="BUY", volume=1.0,
            price_open=174055.0, price_current=174100.0, sl=173870.0, tp=0.0,
            profit=45.0, swap=0.0, magic=555501, open_time="2026-07-08 10:30:00",
            comment="VibeTrading", identifier=1,
        ),
        SimpleNamespace(
            ticket=2472578035, symbol="WDOU26", direction="SELL", volume=1.0,
            price_open=5285.0, price_current=5280.0, sl=5295.0, tp=0.0,
            profit=5.0, swap=0.0, magic=555501, open_time="2026-07-08 10:35:00",
            comment="VibeTrading", identifier=2,
        ),
    ]


def test_get_bot_positions_uses_truth_layer_returns_positions_dict():
    """get_bot_positions() deve usar core.vt_truth.get_open_positions().

    Resultado esperado: dict[str, dict] keyed by str(ticket) com shape
    compativel com find_discrepancies(): {entry_ticket, symbol, direction,
    volume, entry_price, entry_time}.
    """
    module = _load_watchdog_module()
    fake_positions = _make_fake_positions()

    # Patch no modulo core.vt_truth (origem do import lazy dentro de
    # get_bot_positions()). Patchar module.get_open_positions nao funciona
    # porque a funcao faz `from core.vt_truth import get_open_positions`
    # localmente — o name local ganha do atributo de modulo.
    with patch("core.vt_truth.get_open_positions", return_value=fake_positions), \
         patch.object(module, "STATE_FILE", "/tmp/vt_autotrader_state.json", create=True):
        result = module.get_bot_positions()

    assert isinstance(result, dict), f"esperava dict, recebeu {type(result)}"
    assert len(result) == 2, f"esperava 2 posicoes, recebeu {len(result)}"

    # Keyed by ticket as string
    assert "2472719457" in result
    assert "2472578035" in result

    # Shape compativel com find_discrepancies (usa pos.get("entry_ticket"))
    for ticket, pos in result.items():
        assert "entry_ticket" in pos, f"pos {ticket} sem entry_ticket"
        assert pos["entry_ticket"] == ticket
        assert "symbol" in pos
        assert "direction" in pos
        assert "volume" in pos


def test_get_bot_positions_returns_empty_dict_when_truth_layer_fails():
    """get_bot_positions() deve retornar {} (fail-safe) se truth layer falha.

    Antes do fix: open() no STATE_FILE legado crashava FileNotFoundError,
    logava [STATE ERRO] e retornava {} — mas DEPOIS de logar lixo a cada
    2min. Agora deve ser silent (return {}) e usar truth layer.
    """
    module = _load_watchdog_module()

    # Truth layer levanta excecao (ex: MT5 off). Patcha no path canonico
    # core.vt_truth.get_open_positions (mesma razao do teste anterior).
    with patch("core.vt_truth.get_open_positions", side_effect=Exception("MT5 off")):
        result = module.get_bot_positions()

    assert result == {}, f"esperava dict vazio em MT5 off, recebeu {result}"


def test_get_bot_positions_does_not_read_legacy_state_file(tmp_path, monkeypatch):
    """get_bot_positions() NAO deve abrir /tmp/vt_autotrader_state.json.

    Defesa contra regressao: garante que alguem nao vai re-introduzir
    o open() legado enquanto o autotrader (Fase 3) nao escreve mais nele.
    """
    module = _load_watchdog_module()

    # Cria arquivo state legado vazio (simula Fase 1 antiga) — se watchdog
    # ler isso, teste falha. Use create=True pois STATE_FILE foi removido
    # na Fase 3 e pode nao existir no modulo.
    legacy_state = tmp_path / "vt_autotrader_state.json"
    legacy_state.write_text('{"positions": {"fake_key": {"entry_ticket": "999"}}}')
    monkeypatch.setattr(module, "STATE_FILE", str(legacy_state), raising=False)

    # Truth layer retorna lista vazia (sem posicoes abertas)
    with patch("core.vt_truth.get_open_positions", return_value=[]):
        result = module.get_bot_positions()

    # Se o fix estiver correto, truth layer (vazio) vence sobre o arquivo legado
    assert result == {}, \
        f"watchdog ainda le STATE_FILE legado? resultado={result}. " \
        f"Esperado {{}} (truth layer mockado retornou lista vazia)."
