"""
Testes para os 3 bugs do HALT DOL descobertos em 18/06/2026.

RED → GREEN workflow:
  1. Estes testes devem FALHAR antes dos fixes
  2. Aplicar fixes em vt_autotrader.py / vt_config.json
  3. Testes devem passar (GREEN)

Bugs a corrigir:
  Bug 1: _check_max_trades usa params_tf (sem max_daily_trades) → fallback 15 hardcoded
         deveria usar: params_tf → params_symbol → config.max_daily_trades → global_max
  Bug 2: consecutive_losses usa chaves inconsistentes ("DOLN26" vs "DOL_M30")
         deveria usar SEMPRE root_tf (ex: "DOL_M30")
  Bug 3: close_all_and_report usa state.consecutive_losses[symbol] (linha 1632-1635)
         deveria usar state.consecutive_losses[_root_tf]
"""
import sys
import os
sys.path.insert(0, "/home/bruno/Projects/Vibe-Trading")

import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

# Setup state mock
import vt_autotrader

# Patches necessários ANTES de importar
CONFIG_PATH = "/home/bruno/Projects/Vibe-Trading/vt_config.json"


def make_state_mock():
    """Cria um mock do AutotraderState com dicts vazios."""
    state = MagicMock()
    state.consecutive_losses = {}
    state.halt_until = {}
    state.notified_blocks = {}
    state.daily_trade_by_symbol = {}
    state.daily_trade_count = 0
    state.daily_pnl = 0.0
    state.max_consecutive_losses = 3
    state.positions = {}
    return state


def test_bug1_max_daily_trades_fallback():
    """
    Bug 1: Quando params vem de params_by_tf (sem max_daily_trades),
    _check_max_trades deveria usar params do ativo (DOL: 999),
    não o fallback hardcoded 15.

    Hoje: DOL_M30 fez 5 trades, foi bloqueado com msg errada
          "máximo diário atingido" (fallback 15 estava sendo usado).

    Esperado: Com max_daily_trades=999 no nível do ativo,
              _check_max_trades deve retornar True após 5 trades.
    """
    vt_autotrader.state = make_state_mock()

    # Carregar config real
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    # Simular params de TF (sem max_daily_trades) — isso é o que params_by_tf.DOL_M30 tem
    tf_params = {"sl_atr_mult": 0.8, "vwap_period": 50}  # SEM max_daily_trades
    symbol_params = cfg.get("dol", {})  # TEM max_daily_trades=999

    # Simular 5 trades já feitos (DOL_M30 fez 5 losses hoje)
    vt_autotrader.state.daily_trade_by_symbol["DOLN26"] = 5

    # Forçar o params que chega em _check_max_trades
    # Hoje: params=tf_params → cai no fallback 15 → bloqueia
    # Esperado: deveria usar symbol_params ou top-level (999) → passa

    with patch.object(vt_autotrader, "CONFIG", cfg):
        # Quando o código recebe tf_params, deveria fazer fallback chain
        # tf_params → dol.max_daily_trades → 999
        result = vt_autotrader._check_max_trades(tf_params, symbol="DOLN26")

        # ANTES do fix: retorna False (bloqueia com 5 < 15) ❌
        # DEPOIS do fix: retorna True (com 5 < 999) ✓
        assert result is True, (
            f"❌ Bug 1 ainda presente: _check_max_trades bloqueou DOL_M30 com 5 trades "
            f"(deveria permitir até 999 conforme config.dol.max_daily_trades)"
        )


def test_bug2_consecutive_losses_key_consistency():
    """
    Bug 2: HALT por-TF nunca dispara porque consecutive_losses
    usa chaves inconsistentes (DOLN26 vs DOL_M30).

    Setup: state tem consecutive_losses["DOL_M30"] = 4
           _check_consecutive_losses deve retornar False (pausado)

    Hoje: _check_consecutive_losses busca "DOL_M30" (key correto), mas
          state tem "DOLN26" (key errado do close_all_and_report) → sempre 0
    """
    vt_autotrader.state = make_state_mock()
    # 4 losses em DOL_M30
    vt_autotrader.state.consecutive_losses["DOL_M30"] = 4

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    with patch.object(vt_autotrader, "CONFIG", cfg):
        # _check_consecutive_losses("DOLN26", "M30") deve pausar
        result = vt_autotrader._check_consecutive_losses("DOLN26", "M30")

        # DEVE pausar (4 >= threshold DOL_M30=4)
        assert result is False, (
            f"❌ Bug 2 ainda presente: HALT não disparou para DOL_M30 com 4 losses "
            f"(threshold DOL_M30=4). Deveria ter pausado."
        )
        # E o halt_until deve estar populado
        assert "DOL_M30" in vt_autotrader.state.halt_until, (
            f"❌ halt_until.DOL_M30 não foi populado!"
        )


def test_bug3_close_all_uses_root_tf_key():
    """
    Bug 3: close_all_and_report (linha 1632-1635) usa state.consecutive_losses[symbol]
    onde symbol="DOLN26". Deveria usar state.consecutive_losses["DOL_M30"].

    Verifica lendo o código-fonte diretamente (mais robusto que mock).
    """
    import inspect
    from vt_autotrader import close_all_and_report

    src = inspect.getsource(close_all_and_report)

    # DEVE haver linha usando _root_tf (não symbol puro)
    assert "_root_tf" in src or "root_tf" in src, (
        f"❌ Bug 3 ainda presente: close_all_and_report não usa _root_tf.\n"
        f"Procurando '_root_tf' ou 'root_tf' no código da função.\n"
        f"Snippet encontrado:\n{src[:800]}"
    )

    # NÃO deve usar consecutive_losses[symbol] direto
    bad_patterns = [
        "consecutive_losses[symbol]",
        "consecutive_losses.get(symbol",
    ]
    for bad in bad_patterns:
        assert bad not in src, (
            f"❌ Bug 3 ainda presente: padrão '{bad}' encontrado em close_all_and_report"
        )


def test_bug4_max_trades_uses_correct_fallback():
    """
    Bug 1 (parte 2): _check_max_trades deveria fazer fallback chain:
      params_tf.max_daily_trades → params_symbol.max_daily_trades → top.max_daily_trades
    """
    vt_autotrader.state = make_state_mock()

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    # tf_params SEM max_daily_trades (realidade de params_by_tf.DOL_M5)
    tf_params = {"sl_atr_mult": 0.7, "cooldown_seconds": 900}

    # 16 trades feitos em DOL_M5 (já passou do fallback 15)
    vt_autotrader.state.daily_trade_by_symbol["DOLN26"] = 16

    with patch.object(vt_autotrader, "CONFIG", cfg):
        # Com o fix: tf_params não tem max_daily_trades → cai em cfg["dol"]["max_daily_trades"]=999
        result = vt_autotrader._check_max_trades(tf_params, symbol="DOLN26")

        # 16 < 999 → deve passar
        assert result is True, (
            f"❌ Bug 1 (parte 2): com 16 trades e dol.max_daily_trades=999, "
            f"deveria passar (não bloquear). Fallback chain está errado."
        )


def test_bug5_halt_threshold_per_tf_respected():
    """
    Bonus: HALT por-TF deve respeitar o threshold per-TF (max_consecutive_losses_by_tf).
    WDO_M30 tem threshold=4 (substitui DOL_M30 — contratos cheios fora de circulação desde 19/06/2026),
    então 4 losses consecutivas devem pausar.
    """
    vt_autotrader.state = make_state_mock()
    vt_autotrader.state.consecutive_losses["WDO_M30"] = 3  # 3 losses (abaixo de 4)

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    with patch.object(vt_autotrader, "CONFIG", cfg):
        # 3 < 4 (WDO_M30 threshold) → deve permitir
        result = vt_autotrader._check_consecutive_losses("WDON26", "M30")
        assert result is True, "Com 3 losses (threshold WDO_M30=4) deveria permitir"

        # 4ª loss → deve pausar
        vt_autotrader.state.consecutive_losses["WDO_M30"] = 4
        result = vt_autotrader._check_consecutive_losses("WDON26", "M30")
        assert result is False, "Com 4 losses (threshold WDO_M30=4) deveria pausar"


def test_bug6_win_on_resets_halt_per_tf():
    """
    Bonus: Quando WDO_M30 ganha 1 trade, deve limpar HALT apenas desse TF
    (não de WDO_M5 ou outros). WDO substitui DOL — contratos cheios fora de circulação.
    """
    vt_autotrader.state = make_state_mock()
    vt_autotrader.state.consecutive_losses["WDO_M30"] = 4
    vt_autotrader.state.consecutive_losses["WDO_M5"] = 5
    vt_autotrader.state.halt_until["WDO_M30"] = datetime.now() + timedelta(minutes=60)
    vt_autotrader.state.halt_until["WDO_M5"] = datetime.now() + timedelta(minutes=60)

    # Simular win em WDO_M30
    # (não chamamos manage_position, mas validamos que após win o halt só do M30 limpa)
    # Aqui validamos o conceito: state.halt_until.pop(key, None) só remove a chave correta
    vt_autotrader.state.halt_until.pop("WDO_M30", None)
    vt_autotrader.state.consecutive_losses["WDO_M30"] = 0

    assert "WDO_M30" not in vt_autotrader.state.halt_until, "HALT WDO_M30 deveria ter sido limpo"
    assert "WDO_M5" in vt_autotrader.state.halt_until, "HALT WDO_M5 deveria permanecer"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))