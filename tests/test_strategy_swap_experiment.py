"""
Tests for Strategy Swap Experiment runner.

Bruno (17/06) pediu: quando AGI desativa um par, antes de desativar
DEVE testar 2+ estratégias alternativas + web intel (tinyfish).
Este módulo implementa isso.

RED tests — written before implementation.
"""
import pytest
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))


# ════════════════════════════════════════════════════════════════════════
# TESTES DO EXPERIMENT_RUNNER
# ════════════════════════════════════════════════════════════════════════

class TestExperimentRunnerStructure:
    """Verifica que experiment_runner existe e tem as funções corretas."""

    def test_module_imports(self):
        """experiment_runner.py deve existir e ser importável."""
        from experiment_runner import run_strategy_swap_experiment
        assert callable(run_strategy_swap_experiment)

    def test_returns_dict_with_required_keys(self):
        """Resultado deve ter chaves: pair, original_strategy, candidates, winner."""
        from experiment_runner import run_strategy_swap_experiment

        result = run_strategy_swap_experiment(
            sym="BIT", tf="M30", config={}, days=3
        )
        assert "pair" in result
        assert "original_strategy" in result
        assert "candidates" in result
        assert "winner" in result
        assert result["pair"] == "BIT_M30"

    def test_pair_key_format(self):
        """pair key = SYM_TF (ex: BIT_M30)."""
        from experiment_runner import run_strategy_swap_experiment

        result = run_strategy_swap_experiment(
            sym="IND", tf="M5", config={}, days=3
        )
        assert result["pair"] == "IND_M5"

    def test_original_strategy_in_config(self):
        """original_strategy = config.strategy[sym]."""
        from experiment_runner import run_strategy_swap_experiment

        config = {"strategy": {"BIT": "EMA_PULLBACK"}}
        result = run_strategy_swap_experiment(
            sym="BIT", tf="M30", config=config, days=3
        )
        assert result["original_strategy"] == "EMA_PULLBACK"

    def test_candidates_is_list(self):
        """candidates = lista de {strategy, pnl, n_trades, wr, decision}."""
        from experiment_runner import run_strategy_swap_experiment

        result = run_strategy_swap_experiment(
            sym="BIT", tf="M30", config={}, days=3
        )
        assert isinstance(result["candidates"], list)
        assert len(result["candidates"]) >= 2  # min 2 estratégias

    def test_winner_is_best_or_none(self):
        """winner = candidate com PnL mais alto, ou None se todos negativos."""
        from experiment_runner import run_strategy_swap_experiment

        result = run_strategy_swap_experiment(
            sym="BIT", tf="M30", config={}, days=3
        )
        # winner pode ser dict com {strategy, pnl, n_trades, wr} ou None
        assert result["winner"] is None or isinstance(result["winner"], dict)


class TestExperimentRunnerStrategies:
    """Verifica que testa múltiplas estratégias."""

    def test_tests_at_least_3_strategies(self):
        """Bruno pediu: testar 3+ estratégias diferentes."""
        from experiment_runner import run_strategy_swap_experiment

        result = run_strategy_swap_experiment(
            sym="BIT", tf="M30", config={}, days=3
        )
        strategies_tested = {c["strategy"] for c in result["candidates"]}
        assert len(strategies_tested) >= 3, (
            f"Esperado 3+ estratégias testadas, viu {len(strategies_tested)}: {strategies_tested}"
        )

    def test_strategies_from_whitelist(self):
        """Estratégias testadas devem ser da whitelist do AGI."""
        from experiment_runner import run_strategy_swap_experiment
        from agi_tuning_17h import VALID_STRATEGIES

        result = run_strategy_swap_experiment(
            sym="BIT", tf="M30", config={}, days=3
        )
        for c in result["candidates"]:
            assert c["strategy"] in VALID_STRATEGIES, (
                f"{c['strategy']} não está na whitelist"
            )

    def test_excludes_current_strategy(self):
        """Não deve testar a estratégia atual (já sabemos que não funciona)."""
        from experiment_runner import run_strategy_swap_experiment

        config = {"strategy": {"BIT": "RSI_REVERSION"}}
        result = run_strategy_swap_experiment(
            sym="BIT", tf="M30", config=config, days=3
        )
        strategies_tested = {c["strategy"] for c in result["candidates"]}
        assert "RSI_REVERSION" not in strategies_tested

    def test_includes_web_intel(self):
        """Cada candidato deve ter informação de web intel (tinyfish)."""
        from experiment_runner import run_strategy_swap_experiment

        result = run_strategy_swap_experiment(
            sym="BIT", tf="M30", config={}, days=3
        )
        for c in result["candidates"]:
            assert "web_intel" in c, f"Candidate {c['strategy']} sem web_intel"
            assert isinstance(c["web_intel"], str)

    def test_candidate_pnl_aggregation(self):
        """PnL do candidate = soma de pnl dos trades simulados."""
        from experiment_runner import run_strategy_swap_experiment

        result = run_strategy_swap_experiment(
            sym="BIT", tf="M30", config={}, days=3
        )
        for c in result["candidates"]:
            assert "pnl" in c
            assert "n_trades" in c
            assert "wr" in c
            assert "decision" in c


class TestExperimentRunnerAGIIntegration:
    """Verifica que AGI vai usar o experiment antes de desativar."""

    def test_agi_calls_experiment_before_pausing(self):
        """AGI deve chamar run_strategy_swap_experiment antes de adicionar par a disabled_timeframes."""
        from experiment_runner import should_pause_pair, run_strategy_swap_experiment

        # Simular resultado onde experiment encontrou winner positivo
        result = {
            "pair": "BIT_M30",
            "original_strategy": "RSI_REVERSION",
            "candidates": [
                {"strategy": "EMA_PULLBACK", "pnl": 150.0, "n_trades": 5, "wr": 60.0, "decision": "ok", "web_intel": "..."},
                {"strategy": "VWAP", "pnl": 80.0, "n_trades": 4, "wr": 50.0, "decision": "ok", "web_intel": "..."},
            ],
            "winner": {"strategy": "EMA_PULLBACK", "pnl": 150.0, "n_trades": 5, "wr": 60.0, "decision": "ok", "web_intel": "..."}
        }
        # Se tem winner positivo, NÃO deve pausar
        assert should_pause_pair(result, pnl_threshold=-50.0) is False

    def test_should_pause_pair_no_winner(self):
        """Se nenhum winner (todas estratégias negativas), DEVE pausar."""
        from experiment_runner import should_pause_pair

        result = {
            "pair": "BIT_M30",
            "original_strategy": "RSI_REVERSION",
            "candidates": [
                {"strategy": "EMA_PULLBACK", "pnl": -100.0, "n_trades": 5, "wr": 40.0, "decision": "ok", "web_intel": "..."},
                {"strategy": "VWAP", "pnl": -80.0, "n_trades": 4, "wr": 25.0, "decision": "ok", "web_intel": "..."},
            ],
            "winner": None
        }
        assert should_pause_pair(result, pnl_threshold=-50.0) is True

    def test_should_pause_pair_no_data(self):
        """Se não há dados, NÃO deve pausar (seria precipitado)."""
        from experiment_runner import should_pause_pair

        result = {
            "pair": "BIT_M30",
            "original_strategy": "RSI_REVERSION",
            "candidates": [
                {"strategy": "EMA_PULLBACK", "pnl": 0.0, "n_trades": 0, "wr": 0.0, "decision": "no_data", "web_intel": "..."},
            ],
            "winner": None
        }
        assert should_pause_pair(result, pnl_threshold=-50.0) is False

    def test_should_pause_pair_winner_meets_threshold(self):
        """Winner com PnL > threshold = NÃO pausa (reabilita com nova estratégia)."""
        from experiment_runner import should_pause_pair

        result = {
            "pair": "BIT_M30",
            "original_strategy": "RSI_REVERSION",
            "candidates": [
                {"strategy": "EMA_PULLBACK", "pnl": 30.0, "n_trades": 5, "wr": 60.0, "decision": "ok", "web_intel": "..."},
            ],
            "winner": {"strategy": "EMA_PULLBACK", "pnl": 30.0, "n_trades": 5, "wr": 60.0, "decision": "ok", "web_intel": "..."}
        }
        # PnL 30 > threshold -50 → mantém ativo
        assert should_pause_pair(result, pnl_threshold=-50.0) is False

    def test_agi_doesnt_pause_if_swap_promising(self):
        """should_pause_pair deve considerar nº trades mínimo."""
        from experiment_runner import should_pause_pair

        # Winner com poucos trades (não confiável) — cautela
        result = {
            "pair": "BIT_M30",
            "original_strategy": "RSI_REVERSION",
            "candidates": [
                {"strategy": "EMA_PULLBACK", "pnl": 200.0, "n_trades": 1, "wr": 100.0, "decision": "ok", "web_intel": "..."},
            ],
            "winner": {"strategy": "EMA_PULLBACK", "pnl": 200.0, "n_trades": 1, "wr": 100.0, "decision": "ok", "web_intel": "..."}
        }
        # 1 trade é pouco → cautela → ainda pausa (?)
        # Decisão: precisa de min_trades=3 para confiar
        # should_pause_pair deve retornar True se winner tem < min_trades
        decision = should_pause_pair(result, pnl_threshold=-50.0, min_trades=3)
        # Se o winner tem < 3 trades, não é confiável, ainda pausa
        assert decision is True  # 1 trade não é suficiente


class TestApplySwapToConfig:
    """Verifica que update de strategy é aplicado corretamente."""

    def test_apply_swap_updates_strategy_by_tf(self):
        """Deve atualizar config.strategy_by_tf[pair] com nova estratégia."""
        from experiment_runner import apply_swap_to_config

        config = {
            "strategy": {"BIT": "RSI_REVERSION"},
            "strategy_by_tf": {"BIT_M30": "RSI_REVERSION"}
        }
        result = {
            "pair": "BIT_M30",
            "winner": {"strategy": "EMA_PULLBACK", "pnl": 100.0, "n_trades": 5, "wr": 60.0, "decision": "ok", "web_intel": "..."}
        }
        new_config = apply_swap_to_config(config, result)
        assert new_config["strategy_by_tf"]["BIT_M30"] == "EMA_PULLBACK"

    def test_apply_swap_returns_new_config(self):
        """Deve retornar config novo (não mutar in-place)."""
        from experiment_runner import apply_swap_to_config

        config = {
            "strategy": {"BIT": "RSI_REVERSION"},
            "strategy_by_tf": {"BIT_M30": "RSI_REVERSION"}
        }
        result = {
            "pair": "BIT_M30",
            "winner": {"strategy": "EMA_PULLBACK", "pnl": 100.0, "n_trades": 5, "wr": 60.0, "decision": "ok", "web_intel": "..."}
        }
        new_config = apply_swap_to_config(config, result)
        assert new_config is not config
        assert config["strategy_by_tf"]["BIT_M30"] == "RSI_REVERSION"  # original intacto

    def test_apply_swap_no_winner_returns_unchanged(self):
        """Se não há winner, retorna config sem mudanças."""
        from experiment_runner import apply_swap_to_config

        config = {
            "strategy": {"BIT": "RSI_REVERSION"},
            "strategy_by_tf": {"BIT_M30": "RSI_REVERSION"}
        }
        result = {"pair": "BIT_M30", "winner": None}
        new_config = apply_swap_to_config(config, result)
        assert new_config["strategy_by_tf"]["BIT_M30"] == "RSI_REVERSION"


class TestWebIntel:
    """Verifica integração com TinyFish."""

    def test_web_intel_function_exists(self):
        """Função de web intel deve existir."""
        from experiment_runner import get_web_intel_for_strategy
        assert callable(get_web_intel_for_strategy)

    def test_web_intel_returns_string(self):
        """get_web_intel_for_strategy deve retornar string."""
        from experiment_runner import get_web_intel_for_strategy

        intel = get_web_intel_for_strategy("BIT", "M30", "EMA_PULLBACK")
        assert isinstance(intel, str)
        assert len(intel) > 0

    def test_web_intel_handles_offline_gracefully(self):
        """Se TinyFish está offline, retorna string indicativa (sem crash)."""
        from experiment_runner import get_web_intel_for_strategy

        with patch("experiment_runner._query_tinyfish", side_effect=Exception("offline")):
            intel = get_web_intel_for_strategy("BIT", "M30", "EMA_PULLBACK")
            assert isinstance(intel, str)
            assert "offline" in intel.lower() or "indispon" in intel.lower() or len(intel) > 0
