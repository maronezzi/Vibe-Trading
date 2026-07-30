"""
Testes para a seleção de pares por SIMULAÇÃO no Stage 1 (Wave "sem-trades").

REGRA DE OURO: a otimização nunca é decidida sobre trades passados. O Stage 1
identifica pares failing simulando cada par via evaluate_baseline (bar-a-bar,
barras reais). Aqui mockamos evaluate_baseline para deixar a seleção
determinística e sem dependência de MT5/Wine.
"""
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from optimization.agi_v4 import stage1_collect  # noqa: E402


# Config mínimo: 2 símbolos × 2 TFs = 4 pares.
_MIN_CONFIG = {
    "symbols": ["WIN", "WDO"],
    "timeframes_by_symbol": {"WIN": ["M5", "M15"], "WDO": ["M5", "M15"]},
    "timeframes": ["M5", "M15"],
}


def _make_evaluator(results: dict):
    """Cria um evaluate_baseline fake que retorna pnl/n_trades por 'SYM_TF'.

    results: {"WIN_M5": {"total_pnl": -100, "n_trades": 12}, ...}
    Pares ausentes → simula PnL 0 (failing por <= 0).
    """
    def _fake(sym, tf, config):  # noqa: ARG001
        key = f"{sym}_{tf}"
        r = results.get(key, {"total_pnl": 0.0, "n_trades": 0})
        return {"total_pnl": r["total_pnl"], "n_trades": r.get("n_trades", 0)}
    return _fake


class TestIdentifyFailingSimulated:
    """Cobre _identify_failing_simulated (decisão por simulação, sem DB)."""

    def test_marca_apenas_pares_com_pnl_menor_ou_igual_a_zero(self, monkeypatch):
        # WIN_M5 e WDO_M15 lucrativos; WIN_M15 e WDO_M5 não-lucrativos.
        results = {
            "WIN_M5": {"total_pnl": 150.0, "n_trades": 20},
            "WIN_M15": {"total_pnl": -80.0, "n_trades": 15},
            "WDO_M5": {"total_pnl": -200.0, "n_trades": 30},
            "WDO_M15": {"total_pnl": 50.0, "n_trades": 10},
        }
        monkeypatch.setattr(
            "optimization.agi_v4.backtest_evaluator.evaluate_baseline",
            _make_evaluator(results),
        )

        failing = stage1_collect._identify_failing_simulated(_MIN_CONFIG)

        assert isinstance(failing, list)
        assert all(isinstance(p, str) for p in failing), "contrato = list[str]"
        assert set(failing) == {"WIN_M15", "WDO_M5"}

    def test_ordenados_do_mais_negativo_para_o_menos_negativo(self, monkeypatch):
        results = {
            "WIN_M5": {"total_pnl": -10.0},   # menos negativo
            "WIN_M15": {"total_pnl": -200.0},  # mais negativo
            "WDO_M5": {"total_pnl": 100.0},
            "WDO_M15": {"total_pnl": -50.0},
        }
        monkeypatch.setattr(
            "optimization.agi_v4.backtest_evaluator.evaluate_baseline",
            _make_evaluator(results),
        )

        failing = stage1_collect._identify_failing_simulated(_MIN_CONFIG)

        assert failing == ["WIN_M15", "WDO_M15", "WIN_M5"]

    def test_pnl_exatamente_zero_e_failing(self, monkeypatch):
        # Mesmo critério de _check_convergence_simulated: PnL <= 0 = failing.
        results = {
            "WIN_M5": {"total_pnl": 0.0, "n_trades": 0},
            "WIN_M15": {"total_pnl": 1.0, "n_trades": 5},
            "WDO_M5": {"total_pnl": 1.0, "n_trades": 5},
            "WDO_M15": {"total_pnl": 1.0, "n_trades": 5},
        }
        monkeypatch.setattr(
            "optimization.agi_v4.backtest_evaluator.evaluate_baseline",
            _make_evaluator(results),
        )

        failing = stage1_collect._identify_failing_simulated(_MIN_CONFIG)
        assert failing == ["WIN_M5"]

    def test_todos_lucrativos_retorna_lista_vazia(self, monkeypatch):
        results = {k: {"total_pnl": 100.0, "n_trades": 10} for k in
                   ["WIN_M5", "WIN_M15", "WDO_M5", "WDO_M15"]}
        monkeypatch.setattr(
            "optimization.agi_v4.backtest_evaluator.evaluate_baseline",
            _make_evaluator(results),
        )

        assert stage1_collect._identify_failing_simulated(_MIN_CONFIG) == []

    def test_erro_em_uma_sim_nao_quebra_o_resto_e_marca_failing(self, monkeypatch):
        # WIN_M5 levanta exceção → marcado failing (fail-safe), outros ok.
        def _flaky(sym, tf, config):  # noqa: ARG001
            if f"{sym}_{tf}" == "WIN_M5":
                raise RuntimeError("MT5 down")
            return {"total_pnl": 100.0, "n_trades": 10}

        monkeypatch.setattr(
            "optimization.agi_v4.backtest_evaluator.evaluate_baseline",
            _flaky,
        )

        failing = stage1_collect._identify_failing_simulated(_MIN_CONFIG)
        assert failing == ["WIN_M5"]

    def test_sem_evaluator_disponivel_retorna_vazio(self, monkeypatch):
        # Caminho ImportError: se evaluate_baseline não pode ser importado,
        # a função falha-safe retornando [] (não derruba o pipeline).
        import builtins
        real_import = builtins.__import__

        def _block(name, *a, **k):
            if name == "optimization.agi_v4.backtest_evaluator":
                raise ImportError("simulado")
            return real_import(name, *a, **k)

        # Remove do cache de módulos para forçar re-import via __import__.
        monkeypatch.delitem(sys.modules, "optimization.agi_v4.backtest_evaluator",
                            raising=False)
        monkeypatch.setattr(builtins, "__import__", _block)

        assert stage1_collect._identify_failing_simulated(_MIN_CONFIG) == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
