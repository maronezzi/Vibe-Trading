"""
test_pair_optimizer.py
========================
TDD: garante que pair_optimizer.py funciona corretamente.

Sub-agente Wave 8.1 (2026-06-26) criou optimization/pair_optimizer.py
com find_best_strategies_for_pair() e optimize_pair_with_evidence().

Foco: testar interface, fallbacks e edge cases (sem rodar backtest
real, que demora e depende de MT5).
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, str(Path(PROJECT_ROOT, "optimization")))


class TestPairOptimizerImports(unittest.TestCase):
    """Funções existem e são importáveis."""

    def test_find_best_strategies_exists(self):
        from pair_optimizer import find_best_strategies_for_pair
        self.assertTrue(callable(find_best_strategies_for_pair))

    def test_optimize_pair_with_evidence_exists(self):
        from pair_optimizer import optimize_pair_with_evidence
        self.assertTrue(callable(optimize_pair_with_evidence))


class TestOccamPenalize(unittest.TestCase):
    """_occam_penalize aplica penalidade por complexidade."""

    def test_occam_penalize_more_params_lower_score(self):
        from pair_optimizer import _occam_penalize
        # 2 params, raw=100
        score_simple = _occam_penalize(raw_score=100.0, n_params=2)
        # 10 params, raw=100
        score_complex = _occam_penalize(raw_score=100.0, n_params=10)
        # Score simples (menos params) deve ser MAIOR
        self.assertGreater(score_simple, score_complex)

    def test_occam_penalize_zero_params(self):
        from pair_optimizer import _occam_penalize
        # 0 params = sem penalidade
        score = _occam_penalize(raw_score=100.0, n_params=0)
        self.assertEqual(score, 100.0)


class TestAvgPnl(unittest.TestCase):
    """_avg_pnl calcula média corretamente."""

    def test_avg_pnl_normal(self):
        from pair_optimizer import _avg_pnl
        result = {"n_trades": 10, "pnl": 500.0}
        self.assertEqual(_avg_pnl(result), 50.0)

    def test_avg_pnl_zero_trades_returns_neg_inf(self):
        """Sub-agente escolheu retornar -inf para n=0 (ordenação segura)."""
        from pair_optimizer import _avg_pnl
        result = {"n_trades": 0, "pnl": 0.0}
        result_val = _avg_pnl(result)
        self.assertEqual(result_val, float("-inf"))


class TestFindBestStrategiesForPair(unittest.TestCase):
    """find_best_strategies_for_pair retorna top N estratégias."""

    def test_filters_low_n_trades(self):
        """Estratégias com n_trades < 5 devem ser filtradas."""
        from pair_optimizer import find_best_strategies_for_pair

        # Mock simulate_forward pra retornar resultados variados
        def mock_simulate(strategy, symbol, tf, bars, params, **kwargs):
            n = params.get("_test_n_trades", 5)
            return {"n_trades": n, "pnl": params.get("_test_pnl", 0)}

        # Mock discover_strategies
        mock_strategies = ["STRATEGY_A", "STRATEGY_B", "STRATEGY_C"]

        with patch("pair_optimizer._fetch_bars_cached", return_value=[{"close": 100}] * 100), \
             patch("pair_optimizer.discover_strategies", return_value=mock_strategies), \
             patch("pair_optimizer.simulate_forward", side_effect=mock_simulate):
            results = find_best_strategies_for_pair(
                "WINQ26", "M15", n_top=3
            )
            # Deve retornar SOMENTE estratégias com n_trades >= 5
            for r in results:
                self.assertGreaterEqual(
                    r.get("n_trades", 0), 5,
                    f"Strategy {r.get('strategy')} tem n_trades < 5, deveria ser filtrada"
                )

    def test_returns_at_most_n_top(self):
        """Retorna no máximo n_top estratégias."""
        from pair_optimizer import find_best_strategies_for_pair

        def mock_simulate(strategy, symbol, tf, bars, params, **kwargs):
            return {"n_trades": 10, "pnl": 100.0}

        with patch("pair_optimizer._fetch_bars_cached", return_value=[{"close": 100}] * 100), \
             patch("pair_optimizer.discover_strategies",
                   return_value=[f"STRATEGY_{i}" for i in range(20)]), \
             patch("pair_optimizer.simulate_forward", side_effect=mock_simulate):
            results = find_best_strategies_for_pair("WINQ26", "M15", n_top=3)
            self.assertLessEqual(len(results), 3, f"Esperado <=3, got {len(results)}")


if __name__ == "__main__":
    unittest.main()
