"""
test_optimization_helpers.py — TDD tests for optimization/pair_optimizer.py

Cobre:
  1. find_best_strategies_for_pair retorna top N e respeita n_top
  2. Filtra estratégias com n_trades < MIN_N_TRADES
  3. Ordenação é por avg_pnl (PnL/n), não PnL total
  4. optimize_pair_with_evidence retorna tupla completa (evidence block)
  5. _bayesian_refine roda N trials e respeita timeout_sec

Os testes usam MonkeyPatch para stub fetch_bars_for_backtest e
simulate_forward quando relevante, e fallback real (MT5 disponível)
quando não. Marcados com @pytest.mark.slow para os pesados.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "optimization"))

from optimization import pair_optimizer as po  # noqa: E402


class TestFindBestStrategiesStructure(unittest.TestCase):
    """Verifica formato de retorno e contrato básico da API."""

    def test_returns_list_with_strategy_key(self):
        """find_best_strategies_for_pair retorna lista de dicts com chave 'strategy'."""
        # Stub fetch_bars + simulate_forward para não depender de MT5
        with patch.object(po, "_fetch_bars_cached", return_value=[{"x": 1}] * 100):
            with patch.object(po, "simulate_forward") as sim:
                # 3 estratégias com n >= 5
                def fake_sim(symbol, tf, bars, strat, params, config=None):
                    if strat == "STRAT_A":
                        return {"pnl": 50.0, "n_trades": 5, "wr": 60.0,
                                "max_dd": 10.0, "decision": "ok"}
                    if strat == "STRAT_B":
                        return {"pnl": 200.0, "n_trades": 10, "wr": 70.0,
                                "max_dd": 30.0, "decision": "ok"}
                    if strat == "STRAT_C":
                        return {"pnl": -10.0, "n_trades": 7, "wr": 30.0,
                                "max_dd": 50.0, "decision": "negative"}
                    return {"pnl": 0, "n_trades": 0, "wr": 0,
                            "max_dd": 0, "decision": "no_trades"}

                sim.side_effect = fake_sim
                # Stub discover_strategies
                with patch.object(po, "discover_strategies",
                                  return_value=["STRAT_A", "STRAT_B", "STRAT_C"]):
                    result = po.find_best_strategies_for_pair("WIN", "M5", n_top=3)

        self.assertIsInstance(result, list)
        self.assertGreaterEqual(len(result), 1)
        for r in result:
            self.assertIn("strategy", r)
            self.assertIn("pnl", r)
            self.assertIn("n_trades", r)
            self.assertIn("avg_pnl", r)
            self.assertIn("wr", r)
            self.assertIn("params", r)


class TestFindBestFiltersMinTrades(unittest.TestCase):
    """Filtro estatístico: n_trades < MIN_N_TRADES deve ser excluído."""

    def test_filters_strategies_with_n_trades_below_min(self):
        """Estratégias com n_trades < MIN_N_TRADES (5) devem ser excluídas."""
        captured = {}

        def fake_sim(symbol, tf, bars, strat, params, config=None):
            captured[strat] = True
            if strat == "GOOD":
                return {"pnl": 100.0, "n_trades": 8, "wr": 60.0,
                        "max_dd": 10.0, "decision": "ok"}
            if strat == "TOO_FEW":
                return {"pnl": 50.0, "n_trades": 2, "wr": 80.0,
                        "max_dd": 5.0, "decision": "ok"}
            if strat == "ZERO":
                return {"pnl": 0, "n_trades": 0, "wr": 0,
                        "max_dd": 0, "decision": "no_trades"}
            return {"pnl": 0, "n_trades": 0, "wr": 0,
                    "max_dd": 0, "decision": "no_trades"}

        with patch.object(po, "_fetch_bars_cached", return_value=[{"x": 1}] * 100):
            with patch.object(po, "simulate_forward", side_effect=fake_sim):
                with patch.object(po, "discover_strategies",
                                  return_value=["GOOD", "TOO_FEW", "ZERO"]):
                    result = po.find_best_strategies_for_pair("WIN", "M5", n_top=3)

        # Só GOOD deve passar (TOO_FEW e ZERO ficam abaixo de MIN_N_TRADES=5)
        self.assertEqual(len(result), 1, f"esperava 1, recebi {len(result)}")
        self.assertEqual(result[0]["strategy"], "GOOD")
        self.assertEqual(result[0]["n_trades"], 8)


class TestFindBestOrdersByAvgPnl(unittest.TestCase):
    """Ordenação por avg_pnl (PnL/n), não PnL total — protege contra n pequeno."""

    def test_ordering_by_avg_pnl_not_total(self):
        """Estratégia com avg_pnl maior deve estar primeiro mesmo se PnL total menor."""
        def fake_sim(symbol, tf, bars, strat, params, config=None):
            if strat == "HIGH_AVG":
                # PnL total pequeno mas avg_pnl alto
                return {"pnl": 60.0, "n_trades": 6, "wr": 70.0,
                        "max_dd": 10.0, "decision": "ok"}
            if strat == "HIGH_TOTAL":
                # PnL total grande mas avg_pnl baixo
                return {"pnl": 300.0, "n_trades": 50, "wr": 50.0,
                        "max_dd": 100.0, "decision": "ok"}
            return {"pnl": 0, "n_trades": 0, "wr": 0,
                    "max_dd": 0, "decision": "no_trades"}

        with patch.object(po, "_fetch_bars_cached", return_value=[{"x": 1}] * 100):
            with patch.object(po, "simulate_forward", side_effect=fake_sim):
                with patch.object(po, "discover_strategies",
                                  return_value=["HIGH_AVG", "HIGH_TOTAL"]):
                    result = po.find_best_strategies_for_pair("WIN", "M5", n_top=3)

        self.assertEqual(len(result), 2)
        # HIGH_AVG (avg_pnl=10.0) deve vir ANTES de HIGH_TOTAL (avg_pnl=6.0)
        # mesmo com PnL total menor (60 vs 300)
        self.assertEqual(result[0]["strategy"], "HIGH_AVG")
        self.assertEqual(result[1]["strategy"], "HIGH_TOTAL")
        # Sanity: avg_pnl[0] > avg_pnl[1]
        self.assertGreater(result[0]["avg_pnl"], result[1]["avg_pnl"])
        # avg_pnl = PnL / n_trades
        self.assertAlmostEqual(result[0]["avg_pnl"], 60.0 / 6, places=2)
        self.assertAlmostEqual(result[1]["avg_pnl"], 300.0 / 50, places=2)


class TestOptimizeWithEvidenceReturnsCompleteTuple(unittest.TestCase):
    """optimize_pair_with_evidence retorna tupla COMPLETA com evidence block."""

    def test_returns_full_evidence_dict(self):
        """Resultado deve conter: best_strategy, params, pnl, wr, n_trades, evidence."""
        # Stub: top 2 estratégias e refine determinístico
        def fake_top(symbol, tf, n_top=3, bar_count=None):
            return [
                {"strategy": "WINNER", "pnl": 100.0, "n_trades": 8,
                 "wr": 60.0, "avg_pnl": 12.5, "max_dd": 10.0,
                 "decision": "ok", "params": {}},
                {"strategy": "LOSER", "pnl": -50.0, "n_trades": 6,
                 "wr": 30.0, "avg_pnl": -8.3, "max_dd": 50.0,
                 "decision": "negative", "params": {}},
            ][:n_top]

        def fake_bayes(symbol, tf, strat, bars, config,
                       max_evals=50, seed_params=None, timeout_sec=120):
            if strat == "WINNER":
                return {
                    "strategy": "WINNER",
                    "best_params": {"sl_atr_mult": 1.5, "cooldown_seconds": 300},
                    "best_avg_pnl": 15.0, "best_pnl": 120.0,
                    "best_n_trades": 8, "best_wr": 62.5, "best_max_dd": 12.0,
                    "raw_score": 15.0, "complexity_penalty": 0.94,
                    "n_params": 2, "n_trials": 30, "elapsed_seconds": 1.2,
                    "decision": "ok",
                }
            return {
                "strategy": "LOSER",
                "best_params": {"sl_atr_mult": 1.0},
                "best_avg_pnl": -5.0, "best_pnl": -30.0,
                "best_n_trades": 6, "best_wr": 33.0, "best_max_dd": 40.0,
                "raw_score": -5.0, "complexity_penalty": 0.96,
                "n_params": 1, "n_trials": 30, "elapsed_seconds": 1.1,
                "decision": "negative",
            }

        with patch.object(po, "_fetch_bars_cached", return_value=[{"x": 1}] * 100):
            with patch.object(po, "find_best_strategies_for_pair", side_effect=fake_top):
                with patch.object(po, "_bayesian_refine", side_effect=fake_bayes):
                    result = po.optimize_pair_with_evidence("WIN", "M5", n_top=2,
                                                            max_evals=30)

        # Tupla completa esperada
        self.assertIsNotNone(result)
        self.assertEqual(result["symbol"], "WIN")
        self.assertEqual(result["tf"], "M5")
        self.assertEqual(result["best_strategy"], "WINNER")
        self.assertIn("best_params", result)
        self.assertEqual(result["best_pnl"], 120.0)
        self.assertEqual(result["best_n_trades"], 8)
        self.assertEqual(result["best_wr"], 62.5)
        self.assertEqual(result["best_avg_pnl"], 15.0)
        # Evidence block
        self.assertIn("evidence", result)
        ev = result["evidence"]
        self.assertIn("n_trials_per_strategy", ev)
        self.assertIn("top_n_evaluated", ev)
        self.assertIn("all_results", ev)
        self.assertGreaterEqual(ev["top_n_evaluated"], 1)
        # Decisão
        self.assertIn("decision", result)
        self.assertIn(result["decision"], ("ok", "negative", "no_data"))


class TestBayesianRefineRunsTrials(unittest.TestCase):
    """_bayesian_refine roda N trials e respeita timeout_sec."""

    def test_respects_max_evals_and_returns_n_trials(self):
        """max_evals=5 → n_trials >= 1 (1 trial seed + até 4 mutações)."""

        call_count = {"n": 0}

        def fake_sim(symbol, tf, bars, strat, params, config=None):
            call_count["n"] += 1
            # Cada trial gera um avg_pnl diferente → score diferente
            n_t = call_count["n"]
            # Alterna entre score bom e ruim para forçar exploração
            if n_t % 2 == 0:
                return {"pnl": 100.0, "n_trades": 8, "wr": 60.0,
                        "max_dd": 10.0, "decision": "ok"}
            return {"pnl": -10.0, "n_trades": 8, "wr": 40.0,
                    "max_dd": 30.0, "decision": "negative"}

        # Stub module-level _load_strategy_utils etc — mas _bayesian_refine
        # só usa simulate_forward direto, então é seguro
        with patch.object(po, "simulate_forward", side_effect=fake_sim):
            result = po._bayesian_refine(
                "WIN", "M5", "TEST_STRAT", [{"x": 1}] * 100, {},
                max_evals=5, timeout_sec=30, seed_params={"sl_atr_mult": 1.5},
            )

        # Estrutura do retorno
        self.assertEqual(result["strategy"], "TEST_STRAT")
        self.assertIn("best_params", result)
        self.assertIn("best_avg_pnl", result)
        self.assertIn("best_pnl", result)
        self.assertIn("best_n_trades", result)
        self.assertIn("best_wr", result)
        self.assertIn("raw_score", result)
        self.assertIn("complexity_penalty", result)
        self.assertIn("n_trials", result)
        self.assertIn("elapsed_seconds", result)
        # Pelo menos 1 trial foi feito (seed + mutações)
        self.assertGreaterEqual(result["n_trials"], 1)
        # Não pode ter rodado MUITO mais do que pediu (allowance: 1 seed + N)
        self.assertLessEqual(result["n_trials"], 10)


class TestNoBarsReturnsEmpty(unittest.TestCase):
    """Quando MT5 não retorna bars, funções devem retornar vazio/None."""

    def test_find_best_returns_empty_when_no_bars(self):
        with patch.object(po, "_fetch_bars_cached", return_value=[]):
            result = po.find_best_strategies_for_pair("WIN", "M5", n_top=3)
        self.assertEqual(result, [])

    def test_optimize_returns_none_when_no_bars(self):
        with patch.object(po, "_fetch_bars_cached", return_value=[]):
            result = po.optimize_pair_with_evidence("WIN", "M5", n_top=3,
                                                    max_evals=10)
        self.assertIsNone(result)


class TestOccamPenalty(unittest.TestCase):
    """_occam_penalize aplica penalidade decrescente com complexidade."""

    def test_penalty_decreases_with_more_params(self):
        # Sem params: 1.0 (sem penalidade)
        self.assertAlmostEqual(po._occam_penalize(10.0, 0), 10.0, places=4)
        # 5 params: 10 * (1 - 0.02*5) = 10 * 0.9 = 9.0
        self.assertAlmostEqual(po._occam_penalize(10.0, 5), 9.0, places=4)
        # 10 params: 10 * (1 - 0.02*10) = 10 * 0.8 = 8.0
        self.assertAlmostEqual(po._occam_penalize(10.0, 10), 8.0, places=4)
        # Penalidade nunca negativa (clamp a 0)
        self.assertAlmostEqual(po._occam_penalize(10.0, 100), 0.0, places=4)


if __name__ == "__main__":
    unittest.main()