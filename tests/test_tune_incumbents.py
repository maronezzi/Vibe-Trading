"""test_tune_incumbents.py — Testes do tuning fino de incumbentes (Wave AGI-tune-incumbents).

Cobre run(ctx): para cada par que opera uma AGI4, roda tune_strategy + re-sim +
delega ao stage5. Sem MT5 (tune_strategy, evaluate_candidate e stage5 mockados).
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from optimization.agi_v4 import tune_incumbents as ti  # noqa: E402


class TestRunFlow(unittest.TestCase):
    def test_no_incumbents_returns_empty(self):
        ctx = {"config": {"strategy_by_tf": {"WIN_M5": "BOLLINGER"},
                          "disabled_timeframes": []}}
        with patch("optimization.agi_v4.param_tuner.tune_strategy") as m:
            result = ti.run(ctx)
        self.assertEqual(result["incumbent_tunings"], [])
        m.assert_not_called()  # BOLLINGER não é AGI4 — não tunca

    def test_disabled_incumbent_skipped(self):
        ctx = {"config": {"strategy_by_tf": {"WIN_H1": "AGI4_X"},
                          "disabled_timeframes": ["WIN_H1"]}}
        with patch("optimization.agi_v4.param_tuner.tune_strategy") as m:
            ti.run(ctx)
        m.assert_not_called()  # WIN_H1 disabled → skip

    def test_incumbent_tuned_becomes_candidate(self):
        ctx = {
            "config": {"strategy_by_tf": {"WIN_M5": "AGI4_TEST"},
                       "disabled_timeframes": []},
            "dry_run": True,
        }
        captured = []

        def fake_stage5(ctx):
            captured.extend(ctx.get("search_results", []))
            return {"applied_changes": [{"change": {"pair": "WIN_M5"}}]}

        with patch("optimization.agi_v4.param_tuner.tune_strategy",
                   return_value={"ema_fast": 7}), \
             patch("optimization.exhaustive_strategy_search.strategy_path_by_name",
                   return_value="/x/AGI4_TEST.py"), \
             patch("optimization.agi_v4.backtest_evaluator.evaluate_candidate",
                   return_value={"passed": True, "full": {"total_pnl": 200},
                                 "walk_forward": []}), \
             patch("optimization.agi_v4.stage5_apply.run", side_effect=fake_stage5):
            ti.run(ctx)

        self.assertTrue(len(captured) >= 1)
        # Incumbente: NÃO deve ter "generated" (não é promoção de _pending/).
        self.assertNotIn("generated", captured[0])
        self.assertNotIn("pending_path", captured[0])
        self.assertEqual(captured[0]["strategy"], "AGI4_TEST")
        self.assertEqual(captured[0]["params"], {"ema_fast": 7})

    def test_tune_returns_none_skips(self):
        # tune_strategy retorna None (default era melhor) → sem candidato.
        ctx = {
            "config": {"strategy_by_tf": {"WIN_M5": "AGI4_TEST"},
                       "disabled_timeframes": []},
            "dry_run": True,
        }
        with patch("optimization.agi_v4.param_tuner.tune_strategy",
                   return_value=None), \
             patch("optimization.exhaustive_strategy_search.strategy_path_by_name",
                   return_value="/x/AGI4_TEST.py"), \
             patch("optimization.agi_v4.stage5_apply.run") as m:
            result = ti.run(ctx)
        m.assert_not_called()  # nada a aplicar
        self.assertEqual(result["incumbent_tunings"], [])

    def test_restores_search_results(self):
        original = [{"pair": "X", "strategy": "OLD"}]
        ctx = {
            "config": {"strategy_by_tf": {"WIN_M5": "AGI4_TEST"},
                       "disabled_timeframes": []},
            "dry_run": True,
            "search_results": original,
        }
        with patch("optimization.agi_v4.param_tuner.tune_strategy",
                   return_value={"p": 1}), \
             patch("optimization.exhaustive_strategy_search.strategy_path_by_name",
                   return_value="/x.py"), \
             patch("optimization.agi_v4.backtest_evaluator.evaluate_candidate",
                   return_value={"passed": True, "full": {}, "walk_forward": []}), \
             patch("optimization.agi_v4.stage5_apply.run",
                   return_value={"applied_changes": []}):
            ti.run(ctx)
        self.assertEqual(ctx["search_results"], original)


if __name__ == "__main__":
    unittest.main()
