"""test_cross_pair_evaluator.py — Testes da avaliação cruzada de estratégias.

Cobre cross_pair_evaluator (load, register, smoke_check, cross_evaluate,
active_pairs) e a injeção do Stage 4 (_try_cross_pair_salvage) — sem depender
de MT5/Wine (evaluate_candidate e cross_evaluate mockados).

Wave cross-pair (Bruno 11/08/2026): ver scripts/sweep_pending_strategies.py e
optimization/agi_v4/cross_pair_evaluator.py.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from optimization.agi_v4 import cross_pair_evaluator as xeval  # noqa: E402


VALID_STRATEGY = '''
STRATEGY_NAME = "TEST_XPAIR_DEMO"
def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Demonstra contrato válido: retorna None ou dict com direction + sl_pts."""
    if atr <= 0:
        return None
    return {"direction": "BUY", "sl_pts": 100, "info": {}}
'''

# Bug de contrato clássico: indexar um escalar (TypeError em runtime). O ast_gate
# não vê (sintaxe ok); só o _runtime_smoke_gate detecta ao executar check_entry.
BROKEN_STRATEGY = '''
STRATEGY_NAME = "TEST_XPAIR_BROKEN"
def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    rsi = utils["calculate_rsi"](bars, 14)   # retorna escalar (float)
    return {"direction": "SELL", "sl_pts": int(rsi[0])}  # rsi[0] -> TypeError
'''


class TestSmokeCheck(unittest.TestCase):
    def test_valid_strategy_passes_smoke(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "valid.py"
            p.write_text(VALID_STRATEGY)
            g = xeval.smoke_check(p)
            self.assertTrue(g, f"estratégia válida deveria passar: {g.reason}")

    def test_broken_strategy_fails_runtime_smoke(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "broken.py"
            p.write_text(BROKEN_STRATEGY)
            g = xeval.smoke_check(p)
            self.assertFalse(g, "estratégia com bug de contrato deve falhar")
            self.assertEqual(g.gate_name, "runtime_smoke")


class TestRegisterStrategy(unittest.TestCase):
    def test_register_makes_findable_by_name(self):
        import core.vt_strategy_loader as loader
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "demo.py"
            p.write_text(VALID_STRATEGY)
            try:
                ok = xeval.register_strategy("TEST_XPAIR_DEMO", p)
                self.assertTrue(ok, "register_strategy deve injetar com sucesso")
                fn = loader.get_strategy_func("TEST_XPAIR_DEMO")
                self.assertIsNotNone(
                    fn, "get_strategy_func deve encontrar a estratégia injetada"
                )
            finally:
                loader._strategies.pop("TEST_XPAIR_DEMO", None)

    def test_register_returns_false_for_missing_check_entry(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "noentry.py"
            p.write_text('STRATEGY_NAME = "TEST_NO_ENTRY"\n# sem check_entry\n')
            ok = xeval.register_strategy("TEST_NO_ENTRY", p)
            self.assertFalse(ok)


class TestActivePairs(unittest.TestCase):
    def test_excludes_disabled_timeframes(self):
        config = {
            "symbols": ["WIN", "BIT", "WSP", "WDO"],
            "timeframes_by_symbol": {
                s: ["M5", "M15", "M30", "H1"]
                for s in ["WIN", "BIT", "WSP", "WDO"]
            },
            "disabled_timeframes": ["WDO_H1", "WIN_H1", "BIT_H1", "WIN_M5"],
        }
        pairs = xeval.active_pairs(config)
        self.assertEqual(len(pairs), 12)  # 16 - 4 disabled
        for disabled in ["WDO_H1", "WIN_H1", "BIT_H1", "WIN_M5"]:
            self.assertNotIn(disabled, pairs)
        self.assertIn("WSP_M5", pairs)
        self.assertIn("BIT_M30", pairs)


class TestCrossEvaluate(unittest.TestCase):
    def test_returns_winning_pair_excluding_target(self):
        """Estratégia falha em WIN_M5 (excluído), passa em WSP_H1 → retorna WSP_H1."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "demo.py"
            p.write_text(VALID_STRATEGY)

            def fake_eval(sym, tf, name, params, config, *, thresholds=None):
                if sym == "WIN" and tf == "M5":
                    return {"passed": False, "full": {"total_pnl": 0, "n_trades": 0},
                            "walk_forward": [], "reason": "0 trades"}
                if sym == "BIT" and tf == "M5":
                    return {"passed": False, "full": {}, "walk_forward": [], "reason": "fail"}
                if sym == "WSP" and tf == "H1":
                    return {"passed": True,
                            "full": {"total_pnl": 563.38, "n_trades": 19, "pf": 4.85},
                            "walk_forward": [{"total_pnl": 100}], "reason": ""}
                return {"passed": False, "full": {}, "walk_forward": [], "reason": "no"}

            with patch("optimization.agi_v4.backtest_evaluator.evaluate_candidate",
                       side_effect=fake_eval):
                winner = xeval.cross_evaluate(
                    "TEST_XPAIR_DEMO", p,
                    ["WIN_M5", "BIT_M5", "WSP_H1"], {}, thresholds={},
                    exclude={"WIN_M5"},
                )
            self.assertIsNotNone(winner, "deveria ter achado WSP_H1")
            self.assertEqual(winner["pair"], "WSP_H1")
            self.assertTrue(winner["generated"])
            self.assertEqual(winner["pending_path"], str(p))
            self.assertIn("profitability_full", winner["gates_passed"])

    def test_returns_none_when_no_pair_passes(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "demo.py"
            p.write_text(VALID_STRATEGY)
            with patch(
                "optimization.agi_v4.backtest_evaluator.evaluate_candidate",
                return_value={"passed": False, "full": {}, "walk_forward": [],
                              "reason": "fail"},
            ):
                winner = xeval.cross_evaluate(
                    "TEST_XPAIR_DEMO", p, ["WIN_M5", "BIT_M5"], {}, thresholds={}
                )
            self.assertIsNone(winner)

    def test_picks_highest_pnl_among_winners(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "demo.py"
            p.write_text(VALID_STRATEGY)

            def fake_eval(sym, tf, name, params, config, *, thresholds=None):
                # dois pares passam: WSP_M30 (PnL 890) e BIT_M30 (PnL 126)
                pnl = {"WSP": 890.88, "BIT": 126.49}.get(sym, 0)
                passed = sym in ("WSP", "BIT") and tf == "M30"
                return {"passed": passed,
                        "full": {"total_pnl": pnl, "n_trades": 10, "pf": 3.0},
                        "walk_forward": [], "reason": ""}

            with patch("optimization.agi_v4.backtest_evaluator.evaluate_candidate",
                       side_effect=fake_eval):
                winner = xeval.cross_evaluate(
                    "TEST_XPAIR_DEMO", p, ["WSP_M30", "BIT_M30"], {}, thresholds={}
                )
            self.assertEqual(winner["pair"], "WSP_M30")
            self.assertEqual(winner["full"]["total_pnl"], 890.88)


class TestStage4Salvage(unittest.TestCase):
    """Testa a injeção do Stage 4 (_try_cross_pair_salvage)."""

    def test_salvage_returns_approved_with_winning_pair(self):
        from optimization.agi_v4 import stage4_generate as s4
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "demo.py"
            p.write_text(VALID_STRATEGY)
            fake_winner = {
                "pair": "WSP_H1", "strategy": "TEST_XPAIR_DEMO", "params": {},
                "full": {"total_pnl": 563.38, "pf": 4.85, "n_trades": 19},
                "walk_forward": [{"total_pnl": 100}],
                "gates_passed": ["ast", "profitability_full", "walk_forward"],
                "generated": True, "pending_path": str(p),
            }
            with patch("optimization.agi_v4.cross_pair_evaluator.cross_evaluate",
                       return_value=fake_winner), \
                 patch("optimization.agi_v4.cross_pair_evaluator.active_pairs",
                       return_value=["WSP_H1", "BIT_M5"]):
                result = s4._try_cross_pair_salvage(
                    "TEST_XPAIR_DEMO", p, {"pair": "WIN_M5"}, "WIN_M5",
                    {"config": {}}, {},
                )
            self.assertIsNotNone(result)
            self.assertEqual(result["winning_pair"], "WSP_H1")
            self.assertEqual(result["backtest_gate"], "passed")
            self.assertEqual(result["backtest"]["total_pnl"], 563.38)

    def test_salvage_returns_none_when_no_winner(self):
        from optimization.agi_v4 import stage4_generate as s4
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "demo.py"
            p.write_text(VALID_STRATEGY)
            with patch("optimization.agi_v4.cross_pair_evaluator.cross_evaluate",
                       return_value=None), \
                 patch("optimization.agi_v4.cross_pair_evaluator.active_pairs",
                       return_value=["WSP_H1"]):
                result = s4._try_cross_pair_salvage(
                    "TEST_XPAIR_DEMO", p, {"pair": "WIN_M5"}, "WIN_M5",
                    {"config": {}}, {},
                )
            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
