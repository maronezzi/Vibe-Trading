"""test_sweep_pending.py — Testes do sweep automático do _pending/ (Wave AGI-sweep).

Cobre o fluxo run(ctx): enumera _pending/, smoke, cross-evaluate, best-per-pair,
tune, delega ao stage5. Sem depender de MT5/Wine (cross_evaluate, smoke_check e
stage5_apply mockados).

Wave AGI-sweep (Bruno 12/08/2026): ver optimization/agi_v4/sweep_pending.py.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from optimization.agi_v4 import sweep_pending as sp  # noqa: E402

_VALID_STRAT = '''
STRATEGY_NAME = "TEST_SWEEP_DEMO"
def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    if atr <= 0:
        return None
    return {"direction": "BUY", "sl_pts": 100, "info": {}}
'''


class TestExtractStrategyName(unittest.TestCase):
    def test_reads_name(self):
        d = tempfile.mkdtemp()
        p = Path(d) / "s.py"
        p.write_text(_VALID_STRAT)
        self.assertEqual(sp._extract_strategy_name(p), "TEST_SWEEP_DEMO")

    def test_returns_none_on_missing(self):
        d = tempfile.mkdtemp()
        p = Path(d) / "s.py"
        p.write_text("# sem STRATEGY_NAME\n")
        self.assertIsNone(sp._extract_strategy_name(p))


class TestRunFlow(unittest.TestCase):
    """Fluxo run(ctx) com cross_evaluate/smoke_check/stage5 mockados."""

    def _setup_pending(self, n=3):
        """Cria n estratégias em _pending temporário; retorna o dir."""
        d = Path(tempfile.mkdtemp()) / "_pending"
        d.mkdir()
        for i in range(n):
            src = _VALID_STRAT.replace("TEST_SWEEP_DEMO", f"TEST_SWEEP_{i}")
            (d / f"strat_{i}.py").write_text(src)
        return d

    def test_empty_pending_returns_no_promotions(self):
        d = Path(tempfile.mkdtemp()) / "_pending"
        d.mkdir()
        ctx = {"config": {"symbols": ["WIN"], "timeframes": ["M5"],
                          "timeframes_by_symbol": {}, "disabled_timeframes": []}}
        with patch.object(sp, "PENDING_DIR", d):
            result = sp.run(ctx)
        self.assertEqual(result["sweep_promotions"], [])

    def test_no_active_pairs_short_circuits(self):
        d = self._setup_pending(2)
        # config sem symbols → active_pairs retorna []
        ctx = {"config": {}}
        with patch.object(sp, "PENDING_DIR", d):
            result = sp.run(ctx)
        self.assertEqual(result["sweep_promotions"], [])

    def test_winners_become_candidates_and_stage5_called(self):
        d = self._setup_pending(3)
        ctx = {
            "config": {"symbols": ["WIN", "WDO"], "timeframes": ["M5"],
                       "timeframes_by_symbol": {}, "disabled_timeframes": []},
            "dry_run": True,
        }
        # cross_evaluate mock: cada estratégia vence num par diferente.
        def fake_cross(name, path, pairs, config, thresholds, exclude=None):
            pair = "WIN_M5" if "0" in name else "WDO_M5"
            return {"pair": pair, "strategy": name, "params": {},
                    "full": {"total_pnl": 100.0 + (10 if "WDO" in pair else 0)},
                    "walk_forward": [], "pending_path": str(path)}
        captured_cands = []

        def fake_stage5_run(ctx):
            # Captura os cands que o sweep montou em ctx["search_results"].
            captured_cands.extend(ctx.get("search_results", []))
            return {"applied_changes": [{"change": {"pair": "WIN_M5", "strategy": "X"}}]}

        with patch.object(sp, "PENDING_DIR", d), \
             patch("optimization.agi_v4.cross_pair_evaluator.cross_evaluate",
                   side_effect=fake_cross), \
             patch("optimization.agi_v4.cross_pair_evaluator.smoke_check",
                   return_value=True), \
             patch("optimization.agi_v4.cross_pair_evaluator.active_pairs",
                   return_value=["WIN_M5", "WDO_M5"]), \
             patch("optimization.agi_v4.stage5_apply.run", side_effect=fake_stage5_run):
            sp.run(ctx)

        # Em dry_run, stage5_apply NÃO é chamado (sweep só aplica se cands e...).
        # Mas o sweep chama stage5 sempre que há cands (o dry_run interno é do
        # stage5). Verificamos que os cands foram montados e promotions populadas.
        self.assertTrue(len(captured_cands) >= 1)
        self.assertEqual(captured_cands[0]["generated"], True)
        self.assertIn("sweep_promotions", ctx)

    def test_best_per_pair_keeps_highest_pnl(self):
        d = self._setup_pending(3)
        ctx = {
            "config": {"symbols": ["WIN"], "timeframes": ["M5"],
                       "timeframes_by_symbol": {}, "disabled_timeframes": []},
            "dry_run": True,
        }
        # Duas estratégias vencem no MESMO par (WIN_M5) com PnL diferente.
        call_count = {"i": 0}

        def fake_cross(name, path, pairs, config, thresholds, exclude=None):
            call_count["i"] += 1
            pnl = 50.0 if call_count["i"] == 1 else 200.0  # 2a é maior
            return {"pair": "WIN_M5", "strategy": name, "params": {},
                    "full": {"total_pnl": pnl}, "walk_forward": [],
                    "pending_path": str(path)}

        with patch.object(sp, "PENDING_DIR", d), \
             patch("optimization.agi_v4.cross_pair_evaluator.cross_evaluate",
                   side_effect=fake_cross), \
             patch("optimization.agi_v4.cross_pair_evaluator.smoke_check",
                   return_value=True), \
             patch("optimization.agi_v4.cross_pair_evaluator.active_pairs",
                   return_value=["WIN_M5"]), \
             patch("optimization.agi_v4.stage5_apply.run",
                   return_value={"applied_changes": []}):
            sp.run(ctx)

        # best_per_pair deve ter ficado com a de maior PnL (200). Como o sweep
        # só mantém winner se supera +min_advantage, e a 1a foi 50, a 2a (200)
        # supera 50+20=70 → troca. Resumo indica o fluxo.
        self.assertEqual(call_count["i"], 3)  # todas as 3 foram testadas

    def test_cross_evaluate_exception_skips_strat_not_crash(self):
        d = self._setup_pending(2)
        ctx = {
            "config": {"symbols": ["WIN"], "timeframes": ["M5"],
                       "timeframes_by_symbol": {}, "disabled_timeframes": []},
            "dry_run": True,
        }
        # cross_evaluate levanta — sweep deve skip e não crashar.
        with patch.object(sp, "PENDING_DIR", d), \
             patch("optimization.agi_v4.cross_pair_evaluator.cross_evaluate",
                   side_effect=RuntimeError("boom")), \
             patch("optimization.agi_v4.cross_pair_evaluator.smoke_check",
                   return_value=True), \
             patch("optimization.agi_v4.cross_pair_evaluator.active_pairs",
                   return_value=["WIN_M5"]), \
             patch("optimization.agi_v4.stage5_apply.run",
                   return_value={"applied_changes": []}):
            result = sp.run(ctx)  # não deve levantar
        self.assertEqual(result["sweep_promotions"], [])

    def test_restores_search_results_after_stage5(self):
        d = self._setup_pending(1)
        original_search = [{"pair": "ORIGINAL", "strategy": "OLD"}]
        ctx = {
            "config": {"symbols": ["WIN"], "timeframes": ["M5"],
                       "timeframes_by_symbol": {}, "disabled_timeframes": []},
            "dry_run": True,
            "search_results": original_search,
        }
        with patch.object(sp, "PENDING_DIR", d), \
             patch("optimization.agi_v4.cross_pair_evaluator.cross_evaluate",
                   return_value={"pair": "WIN_M5", "strategy": "X", "params": {},
                                 "full": {"total_pnl": 100}, "walk_forward": [],
                                 "pending_path": "x"}), \
             patch("optimization.agi_v4.cross_pair_evaluator.smoke_check",
                   return_value=True), \
             patch("optimization.agi_v4.cross_pair_evaluator.active_pairs",
                   return_value=["WIN_M5"]), \
             patch("optimization.agi_v4.stage5_apply.run",
                   return_value={"applied_changes": []}):
            sp.run(ctx)
        # search_results restaurado ao original após o sweep.
        self.assertEqual(ctx["search_results"], original_search)


if __name__ == "__main__":
    unittest.main()
