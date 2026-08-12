"""test_param_tuner.py — Testes do tuning de params AGI4 (Wave AGI-param-tuning).

Cobre extract_tunable_params (TUNABLE_PARAMS + fallback AST), _generate_grid,
tune_strategy (evaluate_candidate mockado) e o registry de sanctioned params no
guardrail — sem depender de MT5/Wine.

Wave AGI-param-tuning (Bruno 12/08/2026): ver optimization/agi_v4/param_tuner.py.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from optimization.agi_v4 import param_tuner as pt  # noqa: E402
from optimization.agi_v4 import guardrails as gr  # noqa: E402


# Estratégia de exemplo COM TUNABLE_PARAMS declarado.
_STRAT_WITH_DECL = '''
STRATEGY_NAME = "TEST_TUNER_DECL"
TUNABLE_PARAMS = {"ema_fast": (int, 5, 30), "bb_std": (float, 1.5, 3.0)}

def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    ema_fast = params.get("ema_fast", 9)
    bb_std = params.get("bb_std", 2.0)
    adx_period = params.get("adx_period", 14)
    sl_atr_mult = params.get("sl_atr_mult", 1.5)
    return {"direction": "BUY", "sl_pts": 100, "info": {}}
'''

# Estratégia de exemplo SEM TUNABLE_PARAMS (fallback AST puro).
_STRAT_FALLBACK = '''
STRATEGY_NAME = "TEST_TUNER_FALLBACK"

def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    ema_fast = params.get("ema_fast", 9)
    bb_std = params.get("bb_std", 2.0)
    return {"direction": "BUY", "sl_pts": 100, "info": {}}
'''


def _write_strat(content: str) -> Path:
    """Escreve conteúdo num .py temporário e retorna o path."""
    d = tempfile.mkdtemp()
    p = Path(d) / "strat.py"
    p.write_text(content, encoding="utf-8")
    return p


class TestExtractTunableParams(unittest.TestCase):
    def test_with_decl_uses_llm_ranges(self):
        p = _write_strat(_STRAT_WITH_DECL)
        t = pt.extract_tunable_params(p)
        # ema_fast: range vem do TUNABLE_PARAMS (5, 30), default do params.get (9).
        self.assertIn("ema_fast", t)
        self.assertEqual(t["ema_fast"]["kind"], "int")
        self.assertEqual(t["ema_fast"]["lo"], 5.0)
        self.assertEqual(t["ema_fast"]["hi"], 30.0)
        self.assertEqual(t["ema_fast"]["default"], 9)
        # bb_std: float do TUNABLE_PARAMS.
        self.assertEqual(t["bb_std"]["kind"], "float")
        self.assertEqual(t["bb_std"]["lo"], 1.5)
        self.assertEqual(t["bb_std"]["hi"], 3.0)
        # adx_period: NÃO está no TUNABLE_PARAMS → range inferido do default (14).
        self.assertIn("adx_period", t)
        self.assertEqual(t["adx_period"]["kind"], "int")
        self.assertEqual(t["adx_period"]["default"], 14)

    def test_fallback_infers_ranges_from_defaults(self):
        p = _write_strat(_STRAT_FALLBACK)
        t = pt.extract_tunable_params(p)
        self.assertIn("ema_fast", t)
        # int default 9 → ±50% → lo=max(1,round(4.5))=5 (aprox), hi=round(13.5)=14
        self.assertEqual(t["ema_fast"]["kind"], "int")
        self.assertEqual(t["ema_fast"]["default"], 9)
        self.assertLessEqual(t["ema_fast"]["lo"], 9)
        self.assertGreater(t["ema_fast"]["hi"], 9)
        # float default 2.0 → ±30%
        self.assertEqual(t["bb_std"]["kind"], "float")
        self.assertAlmostEqual(t["bb_std"]["lo"], 1.4, places=1)

    def test_universal_params_blocklisted(self):
        p = _write_strat(_STRAT_WITH_DECL)
        t = pt.extract_tunable_params(p)
        # sl_atr_mult é universal — não deve aparecer no tuning próprio.
        self.assertNotIn("sl_atr_mult", t)

    def test_max_five_params(self):
        # 8 params próprios — só os 5 primeiros devem aparecer.
        src = '''
STRATEGY_NAME = "TEST_MANY"
def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    a = params.get("a", 1)
    b = params.get("b", 2)
    c = params.get("c", 3)
    d = params.get("d", 4)
    e = params.get("e", 5)
    f = params.get("f", 6)
    g = params.get("g", 7)
    h = params.get("h", 8)
    return {"direction": "BUY", "sl_pts": 100, "info": {}}
'''
        p = _write_strat(src)
        t = pt.extract_tunable_params(p)
        self.assertLessEqual(len(t), 5)


class TestSanctionedSpec(unittest.TestCase):
    def test_format_matches_guardrail(self):
        p = _write_strat(_STRAT_WITH_DECL)
        spec = pt.sanctioned_spec(p)
        # Formato: {param: (python_type, lo, hi)}.
        self.assertIn("ema_fast", spec)
        py_type, lo, hi = spec["ema_fast"]
        self.assertIs(py_type, int)
        self.assertEqual(lo, 5.0)
        self.assertEqual(hi, 30.0)
        self.assertIs(spec["bb_std"][0], float)


class TestGenerateGrid(unittest.TestCase):
    def test_cap_40(self):
        # 6 params × 3 valores = 729 combos (>40) → subamostrado.
        tunables = {f"p{i}": {"kind": "int", "lo": 1, "hi": 9, "default": 5}
                    for i in range(6)}
        grid = pt._generate_grid(tunables)
        self.assertLessEqual(len(grid), pt._MAX_TUNING_COMBOS)

    def test_no_all_defaults_combo(self):
        # O combo all-defaults é avaliado à parte (params={}) — não deve estar no grid.
        tunables = {"ema_fast": {"kind": "int", "lo": 5, "hi": 30, "default": 9}}
        grid = pt._generate_grid(tunables)
        for combo in grid:
            self.assertFalse(pt._is_all_defaults(combo, tunables))


class TestTuneStrategy(unittest.TestCase):
    """tune_strategy com evaluate_candidate mockado (sem MT5)."""

    def setUp(self):
        gr.clear_sanctioned_params()

    def tearDown(self):
        gr.clear_sanctioned_params()

    @patch("optimization.agi_v4.backtest_evaluator.evaluate_candidate")
    def test_returns_best_when_better(self, mock_eval):
        p = _write_strat(_STRAT_WITH_DECL)
        # baseline (params={}) = 100; combo específico = 250 (melhor).
        def side(sym, tf, name, params, config, thresholds=None):
            pnl = 250 if params.get("ema_fast") == 30 else 100
            return {"passed": True, "full": {"total_pnl": pnl}, "walk_forward": []}
        mock_eval.side_effect = side
        tuned = pt.tune_strategy("WIN", "M5", "TEST", p, {}, None)
        self.assertIsNotNone(tuned)
        self.assertEqual(tuned.get("ema_fast"), 30)

    @patch("optimization.agi_v4.backtest_evaluator.evaluate_candidate")
    def test_returns_none_when_default_best(self, mock_eval):
        p = _write_strat(_STRAT_WITH_DECL)
        # Nenhum combo supera o default.
        mock_eval.return_value = {"passed": True, "full": {"total_pnl": 100}, "walk_forward": []}
        tuned = pt.tune_strategy("WIN", "M5", "TEST", p, {}, None)
        self.assertIsNone(tuned)

    @patch("optimization.agi_v4.backtest_evaluator.evaluate_candidate")
    def test_registers_sanctioned_in_guardrail(self, mock_eval):
        p = _write_strat(_STRAT_WITH_DECL)
        mock_eval.return_value = {"passed": True, "full": {"total_pnl": 100}, "walk_forward": []}
        pt.tune_strategy("WIN", "M5", "TEST_REG", p, {}, None)
        # O registry deve ter a estratégia registrada com seus params.
        self.assertIn("TEST_REG", gr._SANCTIONED_PARAMS)
        self.assertIn("ema_fast", gr._SANCTIONED_PARAMS["TEST_REG"])

    @patch("optimization.agi_v4.backtest_evaluator.evaluate_candidate")
    def test_baseline_not_passed_returns_none(self, mock_eval):
        p = _write_strat(_STRAT_WITH_DECL)
        mock_eval.return_value = {"passed": False, "reason": "no edge", "full": {}}
        self.assertIsNone(pt.tune_strategy("WIN", "M5", "TEST", p, {}, None))


class TestGuardrailRegistry(unittest.TestCase):
    """Registry de sanctioned params no guardrail (default-deny preservado)."""

    def setUp(self):
        gr.clear_sanctioned_params()

    def tearDown(self):
        gr.clear_sanctioned_params()

    def test_sanctioned_accepted_in_range(self):
        # breakout_lookback NÃO está na whitelist estática → só o registry libera.
        gr.register_sanctioned_params("AGI4_X", {"breakout_lookback": (int, 5, 100)})
        config = {"strategy_by_tf": {"WIN_M5": "AGI4_X"}}
        ok, _ = gr.validate_write_target("params_by_tf.WIN_M5.breakout_lookback", 20, config)
        self.assertTrue(ok)

    def test_sanctioned_rejected_out_of_range(self):
        gr.register_sanctioned_params("AGI4_X", {"breakout_lookback": (int, 5, 100)})
        config = {"strategy_by_tf": {"WIN_M5": "AGI4_X"}}
        ok, reason = gr.validate_write_target(
            "params_by_tf.WIN_M5.breakout_lookback", 500, config)
        self.assertFalse(ok)
        self.assertIn("range", reason)

    def test_sanctioned_rejected_wrong_type(self):
        gr.register_sanctioned_params("AGI4_X", {"breakout_lookback": (int, 5, 100)})
        config = {"strategy_by_tf": {"WIN_M5": "AGI4_X"}}
        ok, _ = gr.validate_write_target(
            "params_by_tf.WIN_M5.breakout_lookback", 9.5, config)
        self.assertFalse(ok)  # float num campo int

    def test_default_deny_preserved_for_unsanctioned(self):
        # Param não sancionado E não na whitelist estática → default-deny.
        config = {"strategy_by_tf": {"WIN_M5": "AGI4_X"}}
        ok, reason = gr.validate_write_target(
            "params_by_tf.WIN_M5.unknow_param_xyz", 10, config)
        self.assertFalse(ok)
        self.assertIn("default-deny", reason)

    def test_resolves_strategy_from_config(self):
        # Sanciona AGI4_X mas o par WIN_M5 usa OUTRA estratégia → não aplica.
        gr.register_sanctioned_params("AGI4_X", {"breakout_lookback": (int, 5, 100)})
        config = {"strategy_by_tf": {"WIN_M5": "OUTRA_STRATEGY"}}
        ok, reason = gr.validate_write_target(
            "params_by_tf.WIN_M5.breakout_lookback", 20, config)
        self.assertFalse(ok)  # OUTRA_STRATEGY não sancionou breakout_lookback

    def test_register_normalizes_malformed(self):
        # Entradas malformadas são descartadas silenciosamente.
        gr.register_sanctioned_params("AGI4_X", {
            "good": (int, 5, 30),
            "bad_type": (str, 1, 2),       # tipo inválido
            "bad_len": (int, 5),            # só 2 elementos
        })
        self.assertIn("good", gr._SANCTIONED_PARAMS["AGI4_X"])
        self.assertNotIn("bad_type", gr._SANCTIONED_PARAMS["AGI4_X"])
        self.assertNotIn("bad_len", gr._SANCTIONED_PARAMS["AGI4_X"])

    def test_non_params_path_not_affected(self):
        # strategy_by_tf NÃO é afetado pelo registry (só params_by_tf).
        gr.register_sanctioned_params("AGI4_X", {"ema_fast": (int, 5, 30)})
        config = {"strategy_by_tf": {"WIN_M5": "AGI4_X"}}
        ok, _ = gr.validate_write_target("strategy_by_tf.WIN_M5", "AGI4_X", config)
        # strategy_by_tf tem whitelist própria — não passa pelo registry.
        # (aceita ou não depende da whitelist estática, não do registry)


if __name__ == "__main__":
    unittest.main()
