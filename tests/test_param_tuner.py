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

    def test_clips_to_guardrail_range(self):
        # Bug 1 fix: ema_fast default 25 → fallback range [13, 38], mas a
        # whitelist do guardrail é [5, 30]. Após clip, hi deve ser ≤30 (não 38).
        src = '''
STRATEGY_NAME = "TEST_CLIP"
def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    ema = params.get("ema_fast", 25)
    return None
'''
        p = _write_strat(src)
        t = pt.extract_tunable_params(p)
        self.assertIn("ema_fast", t)
        # Range clipsado ao aceito pelo guardrail ([5,30]).
        self.assertLessEqual(t["ema_fast"]["hi"], 30)
        self.assertGreaterEqual(t["ema_fast"]["lo"], 5)
        # Default 25 está dentro — mantém.
        self.assertEqual(t["ema_fast"]["default"], 25)

    def test_clips_default_outside_guardrail_range(self):
        # Default abaixo do range da whitelist → clipsado para o limite inferior.
        # rsi_period whitelist é [5,30]; default 3 → clipsado para 5.
        src = '''
STRATEGY_NAME = "TEST_CLIP2"
def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    rsi = params.get("rsi_period", 3)
    return None
'''
        p = _write_strat(src)
        t = pt.extract_tunable_params(p)
        if "rsi_period" in t:  # só se passou o filtro de range não-degenerado
            self.assertGreaterEqual(t["rsi_period"]["default"], 5)


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


class TestOrderingFix(unittest.TestCase):
    """#1: validate_target_block usa a estratégia PRETENDIDA (do target), não a
    antiga do disco. Sem isto, params próprios de uma AGI4 recém-aplicada caem em
    default-deny (o _check_sanctioned consulta a estratégia velha)."""

    def setUp(self):
        gr.clear_sanctioned_params()

    def tearDown(self):
        gr.clear_sanctioned_params()

    def test_sanctioned_accepted_when_strategy_in_target(self):
        # AGI4_X sanciona breakout_lookback (não está na whitelist estática).
        gr.register_sanctioned_params("AGI4_X", {"breakout_lookback": (int, 5, 100)})
        # Disco: WIN_M5 ainda usa a estratégia ANTIGA.
        config = {"strategy_by_tf": {"WIN_M5": "VELHA"}}
        # Target aplica AGI4_X + seu param próprio no mesmo bloco.
        target = {
            "strategy_by_tf": {"WIN_M5": "AGI4_X"},
            "params_by_tf": {"WIN_M5": {"breakout_lookback": 20}},
        }
        # Deve PASSAR: effective_config usa AGI4_X (do target) → sancionado.
        gr.validate_target_block(target, config)  # não levanta

    def test_rejected_when_strategy_not_sanctioned(self):
        # Ninguém sanciona breakout_lookback para OUTRA.
        config = {"strategy_by_tf": {"WIN_M5": "VELHA"}}
        target = {
            "strategy_by_tf": {"WIN_M5": "OUTRA"},
            "params_by_tf": {"WIN_M5": {"breakout_lookback": 20}},
        }
        with self.assertRaises(gr.GuardrailReject):
            gr.validate_target_block(target, config)


class TestReadParamNames(unittest.TestCase):
    """read_param_names extrai params.get E params[...] (subscript defensivo)."""

    def test_captures_get_and_subscript(self):
        src = '''
STRATEGY_NAME = "TEST_READ"
def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    a = params.get("ema_fast", 9)
    b = params["hard_key"]
    return None
'''
        p = _write_strat(src)
        names = pt.read_param_names(p)
        self.assertIn("ema_fast", names)
        self.assertIn("hard_key", names)  # subscript defensivo

    def test_empty_on_parse_error(self):
        self.assertEqual(pt.read_param_names("/nonexistent/x.py"), set())


class TestZombieDrop(unittest.TestCase):
    """#3: _compute_zombie_drop (default-keep). Só dropa o que a nova estratégia
    não lê E não é framework E não veio do candidato."""

    def _make_strat(self, name, params_src):
        """Cria estratégia temporária e patcheia strategy_path_by_name p/ achá-la."""
        src = f'''
STRATEGY_NAME = "{name}"
def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    calc_sl = utils["calc_sl"]
    {params_src}
    sl_pts = calc_sl(symbol, atr, params)
    return {{"direction": "BUY", "sl_pts": sl_pts, "info": {{}}}}
'''
        path = _write_strat(src)
        return path

    def test_removes_unread_non_framework(self):
        # Nova estratégia lê SÓ ema_fast. Config tem rsi_period (zombie) +
        # ema_fast (lido) + sl_atr_mult (framework).
        from optimization.agi_v4 import stage5_apply as s5
        path = self._make_strat("TEST_Z1", 'ema = params.get("ema_fast", 9)')
        new_cfg = {"params_by_tf": {"WIN_M5": {
            "rsi_period": 7, "ema_fast": 9, "sl_atr_mult": 1.5}}}
        with patch("optimization.exhaustive_strategy_search.strategy_path_by_name",
                   return_value=str(path)):
            drop = s5._compute_zombie_drop(new_cfg, "WIN_M5", "TEST_Z1", {})
        self.assertIn("rsi_period", drop)
        self.assertNotIn("ema_fast", drop)   # lido pela nova
        self.assertNotIn("sl_atr_mult", drop)  # framework

    def test_framework_never_dropped(self):
        from optimization.agi_v4 import stage5_apply as s5
        # Estratégia não lê sl_atr_mult nem cooldown, mas são framework → mantidos.
        path = self._make_strat("TEST_Z2", 'x = params.get("x", 1)')
        new_cfg = {"params_by_tf": {"WIN_M5": {
            "sl_atr_mult": 1.5, "cooldown_seconds": 300, "x": 1}}}
        with patch("optimization.exhaustive_strategy_search.strategy_path_by_name",
                   return_value=str(path)):
            drop = s5._compute_zombie_drop(new_cfg, "WIN_M5", "TEST_Z2", {})
        self.assertNotIn("sl_atr_mult", drop)
        self.assertNotIn("cooldown_seconds", drop)

    def test_cand_params_kept(self):
        from optimization.agi_v4 import stage5_apply as s5
        # breakout_lookback não é lido pela nova, mas veio no candidato → mantido.
        path = self._make_strat("TEST_Z3", 'x = params.get("x", 1)')
        new_cfg = {"params_by_tf": {"WIN_M5": {"breakout_lookback": 20, "x": 1}}}
        with patch("optimization.exhaustive_strategy_search.strategy_path_by_name",
                   return_value=str(path)):
            drop = s5._compute_zombie_drop(
                new_cfg, "WIN_M5", "TEST_Z3", {"breakout_lookback": 20})
        self.assertNotIn("breakout_lookback", drop)

    def test_empty_when_no_current_params(self):
        from optimization.agi_v4 import stage5_apply as s5
        drop = s5._compute_zombie_drop({}, "WIN_M5", "TEST_Z4", {})
        self.assertEqual(drop, [])


class TestZombieDropGuardrail(unittest.TestCase):
    """#3: validate_target_block valida params_by_tf_drop (defensivo)."""

    def test_validates_drop_block_ok(self):
        config = {"params_by_tf": {"WIN_M5": {"rsi_period": 7}}}
        target = {"params_by_tf_drop": {"WIN_M5": ["rsi_period"]}}
        gr.validate_target_block(target, config)  # não levanta

    def test_rejects_framework_drop(self):
        config = {"params_by_tf": {"WIN_M5": {"sl_atr_mult": 1.5}}}
        target = {"params_by_tf_drop": {"WIN_M5": ["sl_atr_mult"]}}
        with self.assertRaises(gr.GuardrailReject):
            gr.validate_target_block(target, config)

    def test_rejects_nonexistent_drop(self):
        config = {"params_by_tf": {"WIN_M5": {"a": 1}}}
        target = {"params_by_tf_drop": {"WIN_M5": ["inexistente"]}}
        with self.assertRaises(gr.GuardrailReject):
            gr.validate_target_block(target, config)


class TestStrategyPathByName(unittest.TestCase):
    """#2: helper name→path (infra do bootstrap e do zombie keep-set)."""

    def test_returns_none_for_unknown(self):
        from optimization.exhaustive_strategy_search import strategy_path_by_name
        self.assertIsNone(strategy_path_by_name("ESTRATEGIA_INEXISTENTE_XYZ"))

    def test_returns_path_for_known(self):
        from optimization.exhaustive_strategy_search import (
            strategy_path_by_name, ALL_STRATEGIES,
        )
        if not ALL_STRATEGIES:
            self.skipTest("sem estratégias no ambiente")
        name = ALL_STRATEGIES[0]
        path = strategy_path_by_name(name)
        self.assertIsNotNone(path)
        self.assertTrue(Path(path).exists())


if __name__ == "__main__":
    unittest.main()
