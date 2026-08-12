"""test_agi_alerts.py — Testes do relatório Telegram enriquecido do AGI v4.

Cobre _build_telegram_message (stage6_report) e os accumuladores do Stage 5
(all_applied_changes / all_rejected_changes) — sem depender de MT5/Wine/DB/Telegram.

Wave AGI-alerts (Bruno 12/08/2026): ver optimization/agi_v4/stage6_report.py.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from optimization.agi_v4 import stage6_report as s6  # noqa: E402


def _base_ctx(**overrides):
    """ctx mínimo para _build_telegram_message. Defaults neutros."""
    ctx = {
        "converged": False,
        "stagnated": False,
        "deadline_hit": False,
        "current_iteration": 0,
        "duration_s": 0,
        "dry_run": True,
        "performance": {},
        "failing_pairs": [],
        "reactivated": [],
        "deactivated": [],
        "generated_strategies": [],
        "profit_optimizations": [],
        "all_applied_changes": [],
        "all_rejected_changes": [],
        "applied_changes": [],
        "rejected_changes": [],
    }
    ctx.update(overrides)
    return ctx


class TestTerminationHeader(unittest.TestCase):
    """Condição de término (1c): convergiu/estagnou/deadline + duração."""

    def setUp(self):
        # Hermeticidade: shadow lê o DB real — silencia para focar no header.
        patcher = patch.object(s6, "_shadow_today_summary", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_converged(self):
        msg = s6._build_telegram_message(_base_ctx(converged=True))
        self.assertIn("CONVERGIU", msg)
        self.assertIn("✅", msg)

    def test_stagnated_shows_iterations(self):
        msg = s6._build_telegram_message(
            _base_ctx(stagnated=True, current_iteration=7))
        self.assertIn("ESTAGNOU", msg)
        self.assertIn("7 it", msg)
        self.assertIn("🔄", msg)

    def test_deadline_shows_iterations(self):
        msg = s6._build_telegram_message(
            _base_ctx(deadline_hit=True, current_iteration=12))
        self.assertIn("DEADLINE", msg)
        self.assertIn("12 it", msg)
        self.assertIn("⏰", msg)

    def test_duration_minutes(self):
        msg = s6._build_telegram_message(_base_ctx(duration_s=720))  # 12min
        self.assertIn("12min", msg)

    def test_duration_hours(self):
        msg = s6._build_telegram_message(_base_ctx(duration_s=7200))  # 2h
        self.assertIn("2h00min", msg)

    def test_mode_dry_run_vs_aplicado(self):
        self.assertIn("DRY-RUN", s6._build_telegram_message(_base_ctx(dry_run=True)))
        self.assertIn("APLICADO", s6._build_telegram_message(_base_ctx(dry_run=False)))


class TestGeneratedStrategies(unittest.TestCase):
    """Estratégias geradas (Stage 4) + cross-pair salvages (2b)."""

    def setUp(self):
        patcher = patch.object(s6, "_shadow_today_summary", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_summary_counts_and_gate_breakdown(self):
        generated = [
            {"name": "AGI4_WIN_1", "status": "approved_pending",
             "backtest_gate": "passed", "backtest": {"total_pnl": 100}},
            {"name": "AGI4_BIT_2", "status": "rejected",
             "gate": "ast_gate", "reason": "sem STRATEGY_NAME"},
            {"name": "AGI4_WDO_3", "status": "rejected",
             "gate": "runtime_smoke", "reason": "TypeError"},
        ]
        msg = s6._build_telegram_message(_base_ctx(generated_strategies=generated))
        self.assertIn("3 gerada(s)", msg)
        self.assertIn("1 aprov.", msg)
        self.assertIn("2 rej", msg)
        self.assertIn("ast_gate:1", msg)
        self.assertIn("runtime_smoke:1", msg)

    def test_cross_pair_salvage_line(self):
        generated = [
            {"name": "AGI4_WIN_121815", "status": "approved_pending",
             "backtest_gate": "passed", "winning_pair": "WSP_M5",
             "backtest": {"total_pnl": 345}},
        ]
        msg = s6._build_telegram_message(_base_ctx(generated_strategies=generated))
        self.assertIn("↩️", msg)
        self.assertIn("AGI4_WIN_121815", msg)
        self.assertIn("WSP_M5", msg)

    def test_empty_generated_omits_section(self):
        msg = s6._build_telegram_message(_base_ctx(generated_strategies=[]))
        self.assertNotIn("🧪", msg)


class TestAppliedAndRejected(unittest.TestCase):
    """Mudanças aplicadas (métricas completas + dedup) e rejeitadas (accumulator)."""

    def setUp(self):
        patcher = patch.object(s6, "_shadow_today_summary", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_applied_full_metrics_and_baseline(self):
        applied = [{
            "change": {
                "pair": "WIN_M5", "strategy": "AGI4_WIN_X",
                "backtest": {"pf": 1.82, "wr": 62.0, "n_trades": 48,
                             "max_dd": -120},
                "baseline_simulated_pnl": -80,
            },
            "candidate": {},
        }]
        msg = s6._build_telegram_message(_base_ctx(all_applied_changes=applied))
        self.assertIn("WIN_M5", msg)
        self.assertIn("AGI4_WIN_X", msg)
        self.assertIn("PF 1.82", msg)
        self.assertIn("WR 62%", msg)
        self.assertIn("48t", msg)
        self.assertIn("vs base R$-80", msg)

    def test_dedup_by_pair_last_wins(self):
        # Mesmo par aparece 2x (re-aplicado no loop). Só a última vence.
        applied = [
            {"change": {"pair": "WIN_M5", "strategy": "VELHA",
                        "backtest": {}, "baseline_simulated_pnl": 0}},
            {"change": {"pair": "WIN_M5", "strategy": "NOVA",
                        "backtest": {}, "baseline_simulated_pnl": 0}},
        ]
        msg = s6._build_telegram_message(_base_ctx(all_applied_changes=applied))
        self.assertIn("1 par(es)", msg)  # dedup → 1, não 2
        self.assertIn("NOVA", msg)
        self.assertNotIn("VELHA", msg)

    def test_rejected_accumulator_includes_guardrail(self):
        rejected = [
            {"gate": "better_baseline_exists"},
            {"gate": "guardrail_reject"},
            {"gate": "guardrail_reject"},
            {"gate": "must_be_profitable"},
        ]
        msg = s6._build_telegram_message(_base_ctx(all_rejected_changes=rejected))
        self.assertIn("4 rejeitada(s)", msg)
        self.assertIn("guardrail_reject:2", msg)
        self.assertIn("better_baseline_exists:1", msg)

    def test_profit_optimizations_line(self):
        msg = s6._build_telegram_message(
            _base_ctx(profit_optimizations=[{"change": {}}, {"change": {}}]))
        self.assertIn("2 otimização(ões)", msg)


class TestInvalidDayBanner(unittest.TestCase):
    """Banner de dia inválido (2a)."""

    def setUp(self):
        patcher = patch.object(s6, "_shadow_today_summary", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_flag_present_shows_banner(self):
        # True apenas para o path da flag; demais paths devolvem False.
        with patch("os.path.exists",
                   side_effect=lambda p: p == "/tmp/vt_invalid_day.flag"):
            msg = s6._build_telegram_message(_base_ctx())
        self.assertIn("🚫 Dia inválido", msg)

    def test_flag_absent_omits_banner(self):
        with patch("os.path.exists", return_value=False):
            msg = s6._build_telegram_message(_base_ctx())
        self.assertNotIn("Dia inválido", msg)


class TestSendBrief(unittest.TestCase):
    """send_brief (lifecycle) é fail-safe e nunca levanta."""

    def test_returns_true_when_hermes_ok(self):
        with patch("core.vt_hermes_helper.hermes_send", return_value=True):
            self.assertTrue(s6.send_brief("msg", retries=0))

    def test_returns_false_when_hermes_fails(self):
        with patch("core.vt_hermes_helper.hermes_send", return_value=False):
            self.assertFalse(s6.send_brief("msg", retries=0))

    def test_never_raises_on_exception(self):
        with patch("core.vt_hermes_helper.hermes_send",
                   side_effect=RuntimeError("boom")):
            self.assertFalse(s6.send_brief("msg", retries=0))


if __name__ == "__main__":
    unittest.main()
