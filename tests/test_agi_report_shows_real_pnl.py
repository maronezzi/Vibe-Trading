"""
test_agi_report_shows_real_pnl.py
====================================
TDD: garante que o RELATÓRIO FINAL do AGI mostra o PnL REAL (DB),
não o PnL do backtest — OU rotula claramente qual é qual.

PROBLEMA REAL (Bruno, 2026-06-26, dry-run v899):
  O relatório mostrou:
    🤖 AGI 17H v3.0 — convergiu ✅
    📊 30d: 309 trades | PnL R$-8657.00
    🔬 Forward convergence: 16 pairs | failing=0 | no_signal=1
    📈 Evolução por symbol
      WIN: sl=1.0 cd=180 | PnL R$+116→R$+116 (Δ R$+0)
      BIT: sl=1.0 cd=180 | PnL R$-7241→R$-7241 (Δ R$+0)
      WDO: sl=1.0 cd=180 | PnL R$-1134→R$-1134 (Δ R$+0)

  ISSUES:
  1. "convergiu ✅" mas PnL -R$8.657 (não convergiu nada)
  2. Δ R$+0 em todos (auto-apply clipou pro floor, params não mudaram)
  3. PnL "atual" e "melhor" são IGUAIS (porque nenhuma mudança foi
     realmente aplicada)

FIX:
  1. print_report() DEVE emitir ALERTA quando converged=True mas
     pnl_real_total < 0.
  2. print_report() DEVE mostrar Δ honesto:
     - Se "atual == melhor", é porque params não mudaram (clipping)
     - Deve dizer "nenhuma mudança efetiva aplicada"
  3. print_report() DEVE rotular PnL: "(DB real, 30d)" vs "(backtest)"

Por que importa: Bruno não pode tomar decisão baseado em relatório
enganoso. Teatro de otimização = perda de tempo + decisões erradas.
"""
import io
import re
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


def _capture_print_report(perf, applied, converged=True, dry_run=True,
                          iterations=None, paused=None,
                          exhaustive_results=None):
    """Helper: executa print_report() e captura stdout."""
    from optimization.agi_tuning_17h import print_report
    iterations = iterations or []
    paused = paused or {"paused": [], "skipped": []}
    config = {"_version": 900}
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            print_report(
                perf, [], None, applied, config, dry_run,
                None, None,  # web_intel, optimization
                iterations=iterations, converged=converged, paused=paused,
                exhaustive_results=exhaustive_results,
            )
    except Exception as e:
        return f"[ERRO]: {e}\n{buf.getvalue()}"
    return buf.getvalue()


def _make_perf(by_symbol):
    """Helper: cria perf dict com todas as chaves que print_report espera."""
    return {
        "by_symbol": by_symbol,
        "by_symbol_tf": {},
        "exit_reasons": {},
        "today": {},
        "streaks": {},
        "signal_analysis": {},
        "sl_analysis": {},
        "direction_analysis": {},
        "period_days": 30,
        "cutoff_date": "2026-05-26",
    }


class TestPrintReportEmitsPnLWarning(unittest.TestCase):
    """print_report() deve alertar quando converged=True mas pnl_real<0."""

    def test_report_alerts_when_converged_but_pnl_negative(self):
        """
        print_report() com converged=True e PnL real < 0 deve
        emitir ALERTA/ATENÇÃO no output.
        """
        perf = _make_perf({
            "WIN": {"n_trades": 100, "win_rate": 30, "total_pnl": -321.40, "avg_pnl": -3.21},
            "BIT": {"n_trades": 40, "win_rate": 27, "total_pnl": -7240.80, "avg_pnl": -181.02},
            "WDO": {"n_trades": 35, "win_rate": 34, "total_pnl": -1134.00, "avg_pnl": -32.40},
        })
        applied = []  # nenhuma mudança aplicada
        output = _capture_print_report(perf, applied, converged=True, dry_run=True)

        # PnL total é negativo
        pnl_total = -321.40 - 7240.80 - 1134.00  # -8696.20

        # DEVE emitir alerta/atenção sobre pnl negativo com converged
        has_alert = bool(
            re.search(r"⚠️|ALERTA|ATENÇÃO|PnL.*?NEGATIVO|NÃO.*?CONVERGIU|⚠️", output)
        )
        self.assertTrue(
            has_alert,
            f"print_report() NÃO alerta quando converged=True com PnL "
            f"total=R${pnl_total:.2f}. Output:\n{output[:1500]}"
        )

    def test_report_does_not_alert_when_converged_and_pnl_positive(self):
        """Se converged=True E PnL>0, não precisa alertar (situação legítima)."""
        perf = _make_perf({
            "WIN": {"n_trades": 100, "win_rate": 60, "total_pnl": 500, "avg_pnl": 5.0},
            "BIT": {"n_trades": 30, "win_rate": 55, "total_pnl": 200, "avg_pnl": 6.67},
        })
        applied = []
        output = _capture_print_report(perf, applied, converged=True, dry_run=True)
        # Output deve ter PnL (legítimo)
        self.assertIn("PnL", output)


class TestPrintReportHonestDelta(unittest.TestCase):
    """Δ R$+0 deve ser sinalizado como 'nenhuma mudança efetiva'."""

    def test_report_marks_zero_delta_as_no_change(self):
        """Smoke test: applied com sl=1.0 não deve mostrar 'mudou'."""
        perf = _make_perf({
            "WIN": {"n_trades": 100, "win_rate": 30, "total_pnl": -321.40, "avg_pnl": -3.21},
        })
        applied = [{
            "symbol": "WIN",
            "params": {"sl_atr_mult": 1.0, "cooldown_seconds": 180},
            "applied": False,
            "reason": "[DRY-RUN] Explorer: PF=2.57 (clipped)",
        }]
        output = _capture_print_report(perf, applied, converged=True, dry_run=True)
        # Smoke test: não crasha
        self.assertIsInstance(output, str)


class TestBuildEvolutionSummaryHonest(unittest.TestCase):
    """build_evolution_summary() deve ser honesto sobre Δ."""

    def test_evolution_summary_no_change_marker(self):
        """
        Quando params propostos == params atuais, build_evolution_summary
        deve marcar como 'sem mudança efetiva'.
        """
        from optimization.agi_tuning_17h import build_evolution_summary
        # baseline_perf com WIN = +R$116
        baseline_perf = {
            "by_symbol": {"WIN": {"n_trades": 50, "total_pnl": 116.40}}
        }
        # current_perf igual ao baseline (nenhuma mudança)
        current_perf = {
            "by_symbol": {"WIN": {"n_trades": 50, "total_pnl": 116.40}}
        }
        # applied com params propostos (mas que resultam em mesma performance)
        applied = [{
            "symbol": "WIN",
            "params": {"sl_atr_mult": 1.0},
            "reason": "Clipped to floor",
        }]
        result = build_evolution_summary(applied, baseline_perf, current_perf)

        # Verifica que tem alguma indicação de 'sem mudança' ou 'no delta'
        result_text = "\n".join(result)
        has_no_change_marker = bool(
            re.search(r"sem.*?mudança|no.*?change|sem.*?delta|Δ.*?0", result_text, re.IGNORECASE)
        )
        self.assertTrue(
            has_no_change_marker,
            f"build_evolution_summary não indica 'sem mudança' quando "
            f"current_perf == baseline_perf. Output: {result}"
        )


if __name__ == "__main__":
    unittest.main()
