"""
test_agi_progress_only.py
============================
TDD: garante que o relatório do AGI mostra APENAS progresso (o que
melhorou) + expectativa forward-looking (PnL/dia, 30d).

FILOSOFIA (Bruno, 2026-06-26):
  "O AGI deve só mostrar o que melhorou e a expectativa dos novos
  valores, por dia e nos próximos 30 dias. Não precisa mostrar o
  resultado ruim do passado."

  Bruno não quer auditoria contábil do AGI. Quer:
  1. O que mudou (delta positivo, novos params/estratégias/pares)
  2. Expectativa por dia (forward-looking: avg_pnl/trade × n_trades_dia)
  3. Projeção 30d (P&L estimado nos próximos 22 pregões)

  Hoje o relatório faz o OPOSTO:
  - Lista PnL real do passado (negativo, deprimente)
  - Lista "Δ R$+0" (sem mudança efetiva)
  - Lista EXIT REASONS com SL_SERVIDOR 276x (ruim)
  - Lista STREAKS de loss

FIX: reescrever print_report + build_evolution_summary:
  - Remover seção "PERFORMANCE (30 dias)" com PnL histórico
  - Adicionar seção "📈 O que mudou" (só deltas positivos)
  - Adicionar seção "🎯 Expectativa diária" (PnL/dia projetado)
  - Adicionar seção "📅 Projeção 30d" (PnL estimado)
  - Manter "Nenhuma mudança" se não houve nada positivo

Por que importa: Bruno precisa de SINAL DE PROGRESSO, não
auditoria. O AGI é ferramenta de decisão, não de prestação de
contas do passado.
"""
import io
import re
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


def _make_perf(by_symbol):
    """Helper: cria perf dict com avg_pnl."""
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


def _capture_print_report(perf, applied, converged=False, dry_run=True,
                          iterations=None, paused=None):
    from optimization.agi_tuning_17h import print_report
    iterations = iterations or []
    paused = paused or {"paused": [], "skipped": []}
    config = {"_version": 900}
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            print_report(
                perf, [], None, applied, config, dry_run,
                None, None,
                iterations=iterations, converged=converged, paused=paused,
            )
    except Exception as e:
        return f"[ERRO]: {e}\n{buf.getvalue()}"
    return buf.getvalue()


class TestReportShowsOnlyProgress(unittest.TestCase):
    """Relatório deve mostrar APENAS progresso + expectativa, não passado."""

    def test_report_no_historical_pnl_section(self):
        """
        print_report() NÃO deve ter seção "PERFORMANCE (30 dias):" com
        PnL histórico de cada symbol. Bruno não quer ver o passado ruim.
        """
        from optimization.agi_tuning_17h import print_report
        import inspect
        src = inspect.getsource(print_report)

        # Procurar pela string 'PERFORMANCE' (que vem do print)
        has_perf_section = bool(
            re.search(r"PERFORMANCE\s*\(.*?dias.*?\):", src)
        )
        # Hoje TEM. Após fix, NÃO deve ter.
        self.assertFalse(
            has_perf_section,
            "print_report() ainda tem seção 'PERFORMANCE (N dias)' com PnL "
            "histórico. Bruno quer SINAL DE PROGRESSO, não auditoria."
        )

    def test_report_has_progress_section(self):
        """
        print_report() DEVE ter seção "O que mudou" listando apenas
        deltas positivos / melhorias.
        """
        from optimization.agi_tuning_17h import print_report
        import inspect
        src = inspect.getsource(print_report)
        has_progress = bool(
            re.search(r"O que mudou|What changed|Progresso|Improvements", src, re.IGNORECASE)
        )
        self.assertTrue(
            has_progress,
            "print_report() não tem seção 'O que mudou' / Progresso. "
            "Bruno quer saber só o que MELHOROU."
        )

    def test_report_has_daily_expectation(self):
        """
        print_report() DEVE ter seção "Expectativa diária" ou
        "Projeção diária" com PnL/dia estimado.
        """
        from optimization.agi_tuning_17h import print_report
        import inspect
        src = inspect.getsource(print_report)
        has_expectation = bool(
            re.search(r"Expectativa.*?diária|Projeção.*?diária|PnL/dia|daily.*?expectation", src, re.IGNORECASE)
        )
        self.assertTrue(
            has_expectation,
            "print_report() não tem seção 'Expectativa diária'. "
            "Bruno quer saber PnL/dia projetado."
        )

    def test_report_has_30d_projection(self):
        """
        print_report() DEVE ter seção "Projeção 30d" ou
        "Próximos 30 dias" com PnL estimado.
        """
        from optimization.agi_tuning_17h import print_report
        import inspect
        src = inspect.getsource(print_report)
        has_30d = bool(
            re.search(r"Projeção.*?30|30d|30 dias|próximos 30", src, re.IGNORECASE)
        )
        self.assertTrue(
            has_30d,
            "print_report() não tem seção 'Projeção 30d'. "
            "Bruno quer expectativa dos próximos 30 dias."
        )


class TestDailyExpectationCalculation(unittest.TestCase):
    """A função que calcula expectativa diária funciona corretamente."""

    def test_calculate_daily_expectation_helper_exists(self):
        """
        Deve existir uma função helper que calcula expectativa diária.
        """
        from optimization.agi_tuning_17h import calculate_daily_expectation
        self.assertTrue(callable(calculate_daily_expectation))

    def test_calculate_daily_expectation_winning(self):
        """
        Com 100 trades, avg_pnl=R$10, PnL/dia = R$45 (22 dias úteis).
        """
        from optimization.agi_tuning_17h import calculate_daily_expectation
        result = calculate_daily_expectation(
            total_pnl=1000.0,  # 100 trades × R$10
            n_trades=100,
            period_days=30,  # 22 pregões em 30 dias
        )
        # 22 pregões em 30 dias (regra empírica B3)
        # n_trades_dia = 100/30 = 3.33 (assume distribuição uniforme)
        # avg_pnl/trade = 10
        # PnL/dia = 3.33 × 10 = R$33.33
        # Projeção 30d = R$33.33 × 30 = R$1000 (sanity)
        self.assertIn("pnl_per_day", result)
        self.assertIn("projection_30d", result)
        self.assertGreater(result["pnl_per_day"], 0)
        # Projeção 30d ≈ PnL total (já que period_days=30)
        self.assertAlmostEqual(result["projection_30d"], 1000.0, delta=50)

    def test_calculate_daily_expectation_losing(self):
        """
        Com PnL negativo, expectativa diária e 30d são negativos.
        """
        from optimization.agi_tuning_17h import calculate_daily_expectation
        result = calculate_daily_expectation(
            total_pnl=-1000.0,
            n_trades=100,
            period_days=30,
        )
        self.assertLess(result["pnl_per_day"], 0)
        self.assertLess(result["projection_30d"], 0)


class TestBuildEvolutionOnlyShowsImprovements(unittest.TestCase):
    """build_evolution_summary: só mostra deltas positivos (ou 'sem mudança')."""

    def test_zero_delta_marked_as_no_change(self):
        """
        Quando Δ=0 (clipped, no real change), deve ser marcado como
        'sem mudança efetiva' em vez de mostrar 'Δ R$+0'.
        """
        from optimization.agi_tuning_17h import build_evolution_summary
        baseline = _make_perf({"WIN": {"total_pnl": 100, "avg_pnl": 2.0, "n_trades": 50, "win_rate": 40}})
        current = _make_perf({"WIN": {"total_pnl": 100, "avg_pnl": 2.0, "n_trades": 50, "win_rate": 40}})
        applied = [{
            "symbol": "WIN",
            "params": {"sl_atr_mult": 1.0},
            "applied": False,
            "reason": "Clipped to floor (no real change)",
        }]
        result = build_evolution_summary(applied, baseline, current)
        result_text = "\n".join(result)
        # DEVE marcar como sem mudança
        self.assertTrue(
            re.search(r"sem.*?mudança|Δ.*?0|inalterado|clipped", result_text, re.IGNORECASE),
            f"build_evolution_summary não marca Δ=0 como 'sem mudança'. "
            f"Output: {result}"
        )

    def test_only_positive_delta_shown(self):
        """
        Mudanças com Δ negativo devem ser FILTRADAS (não mostradas)
        — Bruno só quer ver o que MELHOROU.
        """
        from optimization.agi_tuning_17h import build_evolution_summary
        baseline = _make_perf({
            "WIN": {"total_pnl": 100, "avg_pnl": 2.0, "n_trades": 50, "win_rate": 40},
            "BIT": {"total_pnl": -100, "avg_pnl": -2.0, "n_trades": 50, "win_rate": 30},
        })
        # WIN melhorou (+50), BIT piorou (-50)
        current = _make_perf({
            "WIN": {"total_pnl": 150, "avg_pnl": 3.0, "n_trades": 50, "win_rate": 50},
            "BIT": {"total_pnl": -150, "avg_pnl": -3.0, "n_trades": 50, "win_rate": 20},
        })
        applied = [
            {"symbol": "WIN", "params": {"sl_atr_mult": 1.5}, "applied": True, "reason": "GOOD"},
            {"symbol": "BIT", "params": {"sl_atr_mult": 2.0}, "applied": True, "reason": "BAD"},
        ]
        result = build_evolution_summary(applied, baseline, current)
        result_text = "\n".join(result)

        # WIN DEVE aparecer
        self.assertIn("WIN", result_text, "WIN (melhorou) deve aparecer")
        # BIT NÃO deve aparecer (piorou) — Bruno não quer ver ruim
        self.assertNotIn("BIT", result_text, "BIT (piorou) NÃO deve aparecer")


if __name__ == "__main__":
    unittest.main()
