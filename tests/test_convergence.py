"""Test #2 — Convergence gate compara DELTA, não valor absoluto.

Problema: collect_performance() lê 7 dias de trades passados. As mudanças
de parâmetros só afetam trades FUTUROS. Logo, re-chamar collect_performance()
na iteração 2+ retorna os mesmos 144 trades → o gate nunca muda.

Solução: medir DELTA entre snapshot inicial e snapshot atual.

API esperada:
    snapshot_performance(perf: dict) -> dict
    check_convergence(current_perf, baseline_snapshot, mode="absolute"|"delta")
        -> (converged: bool, failing_pairs: list[str])

Em modo "delta":
    - Par PnL-positivo: OK
    - Par PnL-negativo: exige melhoria >= 30% vs baseline
    - Par novo (sem baseline): tratado como recém-criado, OK se PnL>0
    - Par que PIOROU >20%: bloqueia convergência mesmo se outros melhoraram

Em modo "absolute" (legado): PnL > 0 em todos.
"""
import sys
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "optimization"))


class TestSnapshotPerformance(unittest.TestCase):
    def test_snapshot_performance_creates_immutable_baseline(self):
        """snapshot_performance() deve retornar dict com PnL por par."""
        from agi_tuning_17h import snapshot_performance

        fake_perf = {
            "by_symbol_tf": {
                "WIN_M5": {"n_trades": 10, "total_pnl": -240.0, "win_rate": 30.0, "strategy": "BOLLINGER"},
                "BIT_M30": {"n_trades": 5, "total_pnl": 500.0, "win_rate": 70.0, "strategy": "RSI_REVERSION"},
            },
        }
        snap = snapshot_performance(fake_perf)
        self.assertIn("WIN_M5", snap)
        self.assertIn("BIT_M30", snap)
        self.assertEqual(snap["WIN_M5"]["pnl"], -240.0)
        self.assertEqual(snap["BIT_M30"]["pnl"], 500.0)


class TestCheckConvergenceAbsolute(unittest.TestCase):
    def test_absolute_mode_passes_when_all_positive(self):
        from agi_tuning_17h import check_convergence
        current = {
            "by_symbol_tf": {
                "WIN_M5": {"n_trades": 10, "total_pnl": 100.0},
                "BIT_M30": {"n_trades": 5, "total_pnl": 50.0},
            }
        }
        snapshot = {"WIN_M5": {"pnl": -200.0}, "BIT_M30": {"pnl": 30.0}}
        converged, failing = check_convergence(current, snapshot, mode="absolute")
        self.assertTrue(converged)
        self.assertEqual(failing, [])

    def test_absolute_mode_fails_with_any_negative(self):
        from agi_tuning_17h import check_convergence
        current = {
            "by_symbol_tf": {
                "WIN_M5": {"n_trades": 10, "total_pnl": 100.0},
                "WDO_M5": {"n_trades": 8, "total_pnl": -50.0},
            }
        }
        snapshot = {"WIN_M5": {"pnl": -200.0}, "WDO_M5": {"pnl": -500.0}}
        converged, failing = check_convergence(current, snapshot, mode="absolute")
        self.assertFalse(converged)
        self.assertIn("WDO_M5", failing)
        self.assertNotIn("WIN_M5", failing)


class TestCheckConvergenceDelta(unittest.TestCase):
    def test_delta_mode_passes_on_strong_improvement(self):
        """Par negativo melhorando >=30% e par positivo conta como convergido."""
        from agi_tuning_17h import check_convergence
        # WIN_M5: -200 → -50 (75% melhoria, passa threshold de 30%)
        # BIT_M30: 100 → 200 (já positivo, mantém)
        current = {
            "by_symbol_tf": {
                "WIN_M5": {"n_trades": 10, "total_pnl": -50.0},
                "BIT_M30": {"n_trades": 5, "total_pnl": 200.0},
            }
        }
        snapshot = {"WIN_M5": {"pnl": -200.0}, "BIT_M30": {"pnl": 100.0}}
        converged, failing = check_convergence(current, snapshot, mode="delta")
        self.assertTrue(converged, f"Esperava convergência por delta, got failing={failing}")

    def test_delta_mode_rejects_stagnation(self):
        """Par negativo melhorando <30% bloqueia convergência."""
        from agi_tuning_17h import check_convergence
        # WIN_M5: -200 → -180 (10% melhoria, não chega em 30%)
        # BIT_M30: 100 → 90 (regressão leve mas ainda positivo)
        current = {
            "by_symbol_tf": {
                "WIN_M5": {"n_trades": 10, "total_pnl": -180.0},
                "BIT_M30": {"n_trades": 5, "total_pnl": 90.0},
            }
        }
        snapshot = {"WIN_M5": {"pnl": -200.0}, "BIT_M30": {"pnl": 100.0}}
        converged, failing = check_convergence(current, snapshot, mode="delta")
        self.assertFalse(converged)
        self.assertIn("WIN_M5", failing)

    def test_delta_mode_rejects_regression(self):
        """Par que PIOROU significativamente (>20%) bloqueia convergência."""
        from agi_tuning_17h import check_convergence
        # WIN_M5 melhorou, mas WDO_M5 piorou muito (-500 → -800, +60%)
        current = {
            "by_symbol_tf": {
                "WIN_M5": {"n_trades": 10, "total_pnl": -50.0},
                "WDO_M5": {"n_trades": 8, "total_pnl": -800.0},
            }
        }
        snapshot = {"WIN_M5": {"pnl": -200.0}, "WDO_M5": {"pnl": -500.0}}
        converged, failing = check_convergence(current, snapshot, mode="delta")
        self.assertFalse(converged)
        self.assertIn("WDO_M5", failing)

    def test_delta_mode_handles_new_pair(self):
        """Par sem baseline (novo) conta como OK se PnL>0."""
        from agi_tuning_17h import check_convergence
        current = {
            "by_symbol_tf": {
                "WIN_M5": {"n_trades": 10, "total_pnl": -50.0},
                "NEW_TF": {"n_trades": 3, "total_pnl": 20.0},
            }
        }
        snapshot = {"WIN_M5": {"pnl": -200.0}}  # NEW_TF sem baseline
        converged, failing = check_convergence(current, snapshot, mode="delta")
        self.assertTrue(converged, f"Esperava convergência, got failing={failing}")


if __name__ == "__main__":
    unittest.main()
