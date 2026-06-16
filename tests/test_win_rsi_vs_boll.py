"""Test #3b — backtest BOLLINGER vs RSI_REVERSION pra WIN M5/M15.

Hipótese: RSI_REVERSION (que WIN M30 já usa com lucro) bate BOLLINGER
em WIN M5/M15. Validamos com mini-backtest que usa os CSVs em data/.

Para evitar duplicar a lógica dos plugins, usamos a função
`run_comparative_backtest()` que importa os check_entry() reais
de strategies/bollinger.py e strategies/rsi_reversion.py.
"""
import sys
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))


class TestComparativeBacktest(unittest.TestCase):
    """WIN M5/M15 — RSI_REVERSION deve ser >= BOLLINGER em PnL."""

    DATA_DIR = PROJECT_DIR / "data"

    def _run(self, tf, rsi_params, boll_params):
        from backtest.mini_compare import run_comparative_backtest
        return run_comparative_backtest(
            csv_path=str(self.DATA_DIR / f"WIN_{tf}.csv"),
            tf=tf,
            rsi_params=rsi_params,
            boll_params=boll_params,
        )

    def test_win_m5_rsi_vs_bollinger(self):
        """Em WIN M5, RSI_REVERSION PnL >= BOLLINGER PnL (ou próximo)."""
        result = self._run("M5",
            rsi_params={"rsi_period": 14, "rsi_overbought": 70, "rsi_oversold": 30,
                        "sl_atr_mult": 0.6, "trail_activate": 1.0, "trail_distance": 0.4,
                        "cooldown_seconds": 180, "max_daily_trades": 8},
            boll_params={"bb_period": 20, "bb_std": 2.0, "rsi_overbought": 70, "rsi_oversold": 30,
                         "sl_atr_mult": 0.6, "trail_activate": 1.0, "trail_distance": 0.4,
                         "cooldown_seconds": 180, "max_daily_trades": 8},
        )
        self.assertIn("rsi", result)
        self.assertIn("bollinger", result)
        print(f"\n[WIN M5] BOLLINGER: PnL {result['bollinger']['pnl']:+,.0f} | "
              f"WR {result['bollinger']['wr']:.1f}% | n={result['bollinger']['n']}")
        print(f"[WIN M5] RSI_REV  : PnL {result['rsi']['pnl']:+,.0f} | "
              f"WR {result['rsi']['wr']:.1f}% | n={result['rsi']['n']}")
        self.assertGreaterEqual(result["rsi"]["n"], 3, "RSI gerou poucos trades (<3) — backtest inválido")
        self.assertGreaterEqual(result["bollinger"]["n"], 3, "BOLLINGER gerou poucos trades (<3)")

    def test_win_m15_rsi_vs_bollinger(self):
        """Em WIN M15, RSI_REVERSION PnL >= BOLLINGER PnL (ou próximo)."""
        result = self._run("M15",
            rsi_params={"rsi_period": 14, "rsi_overbought": 70, "rsi_oversold": 30,
                        "sl_atr_mult": 0.6, "trail_activate": 1.0, "trail_distance": 0.4,
                        "cooldown_seconds": 180, "max_daily_trades": 8},
            boll_params={"bb_period": 20, "bb_std": 2.0, "rsi_overbought": 70, "rsi_oversold": 30,
                         "sl_atr_mult": 0.6, "trail_activate": 1.0, "trail_distance": 0.4,
                         "cooldown_seconds": 180, "max_daily_trades": 8},
        )
        self.assertIn("rsi", result)
        self.assertIn("bollinger", result)
        print(f"\n[WIN M15] BOLLINGER: PnL {result['bollinger']['pnl']:+,.0f} | "
              f"WR {result['bollinger']['wr']:.1f}% | n={result['bollinger']['n']}")
        print(f"[WIN M15] RSI_REV  : PnL {result['rsi']['pnl']:+,.0f} | "
              f"WR {result['rsi']['wr']:.1f}% | n={result['rsi']['n']}")
        self.assertGreaterEqual(result["rsi"]["n"], 3)
        self.assertGreaterEqual(result["bollinger"]["n"], 3)


if __name__ == "__main__":
    unittest.main()
