"""
test_backtest_profit_lock.py
============================
Testa que o backtest engine (backtest_v944.py) modela o profit-lock.

Cenário sintético (lucro seguido de reversão):
- Candle de entry: BUY @ ~100000, SL fixo 200pts → sl_price = 98000.
- Candle de lucro: best sobe +150pts (= 0.75R, > 0.5R threshold).
- Candle de reversão: low cai abaixo do entry → SL dispara.

Resultado esperado:
- profit_lock_r=0.0 (off): SL fica em 98000 → exit @ 98003, perda cheia (-R$206).
- profit_lock_r=0.5 (on): após +150pts >= 0.5×200=100, SL move para entry+tick
  → exit @ 100008, zero-loss (-R$1.20 = só commission).

Trailing/BE-temporal/time-trail são DESLIGADOS (valores 999/99999) para isolar
o efeito do profit-lock.
"""
import sys
import os
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import pandas as pd  # noqa: E402
import numpy as np  # noqa: E402


def _make_profit_then_reversal_df():
    """
    DataFrame: warmup plano (ATR baixa) + entry + lucro + reversão.

    O warmup tem range minúsculo para ATR ~7, evitando ativação de trailing
    com trail_activate alto. Candles de lucro/reversão têm high/low controlados.
    """
    np.random.seed(42)
    base = 100000
    closes, highs, lows = [], [], []
    # warmup 65 candles: oscilação de 5pts (ATR baixa)
    for i in range(65):
        c = base + np.sin(i * 0.3) * 5
        closes.append(c)
        highs.append(c + 5)
        lows.append(c - 5)
    # candle de entry: plano
    closes.append(base)
    highs.append(base + 5)
    lows.append(base - 5)
    # candle de lucro: best sobe 150pts (= 0.75R para R=200)
    closes.append(base + 150)
    highs.append(base + 150)
    lows.append(base + 145)
    # candle de reversão: low cai bem abaixo do entry
    closes.append(base - 400)
    highs.append(base - 395)
    lows.append(base - 500)

    idx = pd.date_range("2026-07-01 09:05", periods=len(closes), freq="5min")
    df = pd.DataFrame({
        "close": np.array(closes, dtype=float),
        "high": np.array(highs, dtype=float),
        "low": np.array(lows, dtype=float),
        "tick_volume": np.full(len(closes), 100),
        "hour": idx.hour, "minute": idx.minute,
        "date": idx.strftime("%Y-%m-%d"),
    }, index=idx)
    return df


def _buy_sl200(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Plugin fake: BUY com SL fixo 200pts (acima do sl_min WIN=100)."""
    return {"direction": "BUY", "sl_pts": 200, "info": {}}


class TestBacktestProfitLock(unittest.TestCase):
    """Verifica que o backtest engine aplica profit-lock quando configurado."""

    def setUp(self):
        self.df = _make_profit_then_reversal_df()

    def _run_backtest(self, profit_lock_r):
        """Roda backtest_combo com profit_lock_r e exits concorrentes desligados."""
        import backtest.backtest_v944 as bt
        params = {
            "sl_atr_mult": 1.5,
            "trail_activate": 99999.0,  # trailing NUNCA ativa (isola profit-lock)
            "trail_distance": 0.5,
            "cooldown_seconds": 0,
            "breakeven_minutes": 999,   # BE temporal desligado
            "time_trail_minutes": 999,  # time-trail desligado
            "max_position_minutes": 999,
            "hard_exit_minutes": 999,
            "profit_lock_r": profit_lock_r,
        }
        with patch("backtest.backtest_v944.get_strategy_func",
                   return_value=_buy_sl200):
            trades = bt.backtest_combo(self.df, "WIN", "M5", "FAKE_BUY", params)
        return trades

    def test_profit_lock_off_loses_full_risk(self):
        """Sem profit-lock, reversão atinge SL original → perda cheia (~-R$206)."""
        trades = self._run_backtest(profit_lock_r=0.0)
        self.assertEqual(len(trades), 1, "Deve ter exatamente 1 trade")
        self.assertLess(trades[0]["pnl"], -100,
            f"Sem profit-lock, perda deveria ser cheia (< -R$100), "
            f"got {trades[0]['pnl']:.2f}")

    def test_profit_lock_on_locks_zero(self):
        """Com profit_lock_r=0.5, SL move para entry após +0.5R → exit ~zero-loss."""
        trades = self._run_backtest(profit_lock_r=0.5)
        self.assertEqual(len(trades), 1, "Deve ter exatamente 1 trade")
        self.assertGreater(trades[0]["pnl"], -10,
            f"Com profit-lock, exit deveria ser ~zero-loss (> -R$10), "
            f"got {trades[0]['pnl']:.2f}")

    def test_profit_lock_changes_pnl(self):
        """O PnL deve diferir entre on/off (prova que o param é consumido)."""
        trades_off = self._run_backtest(profit_lock_r=0.0)
        trades_on = self._run_backtest(profit_lock_r=0.5)
        self.assertEqual(len(trades_off), 1)
        self.assertEqual(len(trades_on), 1)
        diff = abs(trades_on[0]["pnl"] - trades_off[0]["pnl"])
        self.assertGreater(
            diff, 50,
            f"profit_lock_r deve mudar significativamente o PnL "
            f"(diff={diff:.2f}): on={trades_on[0]['pnl']:.2f} "
            f"vs off={trades_off[0]['pnl']:.2f}")

    def test_profit_lock_protects_better_than_off(self):
        """Profit-lock deve produzir PnL MELHOR (menos negativo) que sem lock."""
        trades_off = self._run_backtest(profit_lock_r=0.0)
        trades_on = self._run_backtest(profit_lock_r=0.5)
        self.assertGreater(
            trades_on[0]["pnl"], trades_off[0]["pnl"],
            f"profit-lock deve proteger: on ({trades_on[0]['pnl']:.2f}) "
            f"deve ser > off ({trades_off[0]['pnl']:.2f})")


if __name__ == "__main__":
    unittest.main()
