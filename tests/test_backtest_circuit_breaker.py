"""
test_backtest_circuit_breaker.py
================================
Testa que o backtest engine (backtest_v944.py) modela o circuit breaker.

Cenário sintético: a estratégia dispara BUY a cada candle livre, cada trade
resulta em loss (low cai abaixo do SL fixo). Após N losses consecutivas,
o circuit breaker ativa halt que bloqueia entradas subsequentes.

- Com max_consecutive_losses=999 (off): todos os trades abrem.
- Com max_consecutive_losses=3: após 3 losses, halt de 120min bloqueia o resto.
"""
import sys
import os
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import pandas as pd  # noqa: E402
import numpy as np  # noqa: E402


def _make_loss_streak_df():
    """
    DataFrame sintético com ATR controlada e quedas que sempre atingem SL.

    65 candles de warmup (oscilação suave para ATR > 0), depois candles
    planos com low MUITO baixo (-800pts) para garantir SL hit em qualquer
    configuração de sl_pts.
    """
    np.random.seed(42)
    prices = []
    base = 100000
    # warmup: oscilação suave gera ATR baixa e estável
    for i in range(65):
        prices.append(base + np.sin(i * 0.3) * 10)
    # zona de trades: candles planos, mas low cai muito
    for _ in range(10):
        prices.append(base)

    idx = pd.date_range("2026-07-01 09:05", periods=len(prices), freq="5min")
    closes = np.array(prices, dtype=float)
    # low = close - 800 garante que qualquer SL <= 800 pts dispara
    lows = closes - 800
    highs = closes + 100
    df = pd.DataFrame({
        "close": closes, "high": highs, "low": lows,
        "tick_volume": np.full(len(prices), 100),
        "hour": idx.hour, "minute": idx.minute,
        "date": idx.strftime("%Y-%m-%d"),
    }, index=idx)
    return df


def _buy_fixed_sl(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Plugin fake: sempre BUY com SL fixo de 150 pts (acima do sl_min WIN=100)."""
    return {"direction": "BUY", "sl_pts": 150, "info": {}}


class TestBacktestCircuitBreaker(unittest.TestCase):
    """Verifica que o backtest engine ativa halt após N losses consecutivas."""

    def setUp(self):
        self.df = _make_loss_streak_df()

    def _run_backtest(self, max_consecutive_losses=999, halt_duration_minutes=60):
        """Roda backtest_combo com circuit breaker params."""
        import backtest.backtest_v944 as bt
        params = {
            "sl_atr_mult": 1.5,
            "trail_activate": 10.0,   # trailing nunca ativa
            "trail_distance": 0.5,
            "cooldown_seconds": 0,    # sem cooldown (isolamos circuit breaker)
            "breakeven_minutes": 0,   # BE desligado
            "time_trail_minutes": 0,
            "max_position_minutes": 999,
            "hard_exit_minutes": 999,
            "max_consecutive_losses": max_consecutive_losses,
            "halt_duration_minutes": halt_duration_minutes,
        }
        with patch("backtest.backtest_v944.get_strategy_func",
                   return_value=_buy_fixed_sl):
            trades = bt.backtest_combo(self.df, "WIN", "M5", "FAKE_BUY", params)
        return trades

    def test_breaker_off_opens_many_trades(self):
        """Com max=999 (off), vários trades abrem (todos losses)."""
        trades = self._run_backtest(max_consecutive_losses=999)
        self.assertGreaterEqual(
            len(trades), 5,
            f"Sem circuit breaker, deve abrir >=5 trades, got {len(trades)}")

    def test_breaker_on_limits_to_threshold(self):
        """Com max=3 e halt=120min, após 3 losses as entradas são bloqueadas."""
        trades = self._run_backtest(
            max_consecutive_losses=3, halt_duration_minutes=120)
        # Após 3 losses, halt 120min bloqueia o resto do dataset.
        self.assertLessEqual(
            len(trades), 4,
            f"Circuit breaker (max=3) deve limitar a <=4 trades, got {len(trades)}")

    def test_breaker_reduces_loss_count(self):
        """Circuit breaker REDUZ o número de trades vs off."""
        trades_off = self._run_backtest(max_consecutive_losses=999)
        trades_on = self._run_backtest(
            max_consecutive_losses=3, halt_duration_minutes=120)
        self.assertLess(
            len(trades_on), len(trades_off),
            f"Circuit breaker deve reduzir trades: on={len(trades_on)} "
            f"vs off={len(trades_off)}")

    def test_breaker_param_consumed(self):
        """O param max_consecutive_losses afeta o resultado (não é ignorado)."""
        trades_off = self._run_backtest(max_consecutive_losses=999)
        trades_on = self._run_backtest(
            max_consecutive_losses=3, halt_duration_minutes=120)
        self.assertNotEqual(
            len(trades_on), len(trades_off),
            f"Param deve mudar resultado: on={len(trades_on)} vs off={len(trades_off)}")

    def test_all_trades_are_losses(self):
        """Sanity: todos os trades no dataset sintético são losses (valida setup)."""
        trades = self._run_backtest(max_consecutive_losses=999)
        for t in trades:
            self.assertLess(t["pnl"], 0,
                f"Trade deveria ser loss no dataset sintético, got pnl={t['pnl']:.2f}")


if __name__ == "__main__":
    unittest.main()
