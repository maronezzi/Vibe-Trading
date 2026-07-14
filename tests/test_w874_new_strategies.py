"""
test_w874_new_strategies.py
============================
Wave W874 (2026-07-08): testes unitários para as 5 estratégias novas.

Estratégias:
  - VWAP_EXTREME_REVERSION  (mean-reversion em desvio de VWAP)
  - LIQUIDITY_SWEEP_REVERSAL (SMC stop hunt reversal)
  - HTF_BIAS_LTF_ENTRY      (multi-TF H1+M5)
  - ATR_EXPANSION_BREAKOUT  (vol shock breakout)
  - SESSION_MOMENTUM_CLOSE  (close window continuation)

Padrão:
  - Importação (módulo carrega, STRATEGY_NAME correto, tem check_entry)
  - Casos: sem sinal (bars vazias, ATR=0, condições não-met)
  - Casos: com sinal (todas as condições met)
  - Edge cases: ATR inválido, sessão fora da janela, etc.

Não ative no vt_config.json — estes testes NÃO validam se a estratégia
é lucrativa em produção; apenas validam que o código está correto.
"""
import sys
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)  # noqa: E402


def _brt_ts(year, month, day, hour, minute):
    brt = timezone(timedelta(hours=-3))
    dt = datetime(year, month, day, hour, minute, tzinfo=brt)
    return dt.timestamp()


def _make_bar(time_unix, open_, high, low, close, vol=1000):
    return {
        "time": int(time_unix),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "tick_volume": vol,
    }


def _make_uptrend_bars(n=100, base=100.0, atr_pts=2.0, vol=1000):
    bars = []
    price = base
    brt = timezone(timedelta(hours=-3))
    base_dt = datetime(2026, 7, 8, 9, 0, tzinfo=brt)
    for i in range(n):
        open_ = price
        close = price + 0.3
        high = close + atr_pts / 2
        low = open_ - atr_pts / 2
        ts = base_dt.timestamp() + i * 300
        bars.append(_make_bar(ts, open_, high, low, close, vol))
        price = close
    return bars


def _make_downtrend_bars(n=100, base=100.0, atr_pts=2.0, vol=1000):
    bars = []
    price = base
    brt = timezone(timedelta(hours=-3))
    base_dt = datetime(2026, 7, 8, 9, 0, tzinfo=brt)
    for i in range(n):
        open_ = price
        close = price - 0.3
        high = open_ + atr_pts / 2
        low = close - atr_pts / 2
        ts = base_dt.timestamp() + i * 300
        bars.append(_make_bar(ts, open_, high, low, close, vol))
        price = close
    return bars


def _make_choppy_bars(n=100, base=100.0, atr_pts=2.0, vol=1000):
    import random
    random.seed(42)
    bars = []
    price = base
    brt = timezone(timedelta(hours=-3))
    base_dt = datetime(2026, 7, 8, 9, 0, tzinfo=brt)
    for i in range(n):
        change = random.uniform(-0.5, 0.5)
        new_price = price + change
        high = max(price, new_price) + atr_pts / 2
        low = min(price, new_price) - atr_pts / 2
        ts = base_dt.timestamp() + i * 300
        bars.append(_make_bar(ts, price, high, low, new_price, vol))
        price = new_price
    return bars


def _make_atr_shock_bars(n=100, base_atr=1.0, shock_bars=5, shock_atr=10.0):
    """N barras; as shock_bars mais recentes têm range alto vs base."""
    bars = []
    brt = timezone(timedelta(hours=-3))
    base_dt = datetime(2026, 7, 8, 9, 0, tzinfo=brt)
    price = 100.0
    for i in range(n):
        cur_atr = shock_atr if i < shock_bars else base_atr
        high = price + cur_atr / 2
        low = price - cur_atr / 2
        vol = 2000 if i < shock_bars else 1000
        ts = base_dt.timestamp() + i * 300
        bars.append(_make_bar(ts, price, high, low, price + 0.01, vol=vol))
        price += 0.01
    return bars


def _make_utils(**overrides):
    defaults = {
        "calc_sl": MagicMock(return_value=200),
        "calculate_vwap": MagicMock(return_value=100.0),
        "calculate_ema": MagicMock(return_value=100.0),
        "calculate_rsi": MagicMock(return_value=50.0),
        "calculate_adx": MagicMock(return_value=(20.0, 25.0, 15.0)),
        "calculate_bollinger": MagicMock(return_value=(98.0, 100.0, 102.0)),
        "calculate_atr": MagicMock(return_value=2.0),
        "get_market_regime": MagicMock(return_value="TRENDING"),
    }
    defaults.update(overrides)
    return defaults


class TestVWAPExtremeReversion(unittest.TestCase):
    def setUp(self):
        from strategies import vwap_extreme_reversion
        self.s = vwap_extreme_reversion

    def test_imports(self):
        self.assertEqual(self.s.STRATEGY_NAME, "VWAP_EXTREME_REVERSION")
        self.assertTrue(callable(self.s.check_entry))

    def test_no_signal_when_bars_too_short(self):
        bars = _make_choppy_bars(20)
        utils = _make_utils()
        result = self.s.check_entry("WINQ26", "M5", 100, 2.0,
                                    bars[0]["time"], bars, {}, utils)
        self.assertIsNone(result)

    def test_no_signal_when_atr_zero(self):
        bars = _make_choppy_bars(100)
        utils = _make_utils()
        result = self.s.check_entry("WINQ26", "M5", 100, 0.0,
                                    bars[0]["time"], bars, {}, utils)
        self.assertIsNone(result)

    def test_no_signal_when_outside_session(self):
        bars = _make_choppy_bars(100)
        utils = _make_utils()
        ts_before = _brt_ts(2026, 7, 8, 9, 0)
        result = self.s.check_entry("WINQ26", "M5", 100, 2.0,
                                    ts_before, bars, {}, utils)
        self.assertIsNone(result)

    def test_no_signal_when_vwap_at_baseline(self):
        bars = _make_choppy_bars(100)
        utils = _make_utils(calculate_vwap=MagicMock(return_value=100.0))
        ts = _brt_ts(2026, 7, 8, 10, 0)
        result = self.s.check_entry("WINQ26", "M5", 100, 2.0,
                                    ts, bars, {}, utils)
        self.assertIsNone(result)

    def test_sell_signal_when_price_above_vwap_rsi_ob_volume_high(self):
        bars = _make_choppy_bars(100)
        utils = _make_utils(
            calculate_vwap=MagicMock(return_value=98.0),
            calculate_rsi=MagicMock(return_value=80.0),
            calculate_adx=MagicMock(return_value=(15.0, 25.0, 15.0)),
        )
        bars[0]["tick_volume"] = 2000
        ts = _brt_ts(2026, 7, 8, 10, 0)
        result = self.s.check_entry("WINQ26", "M5", 105, 2.0,
                                    ts, bars, {}, utils)
        self.assertIsNotNone(result)
        self.assertEqual(result["direction"], "SELL")
        self.assertEqual(result["info"]["strategy"], "VWAP_EXTREME_REVERSION")

    def test_no_signal_when_adx_too_high(self):
        bars = _make_choppy_bars(100)
        utils = _make_utils(
            calculate_vwap=MagicMock(return_value=98.0),
            calculate_rsi=MagicMock(return_value=80.0),
            calculate_adx=MagicMock(return_value=(35.0, 25.0, 15.0)),
        )
        bars[0]["tick_volume"] = 2000
        ts = _brt_ts(2026, 7, 8, 10, 0)
        result = self.s.check_entry("WINQ26", "M5", 105, 2.0,
                                    ts, bars, {}, utils)
        self.assertIsNone(result)

    def test_no_signal_when_volume_not_climax(self):
        bars = _make_choppy_bars(100)
        utils = _make_utils(
            calculate_vwap=MagicMock(return_value=98.0),
            calculate_rsi=MagicMock(return_value=80.0),
            calculate_adx=MagicMock(return_value=(15.0, 25.0, 15.0)),
        )
        ts = _brt_ts(2026, 7, 8, 10, 0)
        result = self.s.check_entry("WINQ26", "M5", 105, 2.0,
                                    ts, bars, {}, utils)
        self.assertIsNone(result)


class TestLiquiditySweepReversal(unittest.TestCase):
    def setUp(self):
        from strategies import liquidity_sweep_reversal
        self.s = liquidity_sweep_reversal

    def test_imports(self):
        self.assertEqual(self.s.STRATEGY_NAME, "LIQUIDITY_SWEEP_REVERSAL")
        self.assertTrue(callable(self.s.check_entry))

    def test_no_signal_when_bars_insufficient(self):
        bars = _make_choppy_bars(10)
        utils = _make_utils()
        result = self.s.check_entry("WINQ26", "M5", 100, 2.0,
                                    bars[0]["time"], bars, {}, utils)
        self.assertIsNone(result)

    def test_no_signal_when_atr_zero(self):
        bars = _make_choppy_bars(100)
        utils = _make_utils()
        result = self.s.check_entry("WINQ26", "M5", 100, 0.0,
                                    bars[0]["time"], bars, {}, utils)
        self.assertIsNone(result)

    def test_no_signal_when_adx_too_low(self):
        bars = _make_choppy_bars(100)
        utils = _make_utils(calculate_adx=MagicMock(return_value=(10.0, 15.0, 10.0)))
        result = self.s.check_entry("WINQ26", "M5", 100, 2.0,
                                    bars[0]["time"], bars, {}, utils)
        self.assertIsNone(result)

    def test_sell_signal_on_top_sweep_with_rejection(self):
        bars = _make_uptrend_bars(100)
        swing_high = max(b["high"] for b in bars[1:21])
        bars[0]["high"] = swing_high + 1.0
        bars[0]["close"] = swing_high - 1.0
        utils = _make_utils(
            calculate_adx=MagicMock(return_value=(25.0, 15.0, 25.0)),
        )
        result = self.s.check_entry("WINQ26", "M5", bars[0]["close"], 2.0,
                                    bars[0]["time"], bars, {}, utils)
        self.assertIsNotNone(result)
        self.assertEqual(result["direction"], "SELL")

    def test_buy_signal_on_bottom_sweep_with_rejection(self):
        bars = _make_downtrend_bars(100)
        swing_low = min(b["low"] for b in bars[1:21])
        bars[0]["low"] = swing_low - 1.0
        bars[0]["close"] = swing_low + 1.0
        utils = _make_utils(
            calculate_adx=MagicMock(return_value=(25.0, 25.0, 15.0)),
        )
        result = self.s.check_entry("WINQ26", "M5", bars[0]["close"], 2.0,
                                    bars[0]["time"], bars, {}, utils)
        self.assertIsNotNone(result)
        self.assertEqual(result["direction"], "BUY")

    def test_no_signal_when_no_sweep(self):
        bars = _make_uptrend_bars(100)
        swing_high = max(b["high"] for b in bars[1:21])
        bars[0]["high"] = swing_high - 5.0
        bars[0]["close"] = swing_high - 6.0
        utils = _make_utils(
            calculate_adx=MagicMock(return_value=(25.0, 15.0, 25.0)),
        )
        result = self.s.check_entry("WINQ26", "M5", bars[0]["close"], 2.0,
                                    bars[0]["time"], bars, {}, utils)
        self.assertIsNone(result)


class TestHTFBiasLTFEntry(unittest.TestCase):
    def setUp(self):
        from strategies import htf_bias_ltf_entry
        self.s = htf_bias_ltf_entry

    def test_imports(self):
        self.assertEqual(self.s.STRATEGY_NAME, "HTF_BIAS_LTF_ENTRY")
        self.assertTrue(callable(self.s.check_entry))

    def test_no_signal_when_bars_insufficient(self):
        utils = _make_utils()
        result = self.s.check_entry("WINQ26", "M5", 100, 2.0,
                                    1000, [], {}, utils)
        self.assertIsNone(result)

    def test_buy_signal_with_bull_htf_pullback_ltf(self):
        bars_m5 = _make_choppy_bars(100)
        bars_h1 = _make_uptrend_bars(60)
        # Mock por período: p=9 → 100 (fast), p=21 → 99 (slow) → BULL.
        ema_table = {9: 100.0, 21: 99.0}
        utils = _make_utils(
            calculate_ema=MagicMock(side_effect=lambda b, p: ema_table.get(p, 100.0)),
            calculate_adx=MagicMock(return_value=(25.0, 25.0, 15.0)),
            calculate_rsi=MagicMock(return_value=40.0),
        )
        bars_dict = {"M5": bars_m5, "H1": bars_h1}
        # Pullback zone: low ≤ ema_fast * 1.005 (=100.5), close > ema_fast (100)
        bars_m5[0]["low"] = 99.5
        bars_m5[0]["close"] = 100.5
        result = self.s.check_entry("WINQ26", "M5", 100.5, 2.0,
                                    bars_m5[0]["time"], bars_dict, {}, utils)
        self.assertIsNotNone(result)
        self.assertEqual(result["direction"], "BUY")
        self.assertEqual(result["info"]["htf_bias"], "BULL")

    def test_no_signal_when_htf_neutral(self):
        bars_m5 = _make_choppy_bars(100)
        bars_h1 = _make_choppy_bars(60)
        utils = _make_utils(
            calculate_adx=MagicMock(return_value=(10.0, 15.0, 15.0)),
        )
        bars_dict = {"M5": bars_m5, "H1": bars_h1}
        result = self.s.check_entry("WINQ26", "M5", 100.0, 2.0,
                                    bars_m5[0]["time"], bars_dict, {}, utils)
        self.assertIsNone(result)

    def test_degrades_gracefully_without_h1(self):
        bars_m5 = _make_choppy_bars(100)
        utils = _make_utils(
            calculate_adx=MagicMock(return_value=(15.0, 25.0, 15.0)),
            calculate_rsi=MagicMock(return_value=40.0),
        )
        bars_m5[0]["low"] = 99.5
        bars_m5[0]["close"] = 100.5
        result = self.s.check_entry("WINQ26", "M5", 100.5, 2.0,
                                    bars_m5[0]["time"], bars_m5, {}, utils)
        self.assertIsInstance(result, (type(None), dict))


class TestATRExpansionBreakout(unittest.TestCase):
    def setUp(self):
        from strategies import atr_expansion_breakout
        self.s = atr_expansion_breakout

    def test_imports(self):
        self.assertEqual(self.s.STRATEGY_NAME, "ATR_EXPANSION_BREAKOUT")
        self.assertTrue(callable(self.s.check_entry))

    def test_no_signal_when_bars_insufficient(self):
        bars = _make_choppy_bars(20)
        utils = _make_utils()
        result = self.s.check_entry("WINQ26", "M5", 100, 2.0,
                                    bars[0]["time"], bars, {}, utils)
        self.assertIsNone(result)

    def test_no_signal_outside_session(self):
        bars = _make_choppy_bars(100)
        utils = _make_utils()
        ts = _brt_ts(2026, 7, 8, 9, 0)
        result = self.s.check_entry("WINQ26", "M5", 100, 2.0,
                                    ts, bars, {}, utils)
        self.assertIsNone(result)

    def test_no_signal_when_no_volume_spike(self):
        bars = _make_choppy_bars(100)
        utils = _make_utils()
        ts = _brt_ts(2026, 7, 8, 10, 0)
        result = self.s.check_entry("WINQ26", "M5", 100, 2.0,
                                    ts, bars, {}, utils)
        self.assertIsNone(result)

    def test_buy_signal_with_atr_shock_and_breakout(self):
        bars = _make_atr_shock_bars(n=100, base_atr=1.0,
                                    shock_bars=5, shock_atr=10.0)
        prior_high = max(b["high"] for b in bars[1:11])
        bars[0]["close"] = prior_high + 2.0
        bars[0]["high"] = prior_high + 3.0
        bars[0]["open"] = bars[0]["low"] = prior_high
        utils = _make_utils(
            calculate_adx=MagicMock(return_value=(25.0, 25.0, 15.0)),
        )
        ts = _brt_ts(2026, 7, 8, 10, 0)
        result = self.s.check_entry("WINQ26", "M5", bars[0]["close"], 2.0,
                                    ts, bars, {}, utils)
        self.assertIsNotNone(result)
        self.assertEqual(result["direction"], "BUY")


class TestSessionMomentumClose(unittest.TestCase):
    def setUp(self):
        from strategies import session_momentum_close
        self.s = session_momentum_close

    def test_imports(self):
        self.assertEqual(self.s.STRATEGY_NAME, "SESSION_MOMENTUM_CLOSE")
        self.assertTrue(callable(self.s.check_entry))

    def test_no_signal_outside_close_window(self):
        bars = _make_choppy_bars(100)
        utils = _make_utils()
        ts = _brt_ts(2026, 7, 8, 10, 0)
        result = self.s.check_entry("WINQ26", "M5", 100, 2.0,
                                    ts, bars, {}, utils)
        self.assertIsNone(result)

    def test_no_signal_when_adx_low(self):
        bars = _make_choppy_bars(100)
        utils = _make_utils(
            calculate_adx=MagicMock(return_value=(10.0, 15.0, 15.0)),
        )
        ts = _brt_ts(2026, 7, 8, 16, 30)
        result = self.s.check_entry("WINQ26", "M5", 100, 2.0,
                                    ts, bars, {}, utils)
        self.assertIsNone(result)

    def test_no_signal_when_volume_low(self):
        bars = _make_choppy_bars(100)
        utils = _make_utils()
        ts = _brt_ts(2026, 7, 8, 16, 30)
        result = self.s.check_entry("WINQ26", "M5", 100, 2.0,
                                    ts, bars, {}, utils)
        self.assertIsNone(result)

    def test_buy_signal_in_close_window_with_bullish_setup(self):
        bars = _make_uptrend_bars(100)
        bars[0]["tick_volume"] = 2000
        bars[0]["open"] = bars[0]["close"] - 1.0
        utils = _make_utils(
            calculate_ema=MagicMock(side_effect=lambda b, p: 101.0 if p == 8 else 100.0),
            calculate_adx=MagicMock(return_value=(25.0, 25.0, 15.0)),
        )
        ts = _brt_ts(2026, 7, 8, 16, 30)
        result = self.s.check_entry("WINQ26", "M5", bars[0]["close"], 2.0,
                                    ts, bars, {}, utils)
        self.assertIsNotNone(result)
        self.assertEqual(result["direction"], "BUY")
        self.assertIn("brt_time", result["info"])


class TestW874StrategiesLoadable(unittest.TestCase):
    EXPECTED = [
        "VWAP_EXTREME_REVERSION",
        "LIQUIDITY_SWEEP_REVERSAL",
        "HTF_BIAS_LTF_ENTRY",
        "ATR_EXPANSION_BREAKOUT",
        "SESSION_MOMENTUM_CLOSE",
    ]

    def test_all_present_in_strategy_modules(self):
        import importlib
        for strat in self.EXPECTED:
            module_name = strat.lower()
            mod = importlib.import_module(f"strategies.{module_name}")
            self.assertEqual(mod.STRATEGY_NAME, strat)
            self.assertTrue(callable(mod.check_entry))

    def test_all_present_in_backtest_mirror(self):
        import importlib
        for strat in self.EXPECTED:
            module_name = strat.lower()
            mod = importlib.import_module(f"backtest.strategies.{module_name}")
            self.assertEqual(mod.STRATEGY_NAME, strat)

    def test_loader_discovers_all(self):
        from core.vt_strategy_loader import load_strategies
        strategies = load_strategies(force=True)
        for strat in self.EXPECTED:
            self.assertIn(strat, strategies, f"{strat} não foi carregado pelo loader")
            self.assertTrue(callable(strategies[strat]["check_entry"]))


if __name__ == "__main__":
    unittest.main()
