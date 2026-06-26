"""
test_nova_estrategia_opening_range.py
======================================
TDD para OPENING_RANGE_BREAKOUT (Wave 5.2 — 2026-06-26).

Edge mecânico testado:
  - Range dos primeiros 30 min BRT (9:00-9:30) define OR high/low
  - Breakout acima da high → BUY
  - Breakout abaixo da low → SELL
  - Filtros: ADX < threshold rejeita, ATR floor rejeita, vol ratio rejeita
  - Time-of-day: rejeita antes do range formado e após janela frozen
"""
import sys
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


def _ts_brt(year, month, day, hour, minute=0):
    """Retorna unix timestamp para hora BRT (UTC-3) especificada."""
    brt = datetime(year, month, day, hour, minute, tzinfo=timezone(timedelta(hours=-3)))
    return brt.timestamp()


def _make_bars_day(
    day_bars_spec,
    base_vol=1000,
):
    """Constrói lista de barras para um dia.

    day_bars_spec: lista de tuplas (hour, minute, open, high, low, close)
    Retorna lista newest-first com campo 'time' (unix timestamp BRT).
    """
    bars = []
    for h, m, o, hi, lo, c in day_bars_spec:
        bars.append({
            "time": _ts_brt(2026, 6, 26, h, m),
            "open": o,
            "high": hi,
            "low": lo,
            "close": c,
            "tick_volume": base_vol,
            "volume": base_vol,
        })
    # newest first (autotrader passa assim)
    return list(reversed(bars))


def _make_history_bars(days_back=2, n_each=200, base_price=100, atr=2.0):
    """Gera histórico de dias anteriores para satisfazer min_bars."""
    import random
    random.seed(42)
    bars = []
    # 2 dias anteriores com 200 barras M5 cada = 400 barras históricas
    for day_offset in range(days_back, 0, -1):
        for i in range(n_each):
            brt_time = datetime(
                2026, 6, 24 if day_offset == 2 else 25, 9, 0, tzinfo=timezone(timedelta(hours=-3))
            ) + timedelta(minutes=5 * i)
            # Dia "passado": preços ao redor de base_price
            change = random.uniform(-0.5, 0.5)
            new_price = base_price + change
            bars.append({
                "time": brt_time.timestamp(),
                "open": base_price,
                "high": new_price + atr / 2,
                "low": new_price - atr / 2,
                "close": new_price,
                "tick_volume": 1000,
                "volume": 1000,
            })
    return bars


class TestORBImport(unittest.TestCase):
    """Smoke tests — módulo importa e tem símbolos esperados."""

    def test_module_imports(self):
        from strategies import opening_range_breakout
        self.assertEqual(opening_range_breakout.STRATEGY_NAME, "OPENING_RANGE_BREAKOUT")

    def test_has_check_entry(self):
        from strategies import opening_range_breakout
        self.assertTrue(callable(opening_range_breakout.check_entry))

    def test_has_defensive_defaults(self):
        from strategies import opening_range_breakout
        p = opening_range_breakout.DEFAULT_PARAMS
        self.assertEqual(p["sl_atr_mult"], 1.5)
        self.assertEqual(p["cooldown_seconds"], 300)
        self.assertGreater(p["adx_threshold"], 0)


class TestORBRejectsWhenRangeNotFormed(unittest.TestCase):
    """Antes dos 30 min iniciais, range não existe — sem sinal."""

    def setUp(self):
        from strategies import opening_range_breakout
        self.strategy = opening_range_breakout

    def test_no_entry_before_opening_range_complete(self):
        # Bar atual às 9:10 — só 2 barras M5 (10 min) formadas, faltam 30
        history = _make_history_bars(days_back=2, n_each=200)
        # Hoje: 9:00, 9:05, 9:10 (3 barras M5)
        day_spec = [
            (9, 0,  100.0, 100.5, 99.5, 100.2),
            (9, 5,  100.2, 101.0, 100.0, 100.8),
            (9, 10, 100.8, 102.0, 100.5, 101.5),
        ]
        day_bars = _make_bars_day(day_spec)
        bars = day_bars + history  # newest-first: day_bars já estão newest-first

        utils = {
            "calc_sl": MagicMock(return_value=300),
            "calculate_adx": MagicMock(return_value=30.0),
            "calculate_atr": MagicMock(return_value=2.0),
        }
        # Bar atual: 9:10, preço 101.5 (acima do range formado até agora)
        result = self.strategy.check_entry(
            "WDOQ26", "M5", 101.5, 2.0, _ts_brt(2026, 6, 26, 9, 10), bars, {}, utils
        )
        self.assertIsNone(result, "Não deve entrar antes do OR formado")


class TestORBBreakoutEntry(unittest.TestCase):
    """Breakout válido acima da OR high → BUY; abaixo da OR low → SELL."""

    def setUp(self):
        from strategies import opening_range_breakout
        self.strategy = opening_range_breakout

    def _build_full_day_bars(self, breakout_dir="up"):
        """Constrói barras para um dia completo:
          - 9:00-9:30 (6 barras M5): OR com range ~3.0 pts (realista WDO)
          - 10:00: breakout
        """
        history = _make_history_bars(days_back=2, n_each=200)
        # OR de 9:00-9:30 (6 barras M5, high=103, low=100, range=3.0)
        or_spec = [
            (9, 0,  100.0, 100.5, 99.8, 100.2),
            (9, 5,  100.2, 100.8, 100.0, 100.5),
            (9, 10, 100.5, 101.5, 100.3, 101.0),
            (9, 15, 101.0, 102.0, 100.8, 101.5),
            (9, 20, 101.5, 102.5, 101.2, 102.0),
            (9, 25, 102.0, 103.0, 101.5, 102.5),
        ]
        # Pós-OR: trending até breakout
        if breakout_dir == "up":
            post_or = [
                (9, 30, 102.5, 103.5, 102.3, 103.0),
                (9, 35, 103.0, 104.0, 102.8, 103.5),
                (9, 40, 103.5, 104.5, 103.3, 104.0),
                (9, 45, 104.0, 105.0, 103.8, 104.5),
                (9, 50, 104.5, 105.5, 104.3, 105.0),
                (9, 55, 105.0, 106.0, 104.8, 105.5),
                (10, 0, 105.5, 107.0, 105.3, 106.5),  # breakout up (acima de 103)
            ]
        else:
            post_or = [
                (9, 30, 102.5, 103.0, 102.0, 102.3),
                (9, 35, 102.3, 102.8, 101.5, 102.0),
                (9, 40, 102.0, 102.5, 101.0, 101.5),
                (9, 45, 101.5, 102.0, 100.5, 101.0),
                (9, 50, 101.0, 101.5, 100.0, 100.5),
                (9, 55, 100.5, 101.0, 99.5, 100.0),
                (10, 0, 100.0, 99.0, 97.5, 98.0),   # breakout down (abaixo de 100)
            ]
        day_spec = or_spec + post_or
        day_bars = _make_bars_day(day_spec)
        # Concatena com history (mais antigas no fim)
        bars = day_bars + history
        return bars

    def test_breakout_above_or_high_buy(self):
        bars = self._build_full_day_bars(breakout_dir="up")
        utils = {
            "calc_sl": MagicMock(return_value=300),
            "calculate_adx": MagicMock(return_value=25.0),  # > 15 threshold
            "calculate_atr": MagicMock(return_value=2.0),
        }
        # OR high = 103.0; preço 106.5 está bem acima
        result = self.strategy.check_entry(
            "WDOQ26", "M5", 106.5, 2.0, _ts_brt(2026, 6, 26, 10, 0), bars, {}, utils
        )
        self.assertIsNotNone(result, f"Deveria BUY no breakout, got None")
        self.assertEqual(result["direction"], "BUY")
        self.assertIn("or_high", result["info"])
        self.assertIn("or_low", result["info"])
        self.assertGreater(result["info"]["or_high"], result["info"]["or_low"])

    def test_breakout_below_or_low_sell(self):
        bars = self._build_full_day_bars(breakout_dir="down")
        utils = {
            "calc_sl": MagicMock(return_value=300),
            "calculate_adx": MagicMock(return_value=25.0),
            "calculate_atr": MagicMock(return_value=2.0),
        }
        # OR low = 99.8; preço 98.0 está bem abaixo
        result = self.strategy.check_entry(
            "WDOQ26", "M5", 98.0, 2.0, _ts_brt(2026, 6, 26, 10, 0), bars, {}, utils
        )
        self.assertIsNotNone(result, f"Deveria SELL no breakdown, got None")
        self.assertEqual(result["direction"], "SELL")


class TestORBRegimeFilters(unittest.TestCase):
    """Filtros de regime (ADX) e volatilidade bloqueiam entradas fracas."""

    def setUp(self):
        from strategies import opening_range_breakout
        self.strategy = opening_range_breakout

    def _build_bars_in_or(self, price=106.5):
        """Barras com OR formado (range ~3 pts) e preço atual em breakout."""
        history = _make_history_bars(days_back=2, n_each=200)
        or_spec = [
            (9, 0,  100.0, 100.5, 99.8, 100.2),
            (9, 5,  100.2, 100.8, 100.0, 100.5),
            (9, 10, 100.5, 101.5, 100.3, 101.0),
            (9, 15, 101.0, 102.0, 100.8, 101.5),
            (9, 20, 101.5, 102.5, 101.2, 102.0),
            (9, 25, 102.0, 103.0, 101.5, 102.5),
            (10, 0, 102.5, 107.0, 102.3, price),  # breakout
        ]
        return _make_bars_day(or_spec) + history

    def test_low_adx_blocks_entry(self):
        bars = self._build_bars_in_or()
        utils = {
            "calc_sl": MagicMock(return_value=300),
            "calculate_adx": MagicMock(return_value=10.0),  # < 15 → chop
            "calculate_atr": MagicMock(return_value=2.0),
        }
        result = self.strategy.check_entry(
            "WDOQ26", "M5", 106.5, 2.0, _ts_brt(2026, 6, 26, 10, 0), bars, {}, utils
        )
        self.assertIsNone(result, "ADX < 15 deve bloquear (mercado chop)")

    def test_high_adx_allows_entry(self):
        bars = self._build_bars_in_or()
        utils = {
            "calc_sl": MagicMock(return_value=300),
            "calculate_adx": MagicMock(return_value=28.0),  # > 15 → trending
            "calculate_atr": MagicMock(return_value=2.0),
        }
        result = self.strategy.check_entry(
            "WDOQ26", "M5", 106.5, 2.0, _ts_brt(2026, 6, 26, 10, 0), bars, {}, utils
        )
        self.assertIsNotNone(result, "ADX >= 15 deve permitir (regime ok)")
        self.assertEqual(result["direction"], "BUY")


class TestORBTimeWindow(unittest.TestCase):
    """Fora da janela de operação (após 4h de pregão), OR perde relevância."""

    def setUp(self):
        from strategies import opening_range_breakout
        self.strategy = opening_range_breakout

    def test_no_entry_late_in_day(self):
        history = _make_history_bars(days_back=2, n_each=200)
        # Barras só até 13:30 (4.5h de pregão, OR "frozen")
        or_spec = [
            (9, 0,  100.0, 100.3, 99.9, 100.1),
            (9, 5,  100.1, 100.4, 100.0, 100.2),
            (9, 10, 100.2, 100.5, 100.0, 100.3),
            (9, 15, 100.3, 100.6, 100.1, 100.4),
            (9, 20, 100.4, 100.7, 100.2, 100.5),
            (9, 25, 100.5, 100.8, 100.3, 100.6),
        ]
        # 5 horas de pregão = 60 barras M5
        post_or = []
        for i in range(60):
            m_total = 30 + i * 5
            h = 9 + m_total // 60
            m = m_total % 60
            post_or.append((h, m, 100.5, 103.0, 100.4, 102.5))
        day_spec = or_spec + post_or
        day_bars = _make_bars_day(day_spec)
        bars = day_bars + history

        utils = {
            "calc_sl": MagicMock(return_value=300),
            "calculate_adx": MagicMock(return_value=25.0),
            "calculate_atr": MagicMock(return_value=2.0),
        }
        # Bar atual às 14:30 (5.5h após abertura)
        result = self.strategy.check_entry(
            "WDOQ26", "M5", 102.5, 2.0, _ts_brt(2026, 6, 26, 14, 30), bars, {}, utils
        )
        self.assertIsNone(result, "Após janela frozen (>4h), OR não opera mais")


if __name__ == "__main__":
    unittest.main()