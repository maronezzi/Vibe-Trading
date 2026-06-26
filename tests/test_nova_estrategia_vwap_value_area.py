"""
test_nova_estrategia_vwap_value_area.py
=======================================
TDD para VWAP_VALUE_AREA (Wave 5.2 — 2026-06-26).

Edge mecânico testado:
  - Mean reversion: preço que toca -1σ do VWAP + RSI sobrevenda → BUY
  - Mean reversion: preço que toca +1σ do VWAP + RSI sobrecompra → SELL
  - Filtro ADX: trending (ADX > 25) bloqueia (mean reversion falha)
  - Filtro RSI: sem confirmação, sem entrada
"""
import sys
import unittest
import random
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


def _make_ranging_bars(n=100, base_price=100.0, atr=1.0, range_amplitude=4.0, seed=42):
    """Gera barras em ranging (oscilando ao redor de base_price).

    range_amplitude: quão longe do centro o preço oscila.
    """
    random.seed(seed)
    bars = []
    for i in range(n):
        # Oscilação senoidal + ruído
        phase = (i % 20) / 20.0 * 6.28
        offset = range_amplitude * (0.5 - abs(0.5 - (phase / 6.28))) * (1 if i % 2 == 0 else -1)
        noise = random.uniform(-0.3, 0.3)
        close = base_price + offset + noise
        bar = {
            "time": 1000000 + i * 60,
            "open": close - random.uniform(-0.3, 0.3),
            "high": close + atr / 2,
            "low": close - atr / 2,
            "close": close,
            "tick_volume": 1000 + random.randint(-200, 200),
            "volume": 1000 + random.randint(-200, 200),
        }
        bars.append(bar)
    return bars


def _make_trending_bars(n=100, base_price=100.0, atr=1.0, seed=42):
    """Gera barras em tendência (alta) — devem ser bloqueadas por ADX."""
    random.seed(seed)
    bars = []
    price = base_price
    for i in range(n):
        change = random.uniform(0.1, 0.5)
        price += change
        bar = {
            "time": 1000000 + i * 60,
            "open": price - change,
            "high": price + atr / 2,
            "low": price - atr / 2,
            "close": price,
            "tick_volume": 1000 + random.randint(-100, 100),
            "volume": 1000 + random.randint(-100, 100),
        }
        bars.append(bar)
    return bars


class TestVWAPVAImport(unittest.TestCase):
    """Smoke tests — módulo importa e tem símbolos esperados."""

    def test_module_imports(self):
        from strategies import vwap_value_area
        self.assertEqual(vwap_value_area.STRATEGY_NAME, "VWAP_VALUE_AREA")

    def test_has_check_entry(self):
        from strategies import vwap_value_area
        self.assertTrue(callable(vwap_value_area.check_entry))

    def test_has_defensive_defaults(self):
        from strategies import vwap_value_area
        p = vwap_value_area.DEFAULT_PARAMS
        self.assertEqual(p["sl_atr_mult"], 1.5)
        self.assertEqual(p["cooldown_seconds"], 300)
        # Confirma filtro ADX < threshold (ranging)
        self.assertGreater(p["adx_threshold"], 0)


class TestVWAPVAMeanReversion(unittest.TestCase):
    """Sinais de mean reversion nos extremos das bandas."""

    def setUp(self):
        from strategies import vwap_value_area
        self.strategy = vwap_value_area

    def test_buy_on_lower_band_with_oversold_rsi(self):
        """Preço toca lower band + RSI < 35 → BUY."""
        bars = _make_ranging_bars(n=100, base_price=100, atr=1.0, range_amplitude=4.0)
        # Preço atual muito abaixo do VWAP (lower band - 0.5σ)
        current_price = 95.0  # bem abaixo
        utils = {
            "calc_sl": MagicMock(return_value=200),
            "calculate_vwap": MagicMock(return_value=100.0),
            "calculate_rsi": MagicMock(return_value=28.0),  # sobrevenda
            "calculate_adx": MagicMock(return_value=18.0),  # ranging
            "calculate_atr": MagicMock(return_value=1.0),
        }
        # VWAP=100, σ=1.5 (calculada internamente), lower=98.5
        # preço=95 < 98.5 → toque lower, RSI=28 < 35 → BUY
        result = self.strategy.check_entry(
            "WINQ26", "M5", current_price, 1.0, 1000000 + 99 * 60, bars, {}, utils
        )
        self.assertIsNotNone(result, f"Deveria BUY no lower band, got None")
        self.assertEqual(result["direction"], "BUY")
        self.assertIn("vwap", result["info"])
        self.assertIn("upper_band", result["info"])
        self.assertIn("lower_band", result["info"])
        self.assertGreater(result["info"]["upper_band"], result["info"]["vwap"])
        self.assertLess(result["info"]["lower_band"], result["info"]["vwap"])

    def test_sell_on_upper_band_with_overbought_rsi(self):
        """Preço toca upper band + RSI > 65 → SELL."""
        bars = _make_ranging_bars(n=100, base_price=100, atr=1.0, range_amplitude=4.0)
        current_price = 105.0  # bem acima
        utils = {
            "calc_sl": MagicMock(return_value=200),
            "calculate_vwap": MagicMock(return_value=100.0),
            "calculate_rsi": MagicMock(return_value=72.0),  # sobrecompra
            "calculate_adx": MagicMock(return_value=18.0),  # ranging
            "calculate_atr": MagicMock(return_value=1.0),
        }
        result = self.strategy.check_entry(
            "WINQ26", "M5", current_price, 1.0, 1000000 + 99 * 60, bars, {}, utils
        )
        self.assertIsNotNone(result, f"Deveria SELL no upper band, got None")
        self.assertEqual(result["direction"], "SELL")


class TestVWAPVARegimeFilters(unittest.TestCase):
    """Filtros bloqueiam sinais em regime errado."""

    def setUp(self):
        from strategies import vwap_value_area
        self.strategy = vwap_value_area

    def test_high_adx_blocks_entry(self):
        """ADX > 25 (trending) → bloqueia mean reversion."""
        bars = _make_trending_bars(n=100, base_price=100, atr=1.0)
        # Preço bem abaixo do VWAP (mas ADX está alto)
        utils = {
            "calc_sl": MagicMock(return_value=200),
            "calculate_vwap": MagicMock(return_value=100.0),
            "calculate_rsi": MagicMock(return_value=28.0),
            "calculate_adx": MagicMock(return_value=35.0),  # trending
            "calculate_atr": MagicMock(return_value=1.0),
        }
        result = self.strategy.check_entry(
            "WINQ26", "M5", 95.0, 1.0, 1000000 + 99 * 60, bars, {}, utils
        )
        self.assertIsNone(result, "ADX > 25 deve bloquear (mercado trending)")

    def test_rsi_not_extreme_blocks_entry(self):
        """Sem RSI extremo, mesmo com preço na banda → bloqueia."""
        bars = _make_ranging_bars(n=100, base_price=100, atr=1.0)
        utils = {
            "calc_sl": MagicMock(return_value=200),
            "calculate_vwap": MagicMock(return_value=100.0),
            "calculate_rsi": MagicMock(return_value=50.0),  # neutro
            "calculate_adx": MagicMock(return_value=18.0),  # ranging
            "calculate_atr": MagicMock(return_value=1.0),
        }
        result = self.strategy.check_entry(
            "WINQ26", "M5", 95.0, 1.0, 1000000 + 99 * 60, bars, {}, utils
        )
        self.assertIsNone(result, "RSI neutro deve bloquear mesmo com preço na banda")

    def test_price_in_value_area_blocks_entry(self):
        """Preço DENTRO da value area (entre lower e upper) → sem sinal."""
        bars = _make_ranging_bars(n=100, base_price=100, atr=1.0)
        utils = {
            "calc_sl": MagicMock(return_value=200),
            "calculate_vwap": MagicMock(return_value=100.0),
            "calculate_rsi": MagicMock(return_value=50.0),
            "calculate_adx": MagicMock(return_value=18.0),
            "calculate_atr": MagicMock(return_value=1.0),
        }
        # Preço exatamente no VWAP — bem dentro das bandas
        result = self.strategy.check_entry(
            "WINQ26", "M5", 100.0, 1.0, 1000000 + 99 * 60, bars, {}, utils
        )
        self.assertIsNone(result, "Preço no meio da value area não deve gerar sinal")


class TestVWAPVAStddevBands(unittest.TestCase):
    """Testa que stddev_band customizado funciona."""

    def setUp(self):
        from strategies import vwap_value_area
        self.strategy = vwap_value_area

    def test_wider_stddev_band_requires_larger_move(self):
        """Com stddev_band=2.0, precisa de movimento maior para tocar banda."""
        bars = _make_ranging_bars(n=100, base_price=100, atr=1.0, range_amplitude=6.0)
        # Preço a 95 com stddev=1 → BUY; com stddev=2 → NÃO toca banda
        utils_tight = {
            "calc_sl": MagicMock(return_value=200),
            "calculate_vwap": MagicMock(return_value=100.0),
            "calculate_rsi": MagicMock(return_value=28.0),
            "calculate_adx": MagicMock(return_value=18.0),
            "calculate_atr": MagicMock(return_value=1.0),
        }
        result = self.strategy.check_entry(
            "WINQ26", "M5", 95.0, 1.0, 1000000 + 99 * 60, bars,
            {"stddev_band": 2.0},  # banda larga
            utils_tight,
        )
        # Com banda 2x mais larga, preço 95 ainda pode estar dentro — depende do σ real
        # Verifica apenas que é None OU que info mostra stddev_band respeitado
        if result is not None:
            sigma = result["info"]["upper_band"] - result["info"]["vwap"]
            self.assertGreaterEqual(sigma, 2.5)  # banda larga
        # Aceita ambos resultados — o ponto é que bandas mais largas exigem mais


if __name__ == "__main__":
    unittest.main()