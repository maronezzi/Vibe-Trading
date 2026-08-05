#!/usr/bin/env python3
"""Testes do gate pré-envio (validate_pre_send).

O gate roda ANTES de safe_buy/safe_sell e usa apenas checks determinísticos
locais (SL_LIMITS, ATR) para sugerir ajuste de SL. Nunca bloqueia a ordem.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))


def _order(**overrides):
    """Ordem BIT padrão para testes."""
    base = {
        "symbol": "BITN26", "direction": "BUY", "tf": "M30",
        "timeframe": "M30", "entry_price": 337920.0,
        "sl_pts": 50000, "atr": 1671.0, "strategy": "VWAP",
    }
    base.update(overrides)
    return base


class TestPreSendNeverBlocks(unittest.TestCase):
    """validate_pre_send nunca bloqueia — apenas sugere ajuste de SL."""

    @patch("vt_order_validator_v2.historical_setup_stats",
           return_value={"n_trades": 0, "win_rate": 0.0})
    def test_sl_muito_grande_nao_bloqueia(self, _hist):
        """SL acima do máximo → alerta + ajuste (aperta p/ max), mas permite."""
        from vt_order_validator_v2 import validate_pre_send
        # Wave 880.B5: BIT max agora 50000 (alinhado c/ _calc_sl max_native=500).
        # 600000 > 50000 → alerta SL_MUITO_GRANDE + adjusted_sl=50000 (aperta).
        r = validate_pre_send(_order(sl_pts=600000))
        self.assertTrue(r["allowed"])
        self.assertIsNone(r["block_reason"])
        self.assertTrue(any(a["type"] == "SL_MUITO_GRANDE" for a in r["alerts"]))

    @patch("vt_order_validator_v2.historical_setup_stats",
           return_value={"n_trades": 0, "win_rate": 0.0})
    def test_sl_muito_pequeno_nao_bloqueia(self, _hist):
        """SL abaixo do mínimo → alerta + ajuste, mas permite."""
        from vt_order_validator_v2 import validate_pre_send
        r = validate_pre_send(_order(sl_pts=500))
        self.assertTrue(r["allowed"])
        self.assertIsNone(r["block_reason"])
        self.assertTrue(any(a["type"] == "SL_MUITO_PEQUENO" for a in r["alerts"]))

    @patch("vt_order_validator_v2.historical_setup_stats",
           return_value={"n_trades": 0, "win_rate": 0.0})
    def test_sl_atr_excessivo_nao_bloqueia(self, _hist):
        """SL > 3x ATR (BIT) → alerta + ajuste, mas permite."""
        from vt_order_validator_v2 import validate_pre_send
        r = validate_pre_send(_order(sl_pts=400000, atr=1000.0))
        self.assertTrue(r["allowed"])
        self.assertIsNone(r["block_reason"])
        self.assertTrue(any(a["type"] == "SL_ATR_EXCESSIVO" for a in r["alerts"]))

    @patch("vt_order_validator_v2.historical_setup_stats",
           return_value={"n_trades": 0, "win_rate": 0.0})
    def test_historical_losing_nao_bloqueia(self, _hist):
        """Histórico perdedor NÃO é mais consultado — ordem permitida."""
        from vt_order_validator_v2 import validate_pre_send
        _hist.return_value = {
            "n_trades": 15, "win_rate": 20.0, "avg_pnl": -50.0,
            "total_pnl": -750.0, "avg_duration_min": 10,
        }
        r = validate_pre_send(_order())
        self.assertTrue(r["allowed"])
        self.assertIsNone(r["block_reason"])
        self.assertFalse(any(a["type"] == "HISTORICAL_LOSING" for a in r["alerts"]))


class TestPreSendAdjust(unittest.TestCase):
    """Alertas com sugestão de SL devem gerar adjusted_sl."""

    @patch("vt_order_validator_v2.historical_setup_stats",
           return_value={"n_trades": 0, "win_rate": 0.0})
    def test_sl_atr_apertado_adjusts(self, _hist):
        """SL < 0.5x ATR → sugere 1.0x ATR, mas Wave 880.B5: clamp SÓ APERTA.

        Wave 880.B5 fix (Bruno 2026-08-05): o clamp nunca infla o SL. Antes,
        SL apertado (0.30x ATR) era "corrigido" inflando para 1.0x ATR
        (50000→167100), o que no BIT virava 141857 pts e invertia a proteção.
        Agora: SL apertado dentro do range [min,max] gera alerta, mas o
        adjusted_sl só sobe se estiver ABAIXO do min (legítimo). sl_pts=50000
        (500 nativos) está dentro do range BIT [3000, 50000], então NÃO infla.
        """
        from vt_order_validator_v2 import validate_pre_send
        r = validate_pre_send(_order(sl_pts=50000, atr=1671.0))
        self.assertTrue(r["allowed"])
        # SL 50000 (500 nativos) está dentro do range; alerta existe mas não infla.
        # adjusted_sl é None porque suggested (1.0x ATR = 167100) > current (50000)
        # e current >= min (3000) → regra "só aperta" não aplica.
        self.assertIsNone(r["adjusted_sl"])

    @patch("vt_order_validator_v2.historical_setup_stats",
           return_value={"n_trades": 0, "win_rate": 0.0})
    def test_sl_muito_pequeno_adjusts(self, _hist):
        """SL abaixo do mínimo → ajusta para o mínimo (legítimo: sobe pro min)."""
        from vt_order_validator_v2 import validate_pre_send
        # BIT min = 3000; sl_pts=500 → abaixo do min → adjusted_sl=3000 (sobe pro min)
        r = validate_pre_send(_order(sl_pts=500))
        self.assertTrue(r["allowed"])
        self.assertIsNotNone(r["adjusted_sl"])
        self.assertEqual(r["adjusted_sl"], 3000)

    @patch("vt_order_validator_v2.historical_setup_stats",
           return_value={"n_trades": 0, "win_rate": 0.0})
    def test_sl_normal_no_adjust(self, _hist):
        """SL dentro do range e >= 0.5x ATR → sem ajuste, sem alertas."""
        from vt_order_validator_v2 import validate_pre_send
        # Wave 880.B5: BIT max=50000. sl_pts=40000 → 400 nativos → 400/1671=0.24x.
        # Mas 0.24x < 0.5 gera alerta SL_ATR_MUITO_APERADO. Pra "sem alertas",
        # precisamos de sl_native >= 0.5*atr = 836 nativos = 83600 pts, MAS isso
        # excede max=50000. Então não há SL "normal sem alertas" no BIT com
        # atr=1671 neste range. Usar atr menor: atr=500, sl=40000→400/500=0.8x OK.
        r = validate_pre_send(_order(sl_pts=40000, atr=500.0))
        self.assertTrue(r["allowed"])
        self.assertIsNone(r["adjusted_sl"])
        self.assertEqual(len(r["alerts"]), 0)


class TestPreSendAllow(unittest.TestCase):
    """Ordens válidas devem passar sem alterações."""

    @patch("vt_order_validator_v2.historical_setup_stats",
           return_value={"n_trades": 0, "win_rate": 0.0})
    def test_valid_order_passes(self, _hist):
        from vt_order_validator_v2 import validate_pre_send
        r = validate_pre_send(_order(sl_pts=100000, atr=1671.0))
        self.assertTrue(r["allowed"])
        self.assertIsNone(r["block_reason"])

    @patch("vt_order_validator_v2.historical_setup_stats",
           return_value={"n_trades": 0, "win_rate": 0.0})
    def test_wdo_valid(self, _hist):
        from vt_order_validator_v2 import validate_pre_send
        r = validate_pre_send(_order(
            symbol="WDON26", sl_pts=15000, atr=12.0,
            entry_price=5073.0,
        ))
        self.assertTrue(r["allowed"])

    @patch("vt_order_validator_v2.historical_setup_stats",
           return_value={"n_trades": 0, "win_rate": 0.0})
    def test_win_sl_muito_pequeno_nao_bloqueia(self, _hist):
        """WIN com SL abaixo do mínimo → ajusta, mas permite."""
        from vt_order_validator_v2 import validate_pre_send
        r = validate_pre_send(_order(
            symbol="WINN26", sl_pts=50, atr=800.0,
            entry_price=170500.0,
        ))
        self.assertTrue(r["allowed"])
        self.assertIsNotNone(r["adjusted_sl"])


if __name__ == "__main__":
    unittest.main()
