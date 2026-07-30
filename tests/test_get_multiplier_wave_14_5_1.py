"""Wave 14.5.1: get_multiplier deve usar fallback hardcoded CORRETO quando
contract_specs no config diverge dos valores reais validados pelo MT5.

Bug histórico: vt_config.json tem:
  WIN$: mult=1.0  (real: 0.20 - WIN mini)
  BIT$: mult=0.01 (real: 1.00)
  WDO$: mult=0.0015 (real: 10.00)

Quando get_multiplier lia o config primeiro, retornava 1.0 para WIN, fazendo
o watchdog reportar 'Prejuízo R$ 60' quando o real era R$ -13.

Este teste verifica que:
1. Mesmo com config divergente, get_multiplier retorna o valor CORRETO (fallback)
2. Loga warning quando há divergência
"""
import logging
import unittest
from unittest.mock import patch


class TestGetMultiplierDivergingConfig(unittest.TestCase):
    """Bug: config tinha WIN$.mult=1.0 mas o real é 0.20."""

    def _fake_load_config_with_wrong_mults(self):
        """Simula vt_config.json com multiplicadores errados (bug histórico)."""
        return {
            "contract_specs": {
                "WIN$": {"mult": 1.0},   # ERRADO (real 0.20)
                "BIT$": {"mult": 0.01},  # ERRADO (real 1.00)
                "WDO$": {"mult": 0.0015},  # ERRADO (real 10.00)
                "WSP$": {"mult": 0.01},   # CORRETO
            }
        }

    def test_win_q26_returns_0_20_not_1_0(self):
        """WINQ26 deve retornar 0.20 mesmo com config errado."""
        from core.vt_trade_log import get_multiplier
        with patch("core.vt_config_loader.load_config",
                   return_value=self._fake_load_config_with_wrong_mults()):
            mult = get_multiplier("WINQ26")
        self.assertEqual(mult, 0.20, f"Esperado 0.20 (WIN mini), recebi {mult}")

    def test_bit_n26_returns_1_0_not_0_01(self):
        """BITN26 deve retornar 1.00 mesmo com config errado."""
        from core.vt_trade_log import get_multiplier
        with patch("core.vt_config_loader.load_config",
                   return_value=self._fake_load_config_with_wrong_mults()):
            mult = get_multiplier("BITN26")
        self.assertEqual(mult, 1.0, f"Esperado 1.00, recebi {mult}")

    def test_wdo_n26_returns_10_0_not_0_0015(self):
        """WDON26 deve retornar 10.00 mesmo com config errado."""
        from core.vt_trade_log import get_multiplier
        with patch("core.vt_config_loader.load_config",
                   return_value=self._fake_load_config_with_wrong_mults()):
            mult = get_multiplier("WDON26")
        self.assertEqual(mult, 10.0, f"Esperado 10.00, recebi {mult}")

    def test_wsp_u26_returns_0_01_correct_in_config(self):
        """WSPU26 retorna 0.01 (correto em config E fallback)."""
        from core.vt_trade_log import get_multiplier
        with patch("core.vt_config_loader.load_config",
                   return_value=self._fake_load_config_with_wrong_mults()):
            mult = get_multiplier("WSPU26")
        self.assertEqual(mult, 0.01)

    def test_logs_warning_on_diverging_config(self):
        """Quando config diverge do fallback, deve logar warning."""
        from core import vt_trade_log
        with patch.object(vt_trade_log, "load_config",
                          return_value=self._fake_load_config_with_wrong_mults(), create=True), \
             self.assertLogs("root", level="WARNING") as cm:
            vt_trade_log.get_multiplier("WINQ26")
        # Espera warning sobre WIN
        warnings_text = "\n".join(cm.output)
        self.assertIn("WIN", warnings_text)
        self.assertIn("diverge", warnings_text.lower())


class TestGetMultiplierPNLCalculation(unittest.TestCase):
    """Reproduz o cenário real do DRAWDOWN bug 15:23 BRT 14/07/2026."""

    def test_sell_win_q26_loss_60pts_is_minus_12_reais(self):
        """SELL WINQ26 com -60pts deve reportar -R$ 12,00 (não -R$ 60)."""
        from core.vt_trade_log import get_multiplier
        mult = get_multiplier("WINQ26")
        entry = 177915.0
        current = 177975.0  # preço 60pts acima (contra SELL)
        volume = 1.0

        # SELL: lucro = (entry - current)
        pnl_pts = entry - current  # = -60
        pnl = pnl_pts * mult * volume
        self.assertAlmostEqual(pnl, -12.00, places=1,
            msg=f"SELL -60pts WINQ26 deveria ser -R$ 12,00, foi R$ {pnl:.2f}")


if __name__ == "__main__":
    unittest.main()