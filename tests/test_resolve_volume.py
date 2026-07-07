"""
test_resolve_volume.py
==========================================
Wave Per-TF (Bruno 2026-07-07): helper _resolve_volume() resolve a quantidade
de contratos por (symbol, tf) com hierarquia:

    CONFIG["volume_by_tf"][f"{ROOT}_{TF}"]   (mais específico)
      ↓ (se ausente/inválido)
    CONFIG["volume_by_symbol"][ROOT]          (nível do ativo)
      ↓ (se ausente/inválido)
    CONFIG["volume"]                          (raiz do config)
      ↓ (se ausente/inválido)
    1.0                                       (safety default)

Este helper é usado em _execute_entry() para montar o argumento `volume` do
safe_buy/safe_sell. Cada TF pode ter volume próprio (ex.: WDO_M5=2 contratos,
WDO_M15=1, etc.) — estratégia agressiva no curto + conservadora no longo.
"""
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


def _import_helpers():
    """Import lazy para evitar circular import e side effects do autotrader."""
    from core.vt_autotrader import _resolve_volume
    return _resolve_volume


class TestResolveVolumePerTF(unittest.TestCase):
    """Hierarquia: volume_by_tf > volume_by_symbol > volume > default."""

    def test_volume_by_tf_wins_when_set(self):
        """volume_by_tf tem prioridade maxima."""
        from core.vt_autotrader import _resolve_volume
        cfg = {
            "volume": 1,
            "volume_by_symbol": {"WDO": 2, "WIN": 1},
            "volume_by_tf": {"WDO_M5": 3, "WDO_M15": 1},
        }
        with patch("core.vt_autotrader.CONFIG", cfg):
            self.assertEqual(_resolve_volume("WDON26", "M5"), 3.0)
            self.assertEqual(_resolve_volume("WDON26", "M15"), 1.0)

    def test_falls_back_to_volume_by_symbol(self):
        """Se TF nao tem volume_by_tf, usa volume_by_symbol."""
        from core.vt_autotrader import _resolve_volume
        cfg = {
            "volume": 1,
            "volume_by_symbol": {"WDO": 2},
            "volume_by_tf": {"WDO_M5": 3},  # so M5 definido
        }
        with patch("core.vt_autotrader.CONFIG", cfg):
            self.assertEqual(_resolve_volume("WDON26", "M15"), 2.0)  # fallback para by_symbol
            self.assertEqual(_resolve_volume("WDON26", "M30"), 2.0)

    def test_falls_back_to_global_volume(self):
        """Se nem TF nem symbol tem, usa volume global."""
        from core.vt_autotrader import _resolve_volume
        cfg = {
            "volume": 5,
            "volume_by_symbol": {},  # sem nada para WDO
            "volume_by_tf": {},
        }
        with patch("core.vt_autotrader.CONFIG", cfg):
            self.assertEqual(_resolve_volume("WDON26", "M5"), 5.0)
            self.assertEqual(_resolve_volume("WINM26", "M5"), 5.0)

    def test_safety_default_when_all_missing(self):
        """Se tudo ausente/corrompido, retorna 1.0 (safety default)."""
        from core.vt_autotrader import _resolve_volume
        cfg = {}  # tudo vazio
        with patch("core.vt_autotrader.CONFIG", cfg):
            self.assertEqual(_resolve_volume("WDON26", "M5"), 1.0)

    def test_invalid_volume_by_tf_value_falls_through(self):
        """Valor invalido (< 1, string, None) em volume_by_tf -> tenta proximo nivel."""
        from core.vt_autotrader import _resolve_volume
        cfg = {
            "volume": 1,
            "volume_by_symbol": {"WDO": 2},
            "volume_by_tf": {
                "WDO_M5": 0,        # invalido (< 1)
                "WDO_M15": "abc",   # tipo errado
                "WDO_M30": None,    # ausente
            },
        }
        with patch("core.vt_autotrader.CONFIG", cfg):
            self.assertEqual(_resolve_volume("WDON26", "M5"), 2.0)   # 0 invalido -> fallback
            self.assertEqual(_resolve_volume("WDON26", "M15"), 2.0)  # "abc" invalido -> fallback
            self.assertEqual(_resolve_volume("WDON26", "M30"), 2.0)  # None -> fallback

    def test_extracts_symbol_root_correctly(self):
        """ROOT extraction funciona para varios contratos resolvidos."""
        from core.vt_autotrader import _resolve_volume
        cfg = {
            "volume": 1,
            "volume_by_symbol": {},
            "volume_by_tf": {
                "WIN_M5": 2,
                "WDO_M5": 3,
                "BIT_M15": 1,
                "WSP_H1": 4,
            },
        }
        with patch("core.vt_autotrader.CONFIG", cfg):
            self.assertEqual(_resolve_volume("WINQ26", "M5"), 2.0)
            self.assertEqual(_resolve_volume("WDON26", "M5"), 3.0)
            self.assertEqual(_resolve_volume("BITM26", "M15"), 1.0)
            self.assertEqual(_resolve_volume("WSPM26", "H1"), 4.0)

    def test_corrupted_config_returns_default(self):
        """Config corrompido (dict com tipo errado) nao quebra — retorna fallback."""
        from core.vt_autotrader import _resolve_volume
        # volume_by_tf com valor que nao eh dict
        cfg = {
            "volume": 1,
            "volume_by_symbol": {"WDO": 2},
            "volume_by_tf": "not_a_dict",  # tipo errado
        }
        with patch("core.vt_autotrader.CONFIG", cfg):
            # Nao pode crashar; deve cair no proximo nivel
            result = _resolve_volume("WDON26", "M5")
            self.assertIsInstance(result, float)
            self.assertGreaterEqual(result, 1.0)

    def test_returns_float_not_int(self):
        """Retorno eh sempre float (consistente com safe_buy/safe_sell)."""
        from core.vt_autotrader import _resolve_volume
        cfg = {"volume": 2, "volume_by_symbol": {}, "volume_by_tf": {}}
        with patch("core.vt_autotrader.CONFIG", cfg):
            self.assertIsInstance(_resolve_volume("WDON26", "M5"), float)


if __name__ == "__main__":
    unittest.main()
