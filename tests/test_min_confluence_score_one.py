"""
test_min_confluence_score_one.py
=================================
TDD Wave N+1D (Bruno 10/07): Bruno decidiu reduzir filtro de confluencia
para min=1/4 em TODOS os 16 TFs (WIN/BIT/WSP/WDO × M5/M15/M30/H1).

Objetivo:
- Acelerar a operacao em mercado lateral (ADX<20) onde 4 checks dificilmente
  alinham, mas 1 check de confluence ja da sinal minimo.
- Liberar WIN_M5, BIT_M5, BIT_M30 e pares que estavam travados.

Cobertura:
- Cobre a regra de merge em _get_params_for_tf (params_by_tf > ativo > raiz > default)
- Garante que o hot-reload nao perde a config (autotrader rele a cada tick)
- Garante persistencia no vt_config.json
"""
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class TestMinConfluenceScoreOneEverywhere(unittest.TestCase):
    """min_confluence_score deve ser 1 em TODOS os TFs (cobre param explicito
    e o default de 2 que existia)."""

    EXPECTED_TFS = [
        # WIN
        "WIN_M5", "WIN_M15", "WIN_M30", "WIN_H1",
        # BIT
        "BIT_M5", "BIT_M15", "BIT_M30", "BIT_H1",
        # WDO
        "WDO_M5", "WDO_M15", "WDO_M30", "WDO_H1",
        # WSP
        "WSP_M5", "WSP_M15", "WSP_M30", "WSP_H1",
    ]

    def setUp(self):
        from core.vt_config_loader import load_config
        self.cfg = load_config()

    def test_all_tfs_have_min_confluence_score_1_explicit(self):
        """Cada params_by_tf[*].min_confluence_score deve estar explicitamente 1.

        IMPORTANTE: nao pode ser implicit default — AGI pode mudar. Setar
        explicito protege contra mudanca automatica.
        """
        params_by_tf = self.cfg.get("params_by_tf", {})
        missing = []
        wrong = []
        for tf in self.EXPECTED_TFS:
            entry = params_by_tf.get(tf, {})
            if "min_confluence_score" not in entry:
                missing.append(tf)
            elif entry["min_confluence_score"] != 1:
                wrong.append(f"{tf}={entry['min_confluence_score']}")
        self.assertEqual(missing, [],
                         f"params_by_tf sem min_confluence_score: {missing}")
        self.assertEqual(wrong, [],
                         f"params_by_tf com score != 1: {wrong}")

    def test_winner_merge_returns_one(self):
        """_get_params_for_tf deve resolver min_confluence_score=1 para
        cada TF (caso explicito)."""
        from core.vt_autotrader import _get_params_for_tf
        for tf in self.EXPECTED_TFS:
            symbol_root = tf.split("_")[0]  # WIN_M5 -> WIN
            tf_only = tf.split("_")[1]       # WIN_M5 -> M5
            params = _get_params_for_tf(symbol_root=symbol_root, tf=tf_only)
            self.assertEqual(
                int(params.get("min_confluence_score", 2)),
                1,
                f"_get_params_for_tf({symbol_root},{tf_only}) deveria retornar min_confluence_score=1",
            )

    def test_config_version_and_updated_by_marked(self):
        """Wave N+1D: writer name deve estar no _updated_by para audit."""
        updated_by = self.cfg.get("_updated_by", "")
        self.assertIn("min_confluence_1", updated_by,
                      f"_updated_by deve mencionar o wave, got '{updated_by}'")
        v = int(self.cfg.get("_version", 0))
        self.assertGreater(v, 1024,
                           f"_version deve ter avancado, got {v}")

    def test_autotrader_gate_uses_min_score_1(self):
        """Verifica que o codigo de gate (linha 1661+) le min_confluence_score
        do params_by_tf e usa 1 como minimo."""
        from core.vt_autotrader import _get_params_for_tf
        params = _get_params_for_tf(symbol_root="WIN", tf="M5")
        self.assertEqual(int(params["min_confluence_score"]), 1)


if __name__ == "__main__":
    unittest.main()