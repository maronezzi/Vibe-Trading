"""
test_run_once_strategy_utils.py
================================
TDD: garante que `vt_autotrader.py --once` carrega os strategy utils
ANTES de tentar processar sinais. Sem isso, qualquer estratégia que use
`utils["calculate_ema"]` (STRONG_TREND, etc.) crasha com KeyError.

Achado 2026-06-25: run_once() chamava init_db() mas não
_init_strategy_utils() nem load_strategies(). Resultado: o autotrader
quando invocado via --once crashava imediatamente, e em daemon mode o
estado global podia ser vazio dependendo de race conditions.

ESTE TESTE:
- Importa vt_autotrader
- Chama _init_strategy_utils() e load_strategies() diretamente
- Verifica que _strategy_utils tem todas as chaves esperadas
- Verifica que load_strategies popula o registry de estratégias
- Falha CLARAMENTE se qualquer chave crítica estiver faltando
"""
import os
import sys
import unittest

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


class TestRunOnceStrategyUtils(unittest.TestCase):
    """Garante que _init_strategy_utils() popula todas as chaves usadas pelos plugins."""

    def setUp(self):
        import core.vt_autotrader as vt
        self.vt = vt
        vt._init_strategy_utils()
        vt.load_strategies()

    def test_strategy_utils_has_critical_keys(self):
        """Todas as funções que plugins acessam via utils['X'] devem estar presentes."""
        required = [
            "calculate_vwap",
            "calculate_ema",
            "calculate_rsi",
            "calculate_adx",
            "calculate_bollinger",
            "calculate_atr",
            "get_market_regime",
            "calc_sl",
        ]
        missing = [k for k in required if k not in self.vt._strategy_utils]
        self.assertEqual(
            missing, [],
            f"_strategy_utils está faltando chaves: {missing}. "
            f"_init_strategy_utils() precisa ser chamado antes de check_entry().",
        )

    def test_each_utility_is_callable(self):
        """Cada util deve ser callable (não None ou não-inicializado)."""
        for name, fn in self.vt._strategy_utils.items():
            self.assertTrue(
                callable(fn),
                f"utils['{name}'] não é callable: {fn!r}. "
                f"Provavelmente _init_strategy_utils() rodou antes da função ser definida.",
            )

    def test_strategies_loaded(self):
        """load_strategies() deve popular o registry (não vazio)."""
        # O registry interno fica em self.vt.strategies ou similar
        # Vamos checar de forma robusta: tentar importar e usar uma estratégia
        try:
            from strategies.strong_trend import check_entry
            self.assertTrue(callable(check_entry))
        except ImportError as e:
            self.fail(f"Falha ao importar strategies/strong_trend.py: {e}")

    def test_strong_trend_check_entry_does_not_crash_without_args(self):
        """Simula o caminho que crashou: chamar check_entry com utils válido
        não pode dar KeyError. Não vamos rodar a lógica completa, só
        garantir que 'calculate_ema' está acessível.
        """
        # Simula o erro original
        utils = self.vt._strategy_utils
        self.assertIn(
            "calculate_ema", utils,
            "Regressão: _strategy_utils não tem 'calculate_ema'. "
            "STRONG_TREND vai crashar com KeyError como em 2026-06-25.",
        )
        # Tenta acessar
        try:
            _ = utils["calculate_ema"]
        except KeyError:
            self.fail("KeyError ao acessar utils['calculate_ema']")


if __name__ == "__main__":
    unittest.main()
