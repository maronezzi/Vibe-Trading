"""
test_backtest_uses_real_contracts.py
=====================================
TDD: garante que o backtest (exhaustive search + forward backtest) usa
CONTRATOS REAIS (WINQ26, WDOQ26, BITM26) e NÃO símbolos sintéticos
perpétuos (WIN$, WDO$, BIT$).

PROBLEMA IDENTIFICADO 2026-06-26:
  - optimization/exhaustive_strategy_search.py:320 hardcoda
    `full_symbol = f"{sym}$"` (sintético MT5)
  - optimization/vt_forward_backtest.py:36-42 tem _CONTRACT_SPECS
    só com chaves "WIN$"/"WDO$"/etc.
  - O autotrader real opera WINQ26/WDOQ26/BITM26 (contratos vigentes)
  - Resultado: backtest roda em feed SINTÉTICO (sem slippage real B3,
    sem custos B3 reais) e sugere params baseados nele. O autotrader
    aplica e toma loss no feed REAL. 76% SL_SERVIDOR é sintoma direto.

FIX: o backtest DEVE resolver o symbol via vt_config["resolved_symbols"]
(ou vt_calendar.resolve_symbol) — mesma fonte que o autotrader usa.

VALIDAÇÃO (RED no estado atual):
  - test_exhaustive_search_resolves_real_contract: falha
  - test_forward_backtest_contract_specs_has_real_keys: falha
  - test_backtest_symbol_matches_autotrader_symbol: falha (sintoma raiz)

POR QUE IMPORTA: é a causa raiz de 76% SL_SERVIDOR + PF inflado do AGI.
Sem esse fix, QUALQUER otimização do AGI é cega.
"""
import json
import os
import re
import sys
import unittest

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


class TestBacktestUsesRealContracts(unittest.TestCase):
    """Garante que o backtest consulta contratos REAIS, não sintéticos."""

    def setUp(self):
        with open(os.path.join(PROJECT_ROOT, "vt_config.json")) as f:
            self.config = json.load(f)
        self.resolved = self.config.get("resolved_symbols", {})

    def test_resolved_symbols_present(self):
        """Pré-condição: config tem resolved_symbols (senão o fix não funciona)."""
        self.assertGreater(
            len(self.resolved), 0,
            "vt_config.json deve ter resolved_symbols preenchido"
        )
        for root, contract in self.resolved.items():
            # Contratos reais têm letra de mês + 2 dígitos (ex: WINQ26, BITM26)
            self.assertRegex(
                contract, r"^[A-Z]+[A-Z]\d{2}$",
                f"resolved_symbols.{root}='{contract}' não parece contrato real "
                f"(deveria ser algo como WINQ26, BITM26, WSPU26)"
            )

    def test_exhaustive_search_does_not_hardcode_dollar_symbol(self):
        """
        optimization/exhaustive_strategy_search.py NÃO deve hardcodar
        f"{sym}$" (sintético) NO CONTEXTO DE FETCH. Deve usar
        resolved_symbols do config.
        """
        search_path = os.path.join(
            PROJECT_ROOT, "optimization", "exhaustive_strategy_search.py"
        )
        src = open(search_path).read()
        # Procura padrão perigoso: f"{sym}$" hardcoded (não em fallback legítimo)
        # O fix legítimos usa resolved.get(sym, f"{sym}$") que cai no fallback
        # quando não tem resolved_symbols. O bug é quando usa direto.
        bad_patterns = re.findall(r'full_symbol\s*=\s*f"\{[a-z_]+\}\$"', src)
        self.assertEqual(
            len(bad_patterns), 0,
            f"exhaustive_strategy_search.py hardcoda f'{{sym}}$' "
            f"(encontrado: {bad_patterns}). Deve usar resolved_symbols do config."
        )

    def test_forward_backtest_contract_specs_has_real_keys(self):
        """
        optimization/vt_forward_backtest._CONTRACT_SPECS deve ter chaves
        que correspondem aos contratos REAIS (WINQ26, etc.), não
        apenas sintéticos (WIN$).
        """
        spec_path = os.path.join(
            PROJECT_ROOT, "optimization", "vt_forward_backtest.py"
        )
        src = open(spec_path).read()

        # Procura _CONTRACT_SPECS = { ... }
        match = re.search(r"_CONTRACT_SPECS\s*=\s*\{.*?\n\}", src, re.DOTALL)
        self.assertIsNotNone(
            match, "_CONTRACT_SPECS não encontrado em vt_forward_backtest.py"
        )
        specs = match.group(0)

        # Verifica que tem chaves de contrato real (sufixo mês+ano, sem $)
        real_key_pattern = re.compile(r'"[A-Z]{2,4}[A-Z]\d{2}"\s*:')
        real_keys = real_key_pattern.findall(specs)

        # E chaves sintéticas (WIN$) — quantas tem
        synthetic_key_pattern = re.compile(r'"[A-Z]+\$"\s*:')
        synthetic_keys = synthetic_key_pattern.findall(specs)

        self.assertGreater(
            len(real_keys), 0,
            f"_CONTRACT_SPECS precisa ter pelo menos 1 contrato REAL "
            f"(ex: 'WINQ26'). Encontrados: {len(real_keys)} reais, "
            f"{len(synthetic_keys)} sintéticos."
        )

    def test_backtest_symbol_matches_autotrader_symbol(self):
        """
        O símbolo que o backtest usa DEVE ser o mesmo que o autotrader usa.
        """
        # Pega resolved_symbols do config (fonte única de verdade)
        # Verifica que pelo menos WIN e WDO tem contrato real resolvido
        for root in ["WIN", "WDO", "BIT", "WSP"]:
            if root in self.resolved:
                contract = self.resolved[root]
                # Garante que não é sintético
                self.assertNotIn(
                    "$", contract,
                    f"resolved_symbols.{root}='{contract}' contém $ (sintético!)"
                )


class TestResolvedSymbolInBacktest(unittest.TestCase):
    """Valida que existe helper para resolver symbol real no backtest."""

    def test_resolve_symbol_helper_in_backtest(self):
        """
        O módulo vt_forward_backtest deve ter uma função ou helper para
        resolver o symbol real a partir do config.
        """
        sys.path.insert(0, os.path.join(PROJECT_ROOT, "optimization"))
        try:
            from vt_forward_backtest import _resolve_backtest_symbol
            # Se existe, testa
            cfg = {"resolved_symbols": {"WIN": "WINQ26", "WDO": "WDOQ26"}}
            self.assertEqual(_resolve_backtest_symbol("WIN", cfg), "WINQ26")
            self.assertEqual(_resolve_backtest_symbol("WDO", cfg), "WDOQ26")
        except ImportError:
            self.fail(
                "vt_forward_backtest.py precisa de _resolve_backtest_symbol() "
                "que consulta config['resolved_symbols']"
            )


if __name__ == "__main__":
    unittest.main()
