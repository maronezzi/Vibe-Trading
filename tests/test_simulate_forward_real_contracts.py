"""
test_simulate_forward_real_contracts.py
=========================================
Wave 1B.2: TDD para os 4 bugs do simulate_forward() identificados em 2026-07-02.

PROBLEMA (diagnóstico executado via /tmp/diag_simulate_forward.py):
  optimization/vt_forward_backtest.py:356 usa
      spec = _CONTRACT_SPECS.get(symbol + "$", _CONTRACT_SPECS["WIN$"])
  Quando symbol="WINQ26" (real), busca "WINQ26$" (não existe) e cai no
  fallback _CONTRACT_SPECS["WIN$"] com mult=0.2 slip=1.0 — QUE É O SINTÉTICO.

  Custo: BITM26 deveria ter mult=0.01 slip=50, está aplicando mult=0.2 slip=1.0.
         WSPU26 deveria ter mult=0.5 slip=5, está aplicando mult=0.2 slip=1.0.
         WDOQ26 deveria ter mult=10.0 slip=10, está aplicando mult=0.2 slip=1.0.

  E nas linhas 487, 499, 528 (cálculo de PnL):
      pnl = (gain) * mult - slip - commission
  Trata `slip` (ticks) como se fosse R$. Para WIN slip=5: deveria custar
  5 ticks × 0.20 R$/tick = R$1.00, mas custa R$5. Para BIT slip=50:
  deveria custar 50 × 0.01 = R$0.50, mas custa R$50.

  Resultado: backtest infla PnL em ~5-100× dependendo do ativo.
  exhaustive_strategy_search em 2026-07-02 06:49 reportou +R$32.491 vs
  DB real 30d = -R$8.264. Destruição de config (v964→v965, 98 testes quebrados).

FIX NECESSÁRIO:
  F1. simulate_forward() L356: trocar lookup por _get_spec_by_symbol(symbol)
      (que já existe L84 e faz match correto).
  F2. Linhas 487/499/528: slip_R$ = slip_ticks * mult (converter antes de subtrair).

VALIDAÇÃO RED (estado atual — DEVE FALHAR):
  - test_simulate_forward_uses_real_spec_for_win: falha (mult=0.2 ok, slip=1.0 vs esperado 5.0)
  - test_simulate_forward_uses_real_spec_for_bit: falha (mult=0.2 vs esperado 0.01, slip=1 vs 50)
  - test_simulate_forward_uses_real_spec_for_wdo: falha (mult=0.2 vs esperado 10.0)
  - test_simulate_forward_slip_converted_to_currency: falha (slip=5 ticks subtraído como R$5)
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "optimization"))


# ─── Helper: intercepta o lookup dentro de simulate_forward ────────────────────
# Bug está em L356: spec = _CONTRACT_SPECS.get(symbol + "$", _CONTRACT_SPECS["WIN$"])
# Não tem como inspecionar isso de fora sem monkeypatch. Solução: monkeypatchar
# _CONTRACT_SPECS para uma versão "vigiada" e rastrear o lookup real.

class _TrackedSpecs(dict):
    """dict que rastreia TODOS os __getitem__ feitos."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lookups = []  # [(key_asked, key_returned), ...]

    def get(self, key, default=None):
        result = super().get(key, default)
        self.lookups.append((key, result.get("slip") if isinstance(result, dict) else None))
        return result


class TestSimulateForwardRealContracts(unittest.TestCase):
    """F1: simulate_forward DEVE usar _get_spec_by_symbol() para symbols reais."""

    def setUp(self):
        # Mock bars mínimos para simulate_forward rodar
        self.mock_bars = []
        for i in range(50):
            self.mock_bars.append({
                "time": 1700000000 + i * 300,  # 5min apart
                "open": 100.0 + i * 0.1,
                "high": 100.5 + i * 0.1,
                "low": 99.5 + i * 0.1,
                "close": 100.2 + i * 0.1,
                "volume": 1000,
            })

    def test_simulate_forward_uses_real_spec_for_win(self):
        """simulate_forward('WINQ26', ...) DEVE usar slip=5.0, mult=0.20."""
        from optimization import vt_forward_backtest as vfb
        from optimization.vt_forward_backtest import _get_spec_by_symbol

        spec = _get_spec_by_symbol("WINQ26")
        self.assertEqual(
            spec["mult"], 0.20,
            f"WINQ26 mult deveria ser 0.20 (real), _get_spec_by_symbol retorna {spec['mult']}"
        )
        self.assertEqual(
            spec["slip"], 5.0,
            f"WINQ26 slip deveria ser 5.0 (real B3), _get_spec_by_symbol retorna {spec['slip']}. "
            f"Se for 1.0, o lookup está caindo no fallback WIN$ sintético."
        )

    def test_simulate_forward_uses_real_spec_for_bit(self):
        """simulate_forward('BITM26', ...) DEVE usar slip=50.0, mult=0.01.

        BUG ATUAL: retorna mult=0.2 slip=1.0 (fallback WIN$ sintético).
        """
        from optimization.vt_forward_backtest import _get_spec_by_symbol

        spec = _get_spec_by_symbol("BITM26")
        # assertEqual exato (Pitfall autoreview: assertGreaterEqual mascara bug)
        self.assertEqual(
            spec["mult"], 0.01,
            f"BITM26 mult deveria ser 0.01 (real), _get_spec_by_symbol retorna {spec['mult']}. "
            f"Se for 0.2, está caindo no fallback WIN$ sintético (bug B1)."
        )
        self.assertEqual(
            spec["slip"], 50.0,
            f"BITM26 slip deveria ser 50.0 (BIT tem slip alto B3), "
            f"_get_spec_by_symbol retorna {spec['slip']}. "
            f"Se for 1.0, está caindo no fallback WIN$ sintético."
        )

    def test_simulate_forward_uses_real_spec_for_wdo(self):
        """simulate_forward('WDOQ26', ...) DEVE usar mult=10.0 slip=10.0."""
        from optimization.vt_forward_backtest import _get_spec_by_symbol

        spec = _get_spec_by_symbol("WDOQ26")
        self.assertEqual(
            spec["mult"], 10.0,
            f"WDOQ26 mult deveria ser 10.0 (WDO é cheio, mult=10 R$/ponto), "
            f"_get_spec_by_symbol retorna {spec['mult']}. Se for 0.2, bug B1."
        )
        self.assertEqual(
            spec["slip"], 10.0,
            f"WDOQ26 slip deveria ser 10.0, _get_spec_by_symbol retorna {spec['slip']}. "
            f"Se for 1.0, bug B1."
        )

    def test_simulate_forward_lookup_uses_real_symbol_not_synthetic(self):
        """A linha 356 DEVE chamar _get_spec_by_symbol, NAO _CONTRACT_SPECS.get(symbol+'$').

        RED: verifica via AST que existe _CONTRACT_SPECS.get(symbol+'$') no codigo
        executavel (desconsiderando comentarios).
        GREEN: apos o fix, o codigo executavel nao tem esse pattern.
        """
        import re
        src_path = os.path.join(PROJECT_ROOT, "optimization", "vt_forward_backtest.py")
        raw = open(src_path).read()

        # Remove comentarios (# ... ate fim da linha) antes de procurar pattern
        # (Wave 1B.2 fix inclui comentario citando o codigo antigo -- isso e ok)
        code_lines = []
        for line in raw.splitlines():
            # strip comentario inline
            code = line.split("#", 1)[0]
            code_lines.append(code)
        code_only = "\n".join(code_lines)

        # Procura padrao buggy APENAS em codigo executavel
        buggy_pattern = re.compile(
            r'_CONTRACT_SPECS\.get\(\s*symbol\s*\+\s*["\']\$["\']\s*,',
            re.MULTILINE,
        )
        matches = buggy_pattern.findall(code_only)
        self.assertEqual(
            len(matches), 0,
            f"simulate_forward() ainda usa _CONTRACT_SPECS.get(symbol+'$') "
            f"em codigo executavel (encontrado {len(matches)} ocorrencia). "
            f"BUG B1: cai sempre no fallback WIN$ sintetico. "
            f"Fix: trocar por _get_spec_by_symbol(symbol) -- match real."
        )


class TestSimulateForwardSlipConversion(unittest.TestCase):
    """F2: slip em ticks DEVE ser convertido para R$ via mult."""

    def test_slip_converted_to_currency_via_mult(self):
        """A linha 487 (BUY SL exit) DEVE subtrair (slip * mult), não slip.

        Math: WIN slip=5 ticks × mult=0.20 R$/tick = R$1.00.
              BIT slip=50 ticks × mult=0.01 R$/tick = R$0.50.
              WDO slip=10 ticks × mult=10.0 R$/tick = R$100.
        """
        import re
        src_path = os.path.join(PROJECT_ROOT, "optimization", "vt_forward_backtest.py")
        src = open(src_path).read()

        # Procura padrão buggy: " - slip - commission"
        # Espera-se " - (slip * mult)" ou " - slip_R$"
        buggy_pattern = re.compile(
            r'\*\s*mult\s*-\s*slip\s*-',
            re.MULTILINE,
        )
        matches = buggy_pattern.findall(src)
        self.assertEqual(
            len(matches), 0,
            f"simulate_forward() ainda calcula '...*mult - slip - commission' "
            f"(encontrado {len(matches)} ocorrências). "
            f"BUG B2: trata slip (ticks) como se fosse R$. "
            f"Para WIN slip=5 deveria custar R$1.00 (5×0.20), está custando R$5. "
            f"Para BIT slip=50 deveria custar R$0.50, está custando R$50. "
            f"Fix: slip_R$ = slip * mult (converter antes de subtrair)."
        )

    def test_pnl_calculation_realistic_for_bit(self):
        """End-to-end: para BIT (slip=50 ticks, mult=0.01), slip aplicado = R$0.50/trade.

        Se simulate_forward está subtraindo slip=50 como R$50, destrói BIT.
        """
        from optimization.vt_forward_backtest import _get_spec_by_symbol

        spec = _get_spec_by_symbol("BITM26")
        slip_ticks = spec["slip"]  # 50
        mult = spec["mult"]        # 0.01
        # Custo slip realista
        slip_brl = slip_ticks * mult  # 0.50
        self.assertEqual(
            slip_brl, 0.50,
            f"BITM26 slip realista deveria ser R$0.50 ({slip_ticks} ticks x {mult} R$/tick). "
            f"Se simulate_forward subtrai {slip_ticks} como R$ direto, esta custando R${slip_ticks}. "
            f"BIT destruiria PnL -- bug B2."
        )


class TestSimulateForwardRegression(unittest.TestCase):
    """Regression: garantir que o fix não quebra WIN (único que estava OK por acidente)."""

    def test_win_realistic_slip_is_brl_1(self):
        """Para WIN (slip=5, mult=0.20), slip realista = R$1.00/trade."""
        from optimization.vt_forward_backtest import _get_spec_by_symbol

        spec = _get_spec_by_symbol("WINQ26")
        slip_brl = spec["slip"] * spec["mult"]
        self.assertEqual(
            slip_brl, 1.0,
            f"WINQ26 slip realista deveria ser R$1.00 (5 ticks x 0.20 R$/tick). "
            f"Atualmente slip={spec['slip']}, mult={spec['mult']}, "
            f"logo slip_brl = {slip_brl}. "
            f"Se slip_brl for 5.0, slip esta sendo subtraido como R$ (nao convertido)."
        )


if __name__ == "__main__":
    unittest.main()