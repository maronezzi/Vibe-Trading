"""
Testes do Loop de Síntese de Estratégia (Fase 2.1).

Cobertura:
  1. Lei 2 — IND hard-killed é pulado (nunca testa nem reativa)
  2. Lei 2 — símbolo fora de ALL_SYMBOLS loga mas não crasha
  3. edge_found quando uma variação é lucrativa
  4. no_edge quando nenhuma variação é lucrativa
  5. no_bars quando fetch retorna vazio
  6. max_iterations respeita teto (não roda infinito)
  7. on_iteration callback é invocado (hook Telegram/audit)
  8. _profit_factor / _hash_params helpers
"""
from __future__ import annotations

import math
from unittest.mock import patch

import pytest

from optimization import agi_synthesizer as synth
from optimization.agi_synthesizer import (
    StrategyResult,
    SynthesisReport,
    _generate_variations,
    _hash_params,
    _profit_factor,
    synthesize_all_pairs,
    synthesize_strategy,
)


# ── Fixtures de mock ────────────────────────────────────────────────────────
def _fake_bars(n=300):
    """Bars no formato esperado (newest-first)."""
    return [{"time": i, "open": 100.0, "high": 101.0, "low": 99.0,
             "close": 100.0, "tick_volume": 10} for i in range(n)]


def _make_sim_result(pnl=0.0, n_trades=0, wr=0.0, max_dd=0.0, decision="ok"):
    return {"pnl": pnl, "n_trades": n_trades, "wr": wr,
            "max_dd": max_dd, "decision": decision}


# ── 1. Lei 2: IND hard-killed é pulado ──────────────────────────────────────
class TestHardKillLei2:
    def test_ind_symbol_short_circuits_without_backtest(self):
        """IND nunca é testado (Lei 2: índice cheio, não operado)."""
        with patch("optimization.agi_synthesizer.fetch_bars_for_backtest") as fb, \
             patch("optimization.agi_synthesizer.simulate_forward") as sf:
            rep = synthesize_strategy("IND", "M5", max_iterations=10)
            assert rep.decision == "disabled_symbol"
            assert rep.reason == "permanently_disabled"
            assert rep.iterations_run == 0
            # CRÍTICO: nem fetch nem simulate foram chamados
            fb.assert_not_called()
            sf.assert_not_called()

    def test_ind_lowercase_also_blocked(self):
        """Hard-kill é case-insensitive (IND, ind, Ind).

        Símbolos não-IND caem no fluxo normal. Mockamos fetch_bars vazio para
        NÃO tocar o MT5/Wine real de produção durante o teste.
        """
        with patch("optimization.agi_synthesizer.fetch_bars_for_backtest",
                   return_value=[]), \
             patch("optimization.agi_synthesizer.simulate_forward") as sf:
            for variant in ("ind", "Ind", "IND", "WINQ26"):
                rep = synthesize_strategy(variant, "M5", max_iterations=2)
                # IND* em qualquer casing → disabled_symbol (short-circuit, sem backtest)
                # WINQ26 contém WIN (não IND) → fluxo normal → no_bars (fetch vazio)
                if "IND" in variant.upper():
                    assert rep.decision == "disabled_symbol", variant
                else:
                    assert rep.decision == "no_bars", variant
            # CRÍTICO: IND nunca chegou ao backtest; só WINQ26 tentou (e falhou no fetch)
            sf.assert_not_called()


# ── 2. Símbolo fora de ALL_SYMBOLS ──────────────────────────────────────────
class TestInvalidSymbol:
    def test_unknown_symbol_does_not_crash(self):
        """Símbolo inválido loga warning mas retorna report, não exception."""
        with patch("optimization.agi_synthesizer.fetch_bars_for_backtest",
                   return_value=[]):
            rep = synthesize_strategy("XXX", "M5", max_iterations=5)
            assert rep.decision == "no_bars"
            assert rep.symbol == "XXX"


# ── 3. edge_found quando há lucro ───────────────────────────────────────────
class TestEdgeFound:
    def test_profitable_variation_returns_edge_found(self):
        """Uma variação lucrativa interrompe o loop e retorna edge_found."""
        calls = []

        def fake_sim(symbol, tf, bars, strat, params, config=None):
            calls.append((strat, params))
            # 3ª chamada é lucrativa
            if len(calls) == 3:
                return _make_sim_result(pnl=500.0, n_trades=20,
                                        wr=0.60, decision="ok")
            return _make_sim_result(pnl=-50.0, n_trades=10,
                                    wr=0.30, decision="ok")

        with patch("optimization.agi_synthesizer.fetch_bars_for_backtest",
                   return_value=_fake_bars()), \
             patch("optimization.agi_synthesizer.simulate_forward",
                   side_effect=fake_sim):
            rep = synthesize_strategy("WIN", "M5", max_iterations=20)
            assert rep.decision == "edge_found"
            assert rep.best is not None
            assert rep.best.pnl == 500.0
            assert rep.best.win_rate == pytest.approx(0.60)
            assert rep.best.profit_factor == pytest.approx(0.60 / 0.40)
            assert rep.iterations_run == 3  # parou na 3ª

    def test_best_tracks_highest_pnl_even_without_edge(self):
        """Mesmo sem edge, .best é a variação de maior PnL."""
        seq = [
            _make_sim_result(pnl=-100.0, n_trades=5, wr=0.2, decision="ok"),
            _make_sim_result(pnl=-10.0, n_trades=8, wr=0.375, decision="ok"),
            _make_sim_result(pnl=-200.0, n_trades=4, wr=0.25, decision="ok"),
        ]
        with patch("optimization.agi_synthesizer.fetch_bars_for_backtest",
                   return_value=_fake_bars()), \
             patch("optimization.agi_synthesizer.simulate_forward",
                   side_effect=seq):
            rep = synthesize_strategy("WDO", "M15", max_iterations=3)
            assert rep.decision == "no_edge"
            assert rep.best.pnl == pytest.approx(-10.0)


# ── 4. no_edge quando nada é lucrativo ──────────────────────────────────────
class TestNoEdge:
    def test_all_negative_returns_no_edge(self):
        with patch("optimization.agi_synthesizer.fetch_bars_for_backtest",
                   return_value=_fake_bars()), \
             patch("optimization.agi_synthesizer.simulate_forward",
                   return_value=_make_sim_result(pnl=-50.0, n_trades=10,
                                                 wr=0.30, decision="ok")):
            rep = synthesize_strategy("BIT", "M5", max_iterations=5)
            assert rep.decision == "no_edge"
            assert rep.iterations_run == 5

    def test_zero_trades_does_not_qualify_as_edge(self):
        """decision='ok' mas 0 trades não é edge (sem dados p/ validar)."""
        with patch("optimization.agi_synthesizer.fetch_bars_for_backtest",
                   return_value=_fake_bars()), \
             patch("optimization.agi_synthesizer.simulate_forward",
                   return_value=_make_sim_result(pnl=0.0, n_trades=0,
                                                 wr=0.0, decision="ok")):
            rep = synthesize_strategy("WSP", "M30", max_iterations=3)
            assert rep.decision == "no_edge"


# ── 5. no_bars quando fetch falha ───────────────────────────────────────────
class TestNoBars:
    def test_empty_bars_returns_no_bars(self):
        with patch("optimization.agi_synthesizer.fetch_bars_for_backtest",
                   return_value=[]):
            rep = synthesize_strategy("WIN", "M5", max_iterations=5)
            assert rep.decision == "no_bars"

    def test_fetch_exception_returns_no_bars_not_crash(self):
        with patch("optimization.agi_synthesizer.fetch_bars_for_backtest",
                   side_effect=RuntimeError("Wine down")):
            rep = synthesize_strategy("WIN", "M5", max_iterations=5)
            assert rep.decision == "no_bars"
            assert "fetch_error" in rep.reason


# ── 6. max_iterations respeita teto ─────────────────────────────────────────
class TestIterationCap:
    def test_max_iterations_caps_total_backtests(self):
        with patch("optimization.agi_synthesizer.fetch_bars_for_backtest",
                   return_value=_fake_bars()), \
             patch("optimization.agi_synthesizer.simulate_forward",
                   return_value=_make_sim_result(pnl=-1.0, n_trades=5,
                                                 wr=0.2, decision="ok")) as sf:
            rep = synthesize_strategy("WIN", "M5", max_iterations=4)
            assert rep.iterations_run == 4
            assert sf.call_count == 4  # não excedeu o teto


# ── 7. on_iteration callback ────────────────────────────────────────────────
class TestCallback:
    def test_on_iteration_invoked_per_backtest(self):
        seen = []
        with patch("optimization.agi_synthesizer.fetch_bars_for_backtest",
                   return_value=_fake_bars()), \
             patch("optimization.agi_synthesizer.simulate_forward",
                   return_value=_make_sim_result(pnl=-1.0, n_trades=5,
                                                 wr=0.2, decision="ok")):
            synthesize_strategy("WIN", "M5", max_iterations=3,
                                on_iteration=lambda r, n: seen.append((n, r.pnl)))
            assert len(seen) == 3
            assert seen[0][0] == 1
            assert seen[-1][0] == 3

    def test_callback_exception_does_not_crash_synthesis(self):
        def bad_callback(r, n):
            raise ValueError("telegram down")

        with patch("optimization.agi_synthesizer.fetch_bars_for_backtest",
                   return_value=_fake_bars()), \
             patch("optimization.agi_synthesizer.simulate_forward",
                   return_value=_make_sim_result(pnl=-1.0, n_trades=5,
                                                 wr=0.2, decision="ok")):
            rep = synthesize_strategy("WIN", "M5", max_iterations=3,
                                      on_iteration=bad_callback)
            # Sobreviveu ao callback quebrado
            assert rep.iterations_run == 3


# ── 8. Helpers ──────────────────────────────────────────────────────────────
class TestHelpers:
    def test_profit_factor_zero_trades(self):
        assert _profit_factor(0.0, 0, 0.0) == 0.0

    def test_profit_factor_perfect_winrate(self):
        assert math.isinf(_profit_factor(100.0, 10, 1.0))

    def test_profit_factor_zero_winrate(self):
        assert _profit_factor(-100.0, 10, 0.0) == 0.0

    def test_profit_factor_balanced(self):
        # wr=0.6 → PF = 0.6/0.4 = 1.5
        assert _profit_factor(50.0, 10, 0.6) == pytest.approx(1.5)

    def test_hash_params_stable_and_distinct(self):
        a = _hash_params({"sl_atr_mult": 1.5, "cooldown_seconds": 300})
        b = _hash_params({"sl_atr_mult": 1.5, "cooldown_seconds": 300})
        c = _hash_params({"sl_atr_mult": 2.0, "cooldown_seconds": 300})
        assert a == b           # estável
        assert a != c           # distinto
        assert len(a) == 8      # formato

    def test_generate_variations_combines_grids(self):
        base = {"sl_atr_mult": 1.5}
        variations = _generate_variations("RSI_REVERSION", base)
        # RSI_REVERSION tem grid próprio + UNIVERSAL_PARAMS
        assert len(variations) > 1
        # Cada variação preserva chaves não-grid do base
        for v in variations:
            assert "sl_atr_mult" in v
            assert "cooldown_seconds" in v
            assert "rsi_period" in v  # do grid específico


# ── 9. synthesize_all_pairs (integração leve) ───────────────────────────────
class TestSynthesizeAllPairs:
    def test_all_pairs_skips_ind(self):
        """synthesize_all_pairs pula IND mas roda os demais (Lei 2)."""
        with patch("optimization.agi_synthesizer.fetch_bars_for_backtest",
                   return_value=[]), \
             patch("optimization.agi_synthesizer.simulate_forward") as sf:
            reports = synthesize_all_pairs(
                symbols=["WIN", "IND", "WDO"],
                timeframes=["M5"],
                max_iterations=2,
            )
            # IND pulado (disabled_symbol), WIN/WDO foram pro fluxo (no_bars)
            assert reports["IND_M5"].decision == "disabled_symbol"
            assert reports["WIN_M5"].decision == "no_bars"
            assert reports["WDO_M5"].decision == "no_bars"
            # simulate_forward nunca chamado (todos falharam no fetch vazio)
            sf.assert_not_called()
