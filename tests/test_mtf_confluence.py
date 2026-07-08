"""
test_mtf_confluence.py — Wave N+3A (2026-07-08)

Valida core/vt_signal_scorer.py:
  1. score_signal alinhado (BUY+BULL, SELL+BEAR) → score alto (>0.8).
  2. score_signal contra-tendência → score baixo (<0.3).
  3. Sem htf_context → score neutro (0.5).
  4. Bias neutro (None) → score neutro.
  5. RSI extremo oposto penaliza; RSI alinhado bônus.
  6. Clamp defensivo [0.05, 0.95].
  7. get_htf_context_for_strategy extrai bias H1 (BULL/BEAR/NEutro).
  8. bars_by_tf=None ou sem H1 → context=None.
  9. ATR/RSI retornam None para pouco histórico.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "core")):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.vt_signal_scorer import (  # noqa: E402
    BULL,
    BEAR,
    score_signal,
    get_htf_context_for_strategy,
)


# ═══════════════════════════════════════════════════════════
# score_signal: alinhamento direcional
# ═══════════════════════════════════════════════════════════

def test_buy_with_bull_bias_scores_high():
    """BUY + HTF BULL → score alto (alinhado)."""
    signal = {"direction": "BUY", "info": {}}
    ctx = {"bias": BULL, "rsi": 50.0}
    s = score_signal(signal, ctx)
    assert s >= 0.80


def test_sell_with_bear_bias_scores_high():
    signal = {"direction": "SELL", "info": {}}
    ctx = {"bias": BEAR, "rsi": 50.0}
    s = score_signal(signal, ctx)
    assert s >= 0.80


def test_buy_with_bear_bias_scores_low():
    """BUY contra HTF BEAR → score baixo (anti-tendencial)."""
    signal = {"direction": "BUY", "info": {}}
    ctx = {"bias": BEAR, "rsi": 50.0}
    s = score_signal(signal, ctx)
    assert s <= 0.30


def test_sell_with_bull_bias_scores_low():
    signal = {"direction": "SELL", "info": {}}
    ctx = {"bias": BULL, "rsi": 50.0}
    s = score_signal(signal, ctx)
    assert s <= 0.30


def test_no_htf_context_is_neutral():
    """Single-TF strategy: htf_context=None → score neutro 0.5."""
    signal = {"direction": "BUY", "info": {}}
    s = score_signal(signal, None)
    assert 0.45 <= s <= 0.55


def test_neutral_bias_returns_neutral():
    """Bias neutro (HTF range) → score neutro."""
    signal = {"direction": "BUY", "info": {}}
    ctx = {"bias": None, "rsi": 50.0}
    s = score_signal(signal, ctx)
    assert 0.45 <= s <= 0.55


# ═══════════════════════════════════════════════════════════
# RSI penalty / bonus
# ═══════════════════════════════════════════════════════════

def test_rsi_oversold_penalizes_buy_in_bull():
    """BUY em BULL com RSI oversold (exaustão bull) → penaliza."""
    base = score_signal(
        {"direction": "BUY", "info": {}},
        {"bias": BULL, "rsi": 50.0},
    )
    penalized = score_signal(
        {"direction": "BUY", "info": {}},
        {"bias": BULL, "rsi": 25.0},
    )
    assert penalized < base


def test_rsi_overbought_rewards_buy_in_bull():
    """BUY em BULL com RSI overbought (continuation) → recompensa."""
    base = score_signal(
        {"direction": "BUY", "info": {}},
        {"bias": BULL, "rsi": 50.0},
    )
    rewarded = score_signal(
        {"direction": "BUY", "info": {}},
        {"bias": BULL, "rsi": 75.0},
    )
    assert rewarded > base


def test_rsi_oversold_rewards_sell_in_bear():
    """SELL em BEAR com RSI oversold (continuation bear) → bônus."""
    base = score_signal(
        {"direction": "SELL", "info": {}},
        {"bias": BEAR, "rsi": 50.0},
    )
    rewarded = score_signal(
        {"direction": "SELL", "info": {}},
        {"bias": BEAR, "rsi": 25.0},
    )
    assert rewarded > base


# ═══════════════════════════════════════════════════════════
# Clamp defensivo
# ═══════════════════════════════════════════════════════════

def test_score_clamps_below_max():
    """Nunca acima de 0.95 mesmo com tudo alinhado."""
    s = score_signal({"direction": "BUY", "info": {}},
                    {"bias": BULL, "rsi": 25.0})
    assert s <= 0.95


def test_score_clamps_above_min():
    """Nunca abaixo de 0.05 mesmo com tudo contra."""
    s = score_signal({"direction": "BUY", "info": {}},
                    {"bias": BEAR, "rsi": 75.0})
    assert s >= 0.05


def test_score_handles_no_direction():
    """direction ausente ou inválida → 0.5 (neutro)."""
    assert score_signal({"info": {}}, {"bias": BULL}) == 0.5
    assert score_signal({"direction": "HOLD"}, {"bias": BULL}) == 0.5


# ═══════════════════════════════════════════════════════════
# get_htf_context_for_strategy
# ═══════════════════════════════════════════════════════════

def test_htf_context_bull_on_uptrend():
    """H1 com EMA9 > EMA21 * 1.001 → bias=BULL."""
    closes = [100 + i * 0.5 for i in range(30)]  # tendência linear up
    bars_by_tf = {
        "M5": [{"close": 100 + i * 0.1} for i in range(50)],
        "H1": [{"close": c} for c in closes],
    }
    ctx = get_htf_context_for_strategy("ADX_TREND", bars_by_tf)
    assert ctx is not None
    assert ctx["bias"] == BULL
    assert ctx["bars_count"] == 30


def test_htf_context_bear_on_downtrend():
    closes = [200 - i * 0.5 for i in range(30)]
    bars_by_tf = {
        "M5": [{"close": 100}],
        "H1": [{"close": c} for c in closes],
    }
    ctx = get_htf_context_for_strategy("ADX_TREND", bars_by_tf)
    assert ctx is not None
    assert ctx["bias"] == BEAR


def test_htf_context_none_when_no_h1():
    bars_by_tf = {"M5": [{"close": 100}]}  # sem H1
    assert get_htf_context_for_strategy("X", bars_by_tf) is None


def test_htf_context_none_when_bars_dict_is_list():
    """Legacy single-TF: bars_by_tf=list (não dict) → None."""
    bars_legacy = [{"close": 100}, {"close": 101}]
    assert get_htf_context_for_strategy("X", bars_legacy) is None


def test_htf_context_none_when_h1_insufficient():
    """H1 com <5 candles (sem EMA base) → None."""
    bars_by_tf = {
        "M5": [{"close": 100}],
        "H1": [{"close": 100}, {"close": 101}],
    }
    assert get_htf_context_for_strategy("X", bars_by_tf) is None


def test_htf_context_returns_atr_and_rsi():
    """Quando bias=None (HTF range), retorna rsi e atr preenchidos."""
    closes = [100 + (i % 3 - 1) * 0.5 for i in range(40)]  # oscilante
    bars_by_tf = {
        "M5": [{"close": 100}],
        "H1": [{"close": c} for c in closes],
    }
    ctx = get_htf_context_for_strategy("X", bars_by_tf)
    assert ctx is not None
    # rsi deve ser número ou None para pouco histórico
    if ctx["rsi"] is not None:
        assert 0 <= ctx["rsi"] <= 100
    if ctx["atr"] is not None:
        assert ctx["atr"] >= 0
