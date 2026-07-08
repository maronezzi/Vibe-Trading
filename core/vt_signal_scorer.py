"""
vt_signal_scorer.py — Wave N+3A (2026-07-08)

MTF confluence scoring: complementa o ``check_entry`` das estratégias com um
score multi-TF. Substitui o "single-TF" implícito que o autotrader fazia
antes (apenas M5/H1 ignorados, ver AGENTS.md §2.1).

API:
- ``score_signal(signal_result, htf_context) -> float`` (0..1).
- Gating em ``check_and_trade`` rejeita entry com score
  < min_confluence_score e loga em signal_blocked_log
  (block_reason='MTF_LOW_SCORE').

Heurística (provisional até Wave N+5 calibrar com backtest):
- Sem contexto HTF (estratégia não tem info de bias) → score 0.5 (neutro).
- direction=BUY + htf_bias='BULL' → score 0.85.
- direction=BUY + htf_bias='BEAR' → score 0.20.
- direction=SELL + htf_bias='BEAR' → score 0.85.
- direction=SELL + htf_bias='BULL' → score 0.20.
- direction=BUY/SELL + htf_bias=None → score 0.5 (HTF sem direção).
- Ajuste fino: se info['rsi'] em extremo (HTF overbought/oversold) na
  direção oposta → -0.15 cada. Se alinhado → +0.05.

Guarda: AGI pode tunar ``min_confluence_score`` por (symbol, tf) via
guardrails' SAFE_WRITE_TARGETS (§N+3A).
"""
from __future__ import annotations

import logging

log = logging.getLogger("vt_signal_scorer")

# Direção do bias HTF (string canônica).
BULL = "BULL"
BEAR = "BEAR"
NEUTRAL = "NEUTRAL"  # bias ausente ou sem tendência clara

# Limites RSI universais (pode ser custom por estratégia no params_by_tf).
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0


def score_signal(signal_result: dict, htf_context: dict | None) -> float:
    """Combina direction do signal com bias HTF + indicadores.

    Args:
        signal_result: dict no formato de check_entry (DIRECTION, info, sl_pts).
        htf_context: dict opcional com {bias, rsi, atr, regime, ...}.
            None se estratégia não tem contexto HTF (ex.: estratégias em M5
            puro sem olhar H1).

    Returns:
        float ∈ [0.0, 1.0]. Quanto mais alto, mais o sinal está alinhado
        com o contexto HTF.

    Posture: conservador. Nunca retorna score > 0.95 (não declara
    certeza absoluta — sempre há risco). Nunca retorna < 0.05 (sempre
    alguma chance, em mercados range sinais contra-tendência podem
    ganhar).
    """
    direction = signal_result.get("direction")
    if direction not in ("BUY", "SELL"):
        return 0.5  # sinal sem direção — neutro

    if not htf_context:
        return 0.5

    bias = htf_context.get("bias")  # "BULL" | "BEAR" | None

    # Base: alinhamento direcional.
    if bias == BULL:
        base = 0.85 if direction == "BUY" else 0.20
    elif bias == BEAR:
        base = 0.85 if direction == "SELL" else 0.20
    else:
        base = 0.5  # HTF neutro ou sem bias

    # Ajuste fino por RSI:
    # - RSI alinhado com bias em EXTREMO = trend continuation (recompensa +0.05).
    # - RSI oposto ao bias em EXTREMO = exhaustion/contrarian (penaliza -0.15).
    # Heurística: BUY+BULL+RSI<OS = bull exausto para bear trap = ruim para BUY;
    # BUY+BULL+RSI>OB = bull momentum forte, continuation = bom para BUY.
    rsi = htf_context.get("rsi")
    if isinstance(rsi, (int, float)):
        if direction == "BUY":
            if bias == BULL:
                if rsi > RSI_OVERBOUGHT:
                    base += 0.05      # continuation
                elif rsi < RSI_OVERSOLD:
                    base -= 0.15      # exhaustion
            elif bias == BEAR:
                if rsi > RSI_OVERBOUGHT:
                    base += 0.05      # mean-reversion alinhado
        elif direction == "SELL":
            if bias == BEAR:
                if rsi < RSI_OVERSOLD:
                    base += 0.05      # continuation
                elif rsi > RSI_OVERBOUGHT:
                    base -= 0.15      # exhaustion
            elif bias == BULL:
                if rsi < RSI_OVERSOLD:
                    base += 0.05      # mean-reversion alinhado

    # Ajuste por regime.
    regime = htf_context.get("regime")
    if regime == "RANGE" and bias is None:
        # Sinal contra range = maior edge (mean-reversion).
        # Sinal a favor de range = menor edge.
        if direction == "BUY" and (htf_context.get("last_close", 0)
                                  > htf_context.get("range_top", 1e9)):
            base += 0.05
        if direction == "SELL" and (htf_context.get("last_close", 0)
                                    < htf_context.get("range_bottom", 0)):
            base += 0.05

    # Clamp defensivo.
    return max(0.05, min(0.95, base))


def get_htf_context_for_strategy(strategy_name: str, bars_by_tf: dict) -> dict | None:
    """Adapter utilitário: extrai HTF context (H1) do dict bars_by_tf.

    Args:
        strategy_name: nome registrado em STRATEGY_NAME do plugin.
        bars_by_tf: dict {<tf>: list of bars} ou None (single-TF legacy).

    Returns:
        dict {bias, rsi, atr, regime, last_close} ou None se sem H1.
        bias default = None se HTF neutro.

    Uso:
        bars_by_tf = {"M5": bars, "H1": h1_bars}
        htf_ctx = get_htf_context_for_strategy(strategy_name, bars_by_tf)
        score = score_signal(result, htf_ctx)

    Estratégias que NÃO devem chamar essa função (single-TF puras):
    - VWAP, BOLLINGER puras, RSI_REVERSION, MEAN_REVERSION_ZSCORE
    - SMART_EMA, EMA_CROSSOVER, EMA_PULLBACK
    - SESSION_MOMENTUM_CLOSE
    - WIN_REVERSION (estratégia WIN-only single-TF)

    Estratégias QUE devem chamar (multi-TF):
    - HTF_BIAS_LTF_ENTRY (já faz, mas precisa receber dict)
    - ADX_TREND (com H1 = direção macro)
    - MOMENTUM_BREAKOUT (H1 confirma volatilidade/regime)
    - VOLATILITY_BREAKOUT (H1 confirma range expansion vs contraction)
    - ATR_EXPANSION_BREAKOUT (idem)
    """
    if not isinstance(bars_by_tf, dict):
        return None
    h1_bars = bars_by_tf.get("H1")
    if not h1_bars or len(h1_bars) < 5:
        return None

    # Bias por EMA9 × EMA21 no H1 (heurística simples; refina em Wave N+5B).
    closes_h1 = [b.get("close") for b in h1_bars if isinstance(b, dict)]
    if not closes_h1:
        return None

    if len(closes_h1) >= 21:
        ema_fast = _ema(closes_h1, 9)
        ema_slow = _ema(closes_h1, 21)
        if ema_fast is not None and ema_slow is not None:
            if ema_fast > ema_slow * 1.001:
                bias = BULL
            elif ema_fast < ema_slow * 0.999:
                bias = BEAR
            else:
                bias = NEUTRAL
        else:
            bias = None
    else:
        bias = None

    last_close = closes_h1[-1]
    return {
        "bias": bias,
        "rsi": _rsi(closes_h1, 14),
        "atr": _atr_simple(closes_h1, 14),
        "regime": None,  # futuro: usa ATR ratio ou ADX > 25
        "last_close": last_close,
        "bars_count": len(h1_bars),
    }


def _ema(values: list, period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return ema


def _rsi(values: list, period: int = 14) -> float | None:
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    if len(gains) < period:
        return None
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _atr_simple(values: list, period: int = 14) -> float | None:
    """ATR aproximado — usa True Range em closes (proxy sem high/low).
    Suficiente para htf_context; estratégia usa utils['calculate_atr'] real."""
    if len(values) < 2:
        return None
    diffs = [abs(values[i] - values[i - 1]) for i in range(1, len(values))]
    return sum(diffs[-period:]) / min(period, len(diffs))


__all__ = [
    "BULL",
    "BEAR",
    "NEUTRAL",
    "score_signal",
    "get_htf_context_for_strategy",
]
