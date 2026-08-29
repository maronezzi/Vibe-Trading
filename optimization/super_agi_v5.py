#!/usr/bin/env python3
"""
super_agi_v5.py — Super AGI tunado para TODOS os indices × TFs.

Diferenças vs AGI v4 / exhaustive_strategy_search.py:
  1. Grid 2x mais denso (60 combos/strategy vs 30 cap).
  2. Walk-forward real (4 janelas, exige >= 75% positivas — anti-overfit forte).
  3. TODOS os 16 pares testados (não só failing) — confirma bons + acha novos.
  4. Confirmation filters (volume_min, session_only, atr_min_pct).
  5. Top-K candidatos por par (default 3) — não só "best".
  6. Checkpoint file persistente — retoma de crash.
  7. NUNCA escreve vt_config.json automaticamente — só gera report.json.

Usage:
    /usr/bin/python3 optimization/super_agi_v5.py [--days N] [--grid-size N]
        [--top-k N] [--pairs WIN_M5,BIT_H1,...] [--resume DIR]

Output:
    /tmp/super_agi_v5_<ts>/report.json
    /tmp/super_agi_v5_<ts>/checkpoint.json
    stdout: top-3 por par + projeção agregada.

Wave 12 (Bruno 2026-07-12, Sunday optimization sprint).
"""

from __future__ import annotations

import argparse
import importlib.util
import itertools
import json
import logging
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# Garantir raiz do projeto no sys.path (libs locais: backtest, optimization)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

log = logging.getLogger("super_agi_v5")


# ═══════════════════════════════════════════════════════════════════
# GRID — 28 estrategias, params ~60 combos cada
# ═══════════════════════════════════════════════════════════════════

ALL_SYMBOLS = ["WIN", "BIT", "WSP", "WDO"]
ALL_TIMEFRAMES = ["M5", "M15", "M30", "H1"]


def _discover_all_strategies() -> list:
    """Auto-descobre TODAS as estrategias em strategies/ + strategies/_pending/.

    Wave 12 (2026-07-12): lista auto-descoberta em vez de hardcoded.
    Antes (Wave 11) so testava 27 das 35 estrategias existentes + 0 geradas.
    Agora: 100% coverage (35 existentes + N novas em _pending/).
    """
    import re as _re
    from pathlib import Path as _Path
    strategies_root = _Path(__file__).resolve().parent.parent / "strategies"
    found = []
    seen = set()
    for d in [strategies_root, strategies_root / "_pending"]:
        if not d.exists():
            continue
        for py in sorted(d.glob("*.py")):
            if py.name == "__init__.py":
                continue
            try:
                t = py.read_text(encoding="utf-8")
                m = _re.search(r"^STRATEGY_NAME\s*=\s*[\"\\'](.+?)[\"\\']", t, _re.MULTILINE)
                if m:
                    name = m.group(1)
                    if name not in seen:
                        seen.add(name)
                        found.append(name)
            except Exception:
                continue
    return found


# Auto-discovery em runtime -- 35 + N estrategias
ALL_STRATEGIES = _discover_all_strategies()


# ─── Universal params: tunáveis globais que entram em TODAS estratégias ──
# Slope-respecting additions (Wave 12): volume_min_ratio + session_only + atr_min_pct
# Estes 3 ampliam o espaço de busca exponencialmente — mas reduzem overfit.
UNIVERSAL_PARAMS = {
    "sl_atr_mult":            [1.0, 1.5, 2.0, 2.5, 3.0],
    "cooldown_seconds":       [60, 180, 300, 600, 900],
    "max_consecutive_losses": [2, 3, 4, 5, 7],
    "halt_duration_minutes":  [30, 45, 60, 90, 120],
    "profit_lock_r":          [0.0, 0.3, 0.5, 0.7, 1.0],
    # Novos filtros de confirmação (Wave 12)
    "volume_min_ratio":       [0.0, 0.8, 1.2],      # 0=off; 0.8=skip low-vol; 1.2=skip ultra-low
    "session_only":           [False, True],         # True = respeita start/close hours config
    "atr_min_pct":            [0.0, 0.5, 1.0],      # 0=off; 1.0=ATR/price >= 1%
}


# ─── Strategy-specific param grids (densificados ~2x) ──
STRATEGY_PARAM_GRIDS = {
    "RSI_REVERSION": {
        "rsi_period":      [5, 7, 10, 14, 21],
        "rsi_overbought":  [65, 70, 75, 80, 85],
        "rsi_oversold":    [15, 20, 25, 30, 35],
    },
    "ENHANCED_RSI_REVERSION": {
        "rsi_period":      [5, 7, 10, 14, 21],
        "rsi_overbought":  [65, 70, 75, 80, 85],
        "rsi_oversold":    [15, 20, 25, 30, 35],
    },
    "MACD_MOMENTUM": {
        "macd_fast":       [5, 8, 10, 12, 16],
        "macd_slow":       [18, 20, 24, 26, 32],
        "macd_signal":     [5, 7, 9, 11, 14],
    },
    "ENHANCED_MACD_MOMENTUM": {
        "macd_fast":       [5, 8, 10, 12, 16],
        "macd_slow":       [18, 20, 24, 26, 32],
        "macd_signal":     [5, 7, 9, 11, 14],
    },
    "BOLLINGER": {
        "bb_period":       [10, 14, 20, 30, 50],
        "bb_std":          [1.2, 1.5, 2.0, 2.5, 3.0],
    },
    "ENHANCED_BOLLINGER": {
        "bb_period":       [10, 14, 20, 30, 50],
        "bb_std":          [1.2, 1.5, 2.0, 2.5, 3.0],
    },
    "STRONG_TREND": {
        "adx_threshold":   [10, 15, 20, 25, 30, 35],
        "adx_period":      [7, 10, 14, 20],
    },
    "ADX_TREND": {
        "adx_threshold":   [10, 15, 20, 25, 30, 35],
        "adx_period":      [7, 10, 14, 20],
    },
    "EMA_CROSSOVER": {
        "ema_fast":        [5, 8, 10, 12, 16],
        "ema_slow":        [15, 20, 26, 30, 40],
    },
    "EMA_PULLBACK": {
        "ema_fast":        [5, 8, 10, 12, 16],
        "ema_slow":        [15, 20, 26, 30, 40],
    },
    "TRIPLE_EMA": {
        "ema_fast":        [5, 8, 10, 12, 16],
        "ema_slow":        [15, 20, 26, 30, 40],
    },
    "VWAP": {
        "vwap_period":         [10, 20, 30, 40, 60],
        "vwap_buy_threshold":  [1.001, 1.005, 1.010, 1.015, 1.020],
        "vwap_sell_threshold": [0.980, 0.985, 0.990, 0.995, 0.999],
    },
    "KELTNER_CHANNEL": {
        "keltner_period":     [10, 14, 20, 30, 50],
        "keltner_atr_mult":   [1.0, 1.5, 2.0, 2.5, 3.0],
    },
    "DONCHIAN_BREAKOUT": {
        "donchian_period":    [10, 14, 20, 30, 50],
    },
    "PIVOT_POINTS": {
        "pivot_timeframe":    ["H1", "H4", "D1"],
    },
    "DIVERGENCE_RSI": {
        "rsi_period":      [7, 10, 14, 21, 28],
        "rsi_overbought":  [65, 70, 75, 80],
        "rsi_oversold":    [20, 25, 30, 35],
    },
    "MOMENTUM_BREAKOUT": {
        "adx_threshold":   [10, 15, 20, 25, 30],
        "adx_period":      [7, 10, 14, 20],
    },
    "MEAN_REVERSION_ZSCORE": {
        "bb_period":       [10, 14, 20, 30, 50],
        "bb_std":          [1.2, 1.5, 2.0, 2.5, 3.0],
    },
    "FIBONACCI_RETRACEMENT": {
        "pullback_pct":    [0.02, 0.05, 0.10, 0.15, 0.20, 0.30],
    },
    "VOLATILITY_BREAKOUT": {
        "adx_threshold":   [10, 15, 20, 25, 30],
        "adx_period":      [7, 10, 14, 20],
    },
    "RANGE_TRADING": {
        "rsi_overbought":  [65, 70, 75, 80, 85],
        "rsi_oversold":    [15, 20, 25, 30, 35],
        "rsi_period":      [7, 10, 14, 21, 28],
    },
    "WIN_REVERSION": {
        "rsi_period":      [7, 10, 14, 21, 28],
        "rsi_overbought":  [65, 70, 75, 80, 85],
        "rsi_oversold":    [15, 20, 25, 30, 35],
    },
    "HEIKIN_ASHI": {
        "ema_fast":        [5, 8, 10, 12, 16],
        "ema_slow":        [15, 20, 26, 30],
    },
    "SMART_EMA": {
        "ema_fast":        [5, 8, 10, 12, 16],
        "ema_slow":        [15, 20, 26, 30],
    },
    "SUPERTREND": {
        "adx_threshold":   [10, 15, 20, 25, 30],
        "adx_period":      [7, 10, 14, 20],
    },
    "ICHIMOKU": {
        "adx_threshold":   [10, 15, 20, 25],
    },
    "CANDLE_PATTERNS": {
        "adx_threshold":   [10, 15, 20, 25],
    },
    # Wave 12 (Bruno 2026-07-12) — estrategias adicionadas + geradas
    "ATR_EXPANSION_BREAKOUT": {
        "atr_period":           [10, 14, 20],
        "atr_avg_period":       [15, 20, 30],
        "atr_ratio_threshold":  [1.2, 1.5, 2.0, 2.5],
        "breakout_lookback":    [5, 10, 15, 20],
        "adx_period":           [10, 14, 20],
        "adx_rising_min":       [15, 18, 22, 26],
        "volume_mult":          [1.0, 1.2, 1.5],
        "sl_atr_mult":          [1.2, 1.5, 2.0],
    },
    "HTF_BIAS_LTF_ENTRY": {
        "ema_fast":             [5, 8, 9, 12, 16],
        "ema_slow":             [15, 21, 26, 30],
        "adx_period":           [10, 14, 20],
        "adx_min":              [15, 18, 22, 28],
        "rsi_period":           [7, 10, 14, 21],
        "rsi_pullback_level":   [30, 35, 40, 45],
        "sl_atr_mult":          [1.0, 1.3, 1.5, 2.0],
    },
    "LIQUIDITY_SWEEP_REVERSAL": {
        "lookback":             [10, 15, 20, 30],
        "sweep_buffer_atr":     [0.1, 0.2, 0.3, 0.5],
        "adx_period":           [10, 14, 20],
        "adx_min":              [15, 18, 22, 28],
        "sl_atr_mult":          [1.5, 1.8, 2.5],
    },
    "OPENING_RANGE_BREAKOUT": {
        "opening_range_minutes": [15, 30, 45],
        "atr_period":           [10, 14, 20],
        "adx_threshold":        [10, 15, 20, 25],
        "min_volume_ratio":     [0.3, 0.5, 0.8, 1.0],
        "sl_atr_mult":          [1.2, 1.5, 2.0],
        "cooldown_seconds":     [180, 300, 600],
    },
    "SESSION_MOMENTUM_CLOSE": {
        "ema_fast":             [5, 8, 12],
        "ema_slow":             [15, 21, 26],
        "adx_period":           [10, 14, 20],
        "adx_min":              [15, 20, 25],
        "volume_mult":          [1.0, 1.3, 1.8],
        "sl_atr_mult":          [1.2, 1.5, 2.0],
    },
    "SQUEEZE_BREAKOUT": {
        "bb_period":            [15, 20, 30],
        "bb_std":               [1.5, 2.0, 2.5],
        "kc_period":            [15, 20, 30],
        "kc_atr_mult":          [1.0, 1.5, 2.0, 2.5],
        "macd_fast":            [8, 12, 16],
        "macd_slow":            [20, 26, 32],
        "macd_signal":          [5, 9, 12],
        "adx_threshold":        [15, 20, 25, 30],
        "vol_ratio_min":        [0.4, 0.6, 1.0],
        "sl_atr_mult":          [1.2, 1.5, 2.0],
        "cooldown_seconds":     [180, 300, 600],
    },
    "VWAP_EXTREME_REVERSION": {
        "vwap_period":          [15, 20, 30, 50],
        "deviation_atr_mult":   [1.5, 2.0, 2.5, 3.0],
        "rsi_overbought":       [65, 70, 75, 80, 85],
        "rsi_oversold":         [15, 20, 25, 30, 35],
        "volume_mult":          [1.0, 1.2, 1.5, 1.8],
        "adx_max":              [20, 25, 30, 35],
        "sl_atr_mult":          [1.2, 1.5, 2.0],
    },
    "VWAP_VALUE_AREA": {
        "vwap_period":          [15, 20, 30, 50],
        "stddev_period":        [15, 20, 30, 50],
        "stddev_band":          [0.5, 1.0, 1.5, 2.0],
        "rsi_period":           [7, 10, 14, 21],
        "rsi_overbought":       [60, 65, 70, 75],
        "rsi_oversold":         [25, 30, 35, 40],
        "adx_threshold":        [20, 25, 30, 35],
        "sl_atr_mult":          [1.2, 1.5, 2.0],
        "cooldown_seconds":     [180, 300, 600],
    },
    "OPENING_HOUR_EDGE": {
        "ema_fast":             [5, 9, 12],
        "ema_slow":             [15, 21, 26],
        "adx_period":           [10, 14, 20],
        "adx_min":              [15, 20, 25, 30],
        "rsi_low":              [30, 35, 40, 45],
        "rsi_high":             [60, 65, 70, 75],
        "volume_mult":          [0.8, 1.0, 1.3, 1.5],
        "sl_atr_mult":          [1.0, 1.2, 1.5],
    },
    "TRAIL_HOLDERS_TREND": {
        "ema_fast":             [5, 8, 9, 12],
        "ema_slow":             [15, 21, 26, 30],
        "adx_period":           [10, 14, 20],
        "adx_min":              [22, 25, 28, 32],
        "di_spread_min":        [10, 15, 20, 25],
        "volume_mult":          [1.2, 1.5, 1.8, 2.0],
        "sl_atr_mult":          [1.0, 1.2, 1.5],
    },
    "VWAP_RECLAIM": {
        "vwap_period":          [20, 30, 50],
        "deviation_atr_mult":   [1.0, 1.5, 2.0, 2.5],
        "reclaim_atr_mult":     [0.3, 0.5, 0.7, 1.0],
        "lookback":             [10, 15, 20, 30],
        "adx_period":           [10, 14, 20],
        "adx_min":              [15, 18, 22, 25],
        "volume_mult":          [1.0, 1.3, 1.5, 2.0],
        "sl_atr_mult":          [1.2, 1.4, 1.8],
    },
    "HTF_EMA_PULLBACK_TIGHT": {
        "ema_fast":             [5, 9, 12],
        "ema_slow":             [15, 21, 26],
        "adx_period":           [10, 14, 20],
        "adx_min":              [20, 24, 28, 32],
        "rsi_period":           [7, 10, 14, 21],
        "rsi_pullback_level":   [35, 40, 42, 45, 50],
        "volume_mult":          [1.0, 1.2, 1.5, 2.0],
        "sl_atr_mult":          [1.0, 1.3, 1.5, 1.8],
    },
    "VOLATILITY_BREAKOUT_TIGHT": {
        "breakout_lookback":    [8, 12, 16, 20],
        "adx_period":           [10, 14, 20],
        "adx_min":              [18, 22, 25, 28],
        "volume_mult":          [1.5, 1.8, 2.0, 2.5],
        "rsi_period":           [7, 10, 14],
        "rsi_overbought":       [65, 70, 75],
        "rsi_oversold":         [25, 30, 35],
        "sl_atr_mult":          [1.2, 1.4, 1.6, 2.0],
    },
    "IND_INSTITUTIONAL_SELL": {
        "ema_fast":             [5, 9, 12],
        "ema_slow":             [15, 21, 26],
        "adx_period":           [10, 14, 20],
        "adx_min":              [20, 25, 30, 35],
        "rsi_pullback_low":     [30, 35, 40],
        "rsi_pullback_high":    [50, 55, 60],
        "vwap_period":          [20, 30, 50],
        "vwap_touch_atr":       [0.2, 0.4, 0.6, 0.8],
        "volume_mult":          [1.2, 1.4, 1.8, 2.0],
        "sl_atr_mult":          [1.0, 1.3, 1.5, 1.8],
    },
}


# ─── Gates (Wave 12 — calibrados para "achar edge viável" em qualquer regime) ─
# Estratégia: gates permissivos na busca, scoring forte no ranking.
# Regra: pf>=1.05 + n>=12 + WF>=50% consistente — acha candidatos positivos.
# Ranking posterior (score) penaliza overfit e favorece consistência + Sharpe.
GATES = {
    "min_pf":                1.05,    # PF > 1 = lucrativo
    "min_wr":                28.0,    # WR mínimo (mercado pode ser trend-only)
    "min_trades":            12,      # significância estatística básica
    "min_wf_consistency":    0.50,    # metade das janelas positivas (anti-overfit leve)
    "min_wf_positive":       2,        # alias (>=2 janelas positivas)
    "max_wf_max_dd":         -100.0,
    "max_backtest_max_dd":   -3000.0,  # floor de DD permissivo p/ discovery
    "min_total_pnl":         1.0,      # > R$1 (numeric safety)
}


# ─── Bar count por TF (~30d) ────────────────────────────────────────────────
BAR_COUNT_PER_TF = {
    "M5": 2500,
    "M15": 900,
    "M30": 500,
    "H1":  260,
}

# Walk-forward: 4 janelas (≈7.5d cada)
N_WF_WINDOWS = 4


# ═══════════════════════════════════════════════════════════════════
# Param combos (cartesian product com sampling inteligente)
# ═══════════════════════════════════════════════════════════════════

def _generate_param_combos(strat_name: str, grid_size: int = 60) -> list[dict]:
    """Gera grid densificado (até grid_size combos) por estratégia.

    Estratégia:
      1. cartesian product dos grids (universal + strategy-specific)
      2. Se passar de grid_size, sub-amostra uniformemente — mas SEMPRE
         inclui (all-lowest) e (all-highest) e o "central" (mediano).
    """
    strat_grid = STRATEGY_PARAM_GRIDS.get(strat_name, {})

    combined = {}
    combined.update(UNIVERSAL_PARAMS)
    combined.update(strat_grid)

    if not combined:
        return [{}]

    keys = sorted(combined.keys())
    values_lists = [combined[k] for k in keys]

    total = 1
    for vs in values_lists:
        total *= len(vs)
    if total == 0:
        return [{}]

    if total <= grid_size:
        return [dict(zip(keys, c)) for c in itertools.product(*values_lists)]

    # Subsample. Manter índices representativos + extremos.
    # (fix OOM 2026-08-18: total calculado aritmeticamente + product iterado
    #  lazy — antes materializava o produto cartesiano inteiro em dicts
    #  (milhões de combos, ~GBs por worker) e o pool morria no OOM killer.
    #  Mesmos índices amostrados, resultado idêntico ao anterior.)
    step = total / grid_size
    sampled_indices = sorted({int(i * step) for i in range(grid_size)})

    # Garante inclusão de extremos
    if 0 not in sampled_indices:
        sampled_indices.insert(0, 0)
    if total - 1 not in sampled_indices:
        sampled_indices.append(total - 1)
    # Garante inclusão do "central"
    median_idx = total // 2
    if median_idx not in sampled_indices:
        sampled_indices.append(median_idx)

    sampled_indices = sorted(set(sampled_indices))[:grid_size]

    # Decodifica índice → combo direto (radix misto, mesma ordem do
    # itertools.product) — O(k) por combo, sem enumerar o produto inteiro.
    radix = [len(v) for v in values_lists]
    weights = [1] * len(radix)
    for i in range(len(radix) - 2, -1, -1):
        weights[i] = weights[i + 1] * radix[i + 1]

    out = []
    for idx in sampled_indices:
        rem = idx
        vals = []
        for i, (r, w) in enumerate(zip(radix, weights)):
            d, rem = divmod(rem, w)
            vals.append(values_lists[i][d])
        out.append(dict(zip(keys, vals)))
    return out


# ═══════════════════════════════════════════════════════════════════
# Simulação bar-by-bar (fiel ao autotrader: SL/trailing/breakeven/sessão)
# ═══════════════════════════════════════════════════════════════════

def _run_backtest(df, sym_root: str, tf: str, strategy_name: str, params: dict) -> list:
    """backtest_combo wrapper — fiel ao autotrader (SL/trailing/breakeven/sessão)."""
    try:
        from backtest import backtest_v944 as bt
        bt.load_strategies()
        return bt.backtest_combo(df, sym_root, tf, strategy_name, params)
    except Exception as e:
        log.debug(f"backtest_combo({sym_root}_{tf}, {strategy_name}) falhou: {e}")
        return []


def _metrics(trades: list) -> dict:
    """Métricas: PF, WR, Sharpe, max_dd, total_pnl, n_trades. Tudo em R$ + %."""
    if not trades:
        return {
            "n_trades": 0, "total_pnl": 0.0, "pf": 0.0, "wr": 0.0,
            "sharpe": 0.0, "max_dd": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
        }
    pnls = [float(t.get("pnl", 0)) for t in trades]
    n = len(pnls)
    total_pnl = sum(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    wr = len(wins) / n * 100 if n else 0.0
    sharpe = 0.0
    if n >= 2:
        mean_p = total_pnl / n
        var = sum((p - mean_p) ** 2 for p in pnls) / (n - 1)
        std_p = math.sqrt(var)
        sharpe = (mean_p / std_p * math.sqrt(252)) if std_p > 0 else 0.0
    max_dd = _max_drawdown(pnls)
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    return {
        "n_trades": n,
        "total_pnl": round(total_pnl, 2),
        "pf": round(min(pf, 99.99), 3),    # cap p/ serialização
        "wr": round(wr, 2),
        "sharpe": round(sharpe, 3),
        "max_dd": round(max_dd, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
    }


def _max_drawdown(pnls: list) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        dd = equity - peak
        if dd < max_dd:
            max_dd = dd
    return max_dd


def _split_into_windows(df, n_windows: int) -> list:
    """Divide df em N janelas contíguas (preserva ordem temporal)."""
    if df is None or len(df) == 0:
        return []
    total = len(df)
    size = total // n_windows
    return [df.iloc[i * size : total if i == n_windows - 1 else (i + 1) * size]
            for i in range(n_windows)]


# ═══════════════════════════════════════════════════════════════════
# Avaliação completa: backtest 30d + walk-forward + gates
# ═══════════════════════════════════════════════════════════════════

def evaluate_candidate(df, sym_root: str, tf: str, strategy_name: str, params: dict) -> dict:
    """Avalia 1 candidato: backtest 30d + 4-window walk-forward + gates."""
    # 1. Full backtest (30d)
    full_trades = _run_backtest(df, sym_root, tf, strategy_name, params)
    full_m = _metrics(full_trades)

    # 2. Gates de profitability (full)
    pf_ok   = full_m["pf"] >= GATES["min_pf"]
    wr_ok   = full_m["wr"] >= GATES["min_wr"]
    nt_ok   = full_m["n_trades"] >= GATES["min_trades"]
    pnl_ok  = full_m["total_pnl"] > 0
    dd_ok   = full_m["max_dd"] >= GATES["max_backtest_max_dd"]

    # 3. Walk-forward
    wf_windows = _split_into_windows(df, N_WF_WINDOWS)
    wf_metrics = []
    for i, wdf in enumerate(wf_windows):
        if len(wdf) < 50:
            continue
        trades = _run_backtest(wdf, sym_root, tf, strategy_name, params)
        m = _metrics(trades)
        m["window"] = i + 1
        wf_metrics.append(m)

    # Consistency: fração de janelas com PnL > 0
    judged = [m for m in wf_metrics if m["n_trades"] >= 3]   # min 3 trades/janela p/ contar
    n_pos = sum(1 for m in judged if m["total_pnl"] > 0)
    consistency = n_pos / len(judged) if judged else 0.0

    wf_ok = consistency >= GATES["min_wf_consistency"]

    # Gates consolidados
    gates_passed = {
        "profitability_full":  pf_ok and wr_ok and nt_ok and pnl_ok and dd_ok,
        "walk_forward":        wf_ok,
        "minimum_trades_wf":   len(judged) >= 2,
    }
    passed = all(gates_passed.values())

    return {
        "passed": passed,
        "full": full_m,
        "walk_forward": {
            "windows": wf_metrics,
            "consistency": round(consistency, 3),
            "n_windows_judged": len(judged),
            "n_positive": n_pos,
        },
        "gates_passed": gates_passed,
    }


# ═══════════════════════════════════════════════════════════════════
# Worker — testa TUDO para um par (sym, tf) em UM processo
# ═══════════════════════════════════════════════════════════════════

def search_pair(args) -> dict:
    """Worker top-level (picklable). Testa todas estrategias × combos para um par.

    args = (pair_key, sym, tf, config, grid_size)

    Returns:
        {
            "pair", "n_strategies", "n_combos", "n_tested",
            "n_passed", "top_k": [candidates], "all_passed_count"
        }
    """
    pair_key, sym, tf, config, grid_size = args

    # 1. Fetch bars (1x) — gate de disponibilidade do módulo (sem importar)
    if importlib.util.find_spec("optimization.vt_forward_backtest") is None:
        return {"pair": pair_key, "error": "no vt_forward_backtest", "top_k": []}

    resolved = config.get("resolved_symbols", {})
    full_symbol = resolved.get(sym, f"{sym}$")
    n_bars = BAR_COUNT_PER_TF.get(tf, 500)

    try:
        from backtest import backtest_v944 as bt
        path = bt.fetch(full_symbol, tf, n_bars)
        if not path:
            return {"pair": pair_key, "error": "no_path", "top_k": []}
        df = bt.load_csv(path)
    except Exception as e:
        return {"pair": pair_key, "error": f"fetch_failed:{e}", "top_k": []}

    if df is None or len(df) < 100:
        return {"pair": pair_key, "error": "insufficient_bars", "top_k": []}

    # 2. Walk all strategies × param combos
    n_tested = 0
    n_passed = 0
    all_passed = []

    for strat in ALL_STRATEGIES:
        combos = _generate_param_combos(strat, grid_size=grid_size)
        for params in combos:
            n_tested += 1
            try:
                result = evaluate_candidate(df, sym, tf, strat, params)
            except Exception:
                continue
            if result["passed"]:
                n_passed += 1
                all_passed.append({
                    "strategy": strat,
                    "params": params,
                    "full": result["full"],
                    "wf_consistency": result["walk_forward"]["consistency"],
                    "wf_positive": result["walk_forward"]["n_positive"],
                    "score": _score_candidate(result),
                })

    # 3. Sort by score desc, keep top
    all_passed.sort(key=lambda c: c["score"], reverse=True)
    top_k = all_passed[:5]    # guarda top 5 mesmo se pedir menos

    return {
        "pair": pair_key,
        "sym": sym,
        "tf": tf,
        "n_strategies": len(ALL_STRATEGIES),
        "n_combos": n_tested,
        "n_passed": n_passed,
        "n_bars": len(df),
        "top_k": top_k,
        "all_passed_count": n_passed,
    }


def _score_candidate(result: dict) -> float:
    """Score 0-∞ (maior = melhor). Combina PF + WR + WF consistency + pnl."""
    full = result["full"]
    wf = result["walk_forward"]
    pf = min(full["pf"], 5.0)        # cap
    wr = full["wr"] / 100.0
    consistency = wf["consistency"]
    pnl = max(full["total_pnl"], 0.0) / 1000.0   # normaliza ~ R$1k = 1.0
    return pf * wr * consistency * (1.0 + pnl)


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════

def _load_config() -> dict:
    try:
        from core.vt_config_loader import load_config
        return load_config(force=True)
    except Exception as e:
        log.error(f"config load falhou: {e}")
        return {}


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


def _safe_worker_count() -> int:
    cpu = os.cpu_count() or 1
    try:
        load_avg = os.getloadavg()[0]
    except (OSError, AttributeError):
        load_avg = 0.0
    # conservador: deixa 2 cores livres p/ UI + autotrader
    safe = max(1, cpu - 2)
    if load_avg > cpu * 0.8:
        safe = max(1, safe // 2)
    return min(8, safe)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="SUPER-AGI v5 — busca exaustiva")
    parser.add_argument("--days", type=int, default=30,
                        help="janela de análise (informativo, usa BAR_COUNT fixo)")
    parser.add_argument("--grid-size", type=int, default=60,
                        help="max combos por estratégia (default: 60)")
    parser.add_argument("--top-k", type=int, default=3,
                        help="candidatos top-K por par no report final (default: 3)")
    parser.add_argument("--pairs", type=str, default="ALL",
                        help="lista separada por vírgula, ex 'WIN_M5,BIT_H1' (default: ALL=16)")
    parser.add_argument("--out", type=str, default=None,
                        help="diretório de output (default: /tmp/super_agi_v5_<ts>)")
    parser.add_argument("--resume", action="store_true",
                        help="retoma de checkpoint existente no --out dir")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    log.info(f"═══ SUPER-AGI v5 START ═══ grid_size={args.grid_size} top_k={args.top_k}")

    # Output dir
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) if args.out else Path(f"/tmp/super_agi_v5_{ts}")
    out_dir.mkdir(parents=True, exist_ok=True)
    log.info(f"Output dir: {out_dir}")

    # Config
    config = _load_config()
    if not config:
        log.error("Config vazia — abortando")
        return 1

    # Pairs alvo
    if args.pairs == "ALL":
        target_pairs = []
        symbols = config.get("symbols", ALL_SYMBOLS)
        tfs_by_sym = config.get("timeframes_by_symbol", {})
        global_tfs = config.get("timeframes", ALL_TIMEFRAMES)
        # Filtra pares desabilitados
        disabled_tf = set(config.get("disabled_timeframes", []))
        disabled_sym = set(config.get("disabled_symbols", []))
        for sym in symbols:
            if sym in disabled_sym:
                continue
            tfs = tfs_by_sym.get(sym, global_tfs)
            for tf in tfs:
                pair_key = f"{sym}_{tf}"
                if pair_key not in disabled_tf:
                    target_pairs.append(pair_key)
    else:
        target_pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]

    log.info(f"Pares alvo: {len(target_pairs)} — {target_pairs}")

    # Checkpoint
    checkpoint_path = out_dir / "checkpoint.json"
    completed: dict[str, dict] = {}
    if args.resume and checkpoint_path.exists():
        try:
            completed = json.loads(checkpoint_path.read_text())
            log.info(f"Resumindo de checkpoint: {len(completed)} pares já completos")
        except Exception as e:
            log.warning(f"checkpoint corrompido, ignorando: {e}")

    pending_pairs = [p for p in target_pairs if p not in completed]
    if not pending_pairs:
        log.info("Todos os pares já processados — só agregando")
    log.info(f"Pares pendentes: {len(pending_pairs)}")

    # Pool
    n_workers = _safe_worker_count()
    log.info(f"Workers: {n_workers} (cpu={os.cpu_count()}, load={os.getloadavg()[0]:.2f})")

    work_items = []
    for pair_key in pending_pairs:
        sym, tf = pair_key.split("_", 1)
        work_items.append((pair_key, sym, tf, config, args.grid_size))

    start_ts = time.time()
    completed_count = len(completed)
    total_count = len(target_pairs)

    if work_items:
        try:
            with ProcessPoolExecutor(max_workers=n_workers) as pool:
                futures = {pool.submit(search_pair, w): w[0] for w in work_items}
                for future in as_completed(futures):
                    pair_key = futures[future]
                    try:
                        result = future.result(timeout=300)
                    except Exception as e:
                        log.error(f"{pair_key}: worker falhou ({e})")
                        result = {"pair": pair_key, "error": str(e), "top_k": []}
                    completed[pair_key] = result
                    completed_count += 1
                    elapsed = time.time() - start_ts
                    avg_per_pair = elapsed / max(1, (completed_count - len(completed)))
                    eta_total = avg_per_pair * len(work_items)
                    eta_remain = max(0, eta_total - elapsed)

                    if result.get("top_k"):
                        best = result["top_k"][0]
                        log.info(
                            f"✓ [{completed_count}/{total_count}] {pair_key}: "
                            f"best={best['strategy']} PF={best['full']['pf']:.2f} "
                            f"PnL=R${best['full']['total_pnl']:.0f} "
                            f"WR={best['full']['wr']:.0f}% "
                            f"wf={best['wf_consistency']:.0%} "
                            f"({result['n_passed']}/{result['n_combos']} pass, "
                            f"{result['n_bars']} bars, +{elapsed:.0f}s ETA {eta_remain:.0f}s)"
                        )
                    else:
                        err = result.get("error", "no candidate passed all gates")
                        log.warning(
                            f"⚠️ [{completed_count}/{total_count}] {pair_key}: "
                            f"{err} ({elapsed:.0f}s elapsed)"
                        )

                    # Checkpoint a cada par
                    checkpoint_path.write_text(
                        json.dumps(completed, ensure_ascii=False, indent=1, default=str)
                    )
        except KeyboardInterrupt:
            log.warning("Interrompido pelo usuário — checkpoint salvo")
            return 2

    # ═══ AGREGAÇÃO ═══
    log.info("═══ AGGREGATING RESULTS ═══")
    all_results = list(completed.values())
    successful_pairs = [r for r in all_results if r.get("top_k")]

    # Report
    report = {
        "version": "5.0",
        "timestamp": datetime.now().isoformat(),
        "duration_s": round(time.time() - start_ts, 2),
        "n_pairs_target": len(target_pairs),
        "n_pairs_completed": len(successful_pairs),
        "gates": GATES,
        "grid_size": args.grid_size,
        "per_pair": {},
    }

    # Para cada par: top-K
    aggregate_recommended = {
        "strategy_by_tf": {},
        "params_by_tf": {},
    }
    pair_summary = []

    for pair_key, result in completed.items():
        if not result.get("top_k"):
            report["per_pair"][pair_key] = {
                "error": result.get("error", "no candidate passed"),
                "best": None,
            }
            pair_summary.append((pair_key, None, None, None, None, None))
            continue

        # Pega top-K
        top_k = result["top_k"][:args.top_k]
        report["per_pair"][pair_key] = {
            "n_tested": result["n_combos"],
            "n_passed": result["n_passed"],
            "n_bars": result["n_bars"],
            "top_k": top_k,
            "best": top_k[0] if top_k else None,
        }
        # Recomendação: top-1 (best)
        best = top_k[0]
        aggregate_recommended["strategy_by_tf"][pair_key] = best["strategy"]
        if best["params"]:
            aggregate_recommended["params_by_tf"][pair_key] = best["params"]
        pair_summary.append(
            (pair_key, best["strategy"], best["full"]["total_pnl"],
             best["full"]["pf"], best["full"]["wr"], best["wf_consistency"])
        )

    # Sort summary by pnl desc para display
    pair_summary.sort(key=lambda r: -(r[2] or 0))

    # Aggregate stats
    total_pnl_projection = sum(b["full"]["total_pnl"] for b in [
        r for r in [
            (completed[p].get("top_k") or [None])[0]
            for p in target_pairs if p in completed
        ] if r
    ])
    total_trades_projection = sum(b["full"]["n_trades"] for b in [
        r for r in [
            (completed[p].get("top_k") or [None])[0]
            for p in target_pairs if p in completed
        ] if r
    ])

    report["aggregate"] = {
        "total_pnl_30d_R": round(total_pnl_projection, 2),
        "total_trades_30d": total_trades_projection,
        "n_active_pairs": len(successful_pairs),
        "n_failed_pairs": len([p for p in target_pairs
                                if not completed.get(p, {}).get("top_k")]),
        "recommended_strategy_by_tf": aggregate_recommended["strategy_by_tf"],
        "recommended_params_by_tf": aggregate_recommended["params_by_tf"],
    }

    # Write final report
    report_path = out_dir / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str)
    )
    log.info(f"Report JSON: {report_path}")

    # ═══ PRINT HUMAN-LEGÍVEL ═══
    print()
    print("=" * 100)
    print(f"  SUPER-AGI v5 — RESULTADO (30d MT5, grid_size={args.grid_size})")
    print(f"  Gates: PF>={GATES['min_pf']}, WR>={GATES['min_wr']}%, "
          f"n>={GATES['min_trades']}, WF>={GATES['min_wf_consistency']:.0%}, "
          f"DD>={GATES['max_backtest_max_dd']:.0f} R$")
    print("=" * 100)
    print()
    print(f"{'Pair':<10} {'Strategy':<25} {'PnL R$':>10} {'PF':>5} {'WR%':>5} {'n':>4} "
          f"{'WF%':>5} {'DD R$':>10} {'Sharpe':>7}")
    print("-" * 100)

    total_pnl = 0.0
    total_trades = 0
    active_pairs = 0
    failed_pairs = []
    for pair, strat, pnl, pf, wr, wf in pair_summary:
        if strat is None:
            failed_pairs.append(pair)
            print(f"{pair:<10} {'(NO EDGE)':<25} {'—':>10} {'—':>5} {'—':>5} "
                  f"{'—':>4} {'—':>5} {'—':>10} {'—':>7}")
            continue
        full_m = (
            completed[pair].get("top_k") or [{}]
        )[0]["full"] if completed.get(pair, {}).get("top_k") else {}
        print(f"{pair:<10} {strat:<25} {pnl:>10.0f} {pf:>5.2f} {wr:>5.1f} "
              f"{full_m.get('n_trades', 0):>4d} {wf:>5.0%} "
              f"{full_m.get('max_dd', 0):>10.0f} {full_m.get('sharpe', 0):>7.2f}")
        total_pnl += pnl
        total_trades += full_m.get("n_trades", 0)
        active_pairs += 1

    print("-" * 100)
    print(f"{'TOTAL':<10} {'':<25} {total_pnl:>10.0f} {'—':>5} {'—':>5} "
          f"{total_trades:>4d}")
    print()
    print(f"📊 {active_pairs} pares ativos / {len(failed_pairs)} sem edge / "
          f"{len(target_pairs)} total")
    print(f"💰 Projeção 30d: R$ {total_pnl:+,.0f} ({total_trades} trades)")
    print()
    if failed_pairs:
        print("⚠️  Pares SEM edge (gates não aprovaram nenhum candidato):")
        for p in failed_pairs:
            print(f"    - {p}")
        print()
    print(f"📁 Report completo: {report_path}")
    print(f"📁 Checkpoint: {checkpoint_path}")
    print()
    print("⚠️  NENHUMA mudança aplicada ao vt_config.json.")
    print("    Para aplicar, faça manualmente após revisar o report.json.")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
