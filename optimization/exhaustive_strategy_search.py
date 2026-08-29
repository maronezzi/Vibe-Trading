#!/usr/bin/env python3
"""
Exhaustive strategy search with parameter optimization:
test ALL 16 pairs × ALL 28 strategies × top param combos per strategy.
For each pair, find the strategy+params that gives positive PnL over 7 days.
Apply winning config. Disable pairs that can't be profitable.

v2: Adds parameter optimization via strategic grid search (~20-30 combos/strategy).
"""
import itertools
import json
import sys
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from optimization.vt_forward_backtest import (
    fetch_bars_for_backtest, simulate_forward, BAR_COUNT_PER_TF, DEFAULT_BAR_COUNT
)

# ALL_STRATEGIES agora é auto-descoberto de strategies/ (sem hardcode).
# Wave 13 (Bruno 2026-07-12): antes era uma lista hardcoded de 27 nomes
# que excluía silenciosamente qualquer plugin novo (e ficava desatualizada
# sempre que alguém adicionava strategy). Agora espelha core/vt_strategy_loader
# e super_agi_v5 — adicionou uma estratégia nova? Ela aparece em todos os
# pontos de enumeração na próxima execução.
import re as _re_exhaust
from pathlib import Path as _Path_exhaust


def _discover_all_strategies() -> list:
    found: list = []
    seen: set = set()
    strategies_dir = _Path_exhaust(__file__).resolve().parent.parent / "strategies"
    if not strategies_dir.exists():
        return found
    for py in sorted(strategies_dir.glob("*.py")):
        if py.name == "__init__.py":
            continue
        try:
            t = py.read_text(encoding="utf-8")
            m = _re_exhaust.search(r"^STRATEGY_NAME\s*=\s*[\"\'](.+?)[\"\']", t, _re_exhaust.MULTILINE)
            if m:
                name = m.group(1)
                if name not in seen:
                    seen.add(name)
                    found.append(name)
        except Exception:
            continue
    return found


ALL_STRATEGIES = _discover_all_strategies()


# Wave AGI-param-tuning (Bruno 12/08): mapeamento name -> path absoluto do .py.
# Populado uma vez no import (module-level). Reusa o mesmo glob de
# _discover_all_strategies. Usado por param_tuner (zombie keep-set) e pelo
# bootstrap de grids AGI4 (Stage 3 existentes).
def _discover_name_to_path() -> dict:
    out: dict = {}
    strategies_dir = _Path_exhaust(__file__).resolve().parent.parent / "strategies"
    if not strategies_dir.exists():
        return out
    for py in sorted(strategies_dir.glob("*.py")):
        if py.name == "__init__.py":
            continue
        try:
            t = py.read_text(encoding="utf-8")
            m = _re_exhaust.search(r"^STRATEGY_NAME\s*=\s*[\"\'](.+?)[\"\']", t, _re_exhaust.MULTILINE)
            if m and m.group(1) not in out:
                out[m.group(1)] = str(py)
        except Exception:
            continue
    return out


_STRATEGY_PATHS = _discover_name_to_path()


def strategy_path_by_name(name: str) -> str | None:
    """Retorna o path absoluto do .py de uma estratégia pelo STRATEGY_NAME."""
    return _STRATEGY_PATHS.get(name)


ALL_SYMBOLS = ["WIN", "BIT", "WSP", "WDO"]
ALL_TIMEFRAMES = ["M5", "M15", "M30", "H1"]


# ─── Parameter Grid for Optimization ─────────────────────────────────────────
# Strategic sampling: 3-5 values per param, chosen from PARAM_BOUNDS ranges.
# Each strategy maps to its key parameters. Total combos per strategy: ~10-30.

# Universal params (applied by simulate_forward for all strategies).
# Wave Melhoria 3 (Bruno 13/07): grids expandidos após dry-run mostrar que
# params atuais já são ótimos do grid antigo (v3.0). Adicionados valores
# intermediários e extremos leves para refinar a busca sem explodir combos.
UNIVERSAL_PARAMS = {
    "sl_atr_mult":       [0.8, 1.0, 1.2, 1.5, 1.8, 2.0, 2.5, 3.0],
    "cooldown_seconds":  [60, 120, 180, 300, 450, 600, 900],
    # Wave Melhoria 1 (Bruno 12/07): circuit breaker AGI-tunable.
    # max_consecutive_losses: após N losses seguidas no slot, pausa.
    # halt_duration_minutes: tempo de pausa. Defaults 999/60 = efetivamente off.
    "max_consecutive_losses":   [2, 3, 4, 5, 6, 8],
    "halt_duration_minutes":    [15, 30, 45, 60, 90, 120, 180],
    # Wave Melhoria 2 (Bruno 12/07): profit-lock por R (AGI-tunable).
    # Quando lucro atinge profit_lock_r × risco inicial, move SL pro entry.
    # 0.0 = desligado. O AGI testa se travar cedo (0.3) ou tarde (1.0) é melhor.
    # Wave 13.5: adicionado 0.0 (off) e 1.5, 2.0 (trailing mais largo).
    "profit_lock_r":            [0.0, 0.3, 0.5, 0.7, 1.0, 1.5, 2.0],
}

# Strategy-specific param grids
# Wave Melhoria 3 (Bruno 13/07): grids expandidos. Cada estratégia agora testa
# mais valores intermediários em vez de só 3-4 pontilhados. SUPERTREND
# corrigido para usar seus params reais (atr_period, multiplier) ao invés
# dos adx_* herdados. Estratégias que não tinham grid (HTF_EMA_PULLBACK_TIGHT,
# IND_INSTITUTIONAL_SELL, TRAIL_HOLDERS_TREND, VOLATILITY_*, VWAP_RECLAIM)
# ganharam grids baseados nos params.get() reais.
STRATEGY_PARAM_GRIDS = {
    "RSI_REVERSION": {
        "rsi_period":      [5, 7, 10, 14, 17, 21],
        "rsi_overbought":  [65, 70, 75, 80, 85],
        "rsi_oversold":    [15, 20, 25, 30, 35],
    },
    "ENHANCED_RSI_REVERSION": {
        "rsi_period":      [5, 7, 10, 14, 17, 21],
        "rsi_overbought":  [65, 70, 75, 80, 85],
        "rsi_oversold":    [15, 20, 25, 30, 35],
    },
    "MACD_MOMENTUM": {
        "macd_fast":       [6, 8, 10, 12, 14],
        "macd_slow":       [18, 20, 22, 24, 26, 28, 30],
        "macd_signal":     [7, 8, 9, 10, 11, 12, 13],
    },
    "ENHANCED_MACD_MOMENTUM": {
        "macd_fast":       [6, 8, 10, 12, 14],
        "macd_slow":       [18, 20, 22, 24, 26, 28, 30],
        "macd_signal":     [7, 8, 9, 10, 11, 12, 13],
    },
    "BOLLINGER": {
        "bb_period":       [10, 14, 18, 20, 25, 30],
        "bb_std":          [1.0, 1.5, 1.8, 2.0, 2.5, 3.0, 3.5],
    },
    "ENHANCED_BOLLINGER": {
        "bb_period":       [10, 14, 18, 20, 25, 30],
        "bb_std":          [1.0, 1.5, 1.8, 2.0, 2.5, 3.0, 3.5],
    },
    "STRONG_TREND": {
        "adx_threshold":   [12, 15, 18, 20, 25, 30, 35],
        "adx_period":      [7, 10, 14, 18, 20],
    },
    "ADX_TREND": {
        "adx_threshold":   [12, 15, 18, 20, 25, 30, 35],
        "adx_period":      [7, 10, 14, 18, 20],
    },
    "EMA_CROSSOVER": {
        "ema_fast":        [5, 8, 10, 12, 15],
        "ema_slow":        [15, 20, 26, 30, 35],
    },
    "EMA_PULLBACK": {
        "ema_fast":        [5, 8, 10, 12, 15],
        "ema_slow":        [15, 20, 26, 30, 35],
        "pullback_pct":    [0.10, 0.15, 0.20, 0.25, 0.30],
    },
    "TRIPLE_EMA": {
        "ema_fast":        [5, 8, 10, 12],
        "ema_mid":         [13, 15, 18, 21],
        "ema_slow":        [20, 26, 30, 35],
    },
    "VWAP": {
        "vwap_period":         [15, 20, 25, 30, 40, 50],
        "vwap_buy_threshold":  [1.002, 1.005, 1.008, 1.010, 1.015, 1.020],
        "vwap_sell_threshold": [0.980, 0.985, 0.988, 0.990, 0.995, 0.998],
    },
    "KELTNER_CHANNEL": {
        "keltner_period":     [10, 14, 18, 20, 25, 30],
        "keltner_atr_mult":   [1.0, 1.5, 1.8, 2.0, 2.5, 3.0],
    },
    "DONCHIAN_BREAKOUT": {
        "period":             [10, 14, 18, 20, 25, 30],
        "exit_period":        [5, 8, 10, 14],
    },
    "PIVOT_POINTS": {
        "pivot_timeframe":    ["H1", "H4", "D1"],
    },
    "DIVERGENCE_RSI": {
        "rsi_period":      [7, 10, 14, 17, 21],
        "rsi_overbought":  [65, 70, 75, 80, 85],
        "rsi_oversold":    [15, 20, 25, 30, 35],
    },
    "MOMENTUM_BREAKOUT": {
        "adx_threshold":   [12, 15, 18, 20, 25, 30],
        "adx_period":      [7, 10, 14, 18, 20],
    },
    "MEAN_REVERSION_ZSCORE": {
        "bb_period":       [10, 14, 20, 30],
        "bb_std":          [1.0, 1.5, 2.0, 2.5, 3.0],
    },
    "FIBONACCI_RETRACEMENT": {
        "pullback_pct":    [0.05, 0.08, 0.10, 0.15, 0.20, 0.25, 0.30],
    },
    "VOLATILITY_BREAKOUT": {
        "adx_threshold":   [12, 15, 18, 20, 25, 30],
        "adx_period":      [7, 10, 14, 18, 20],
    },
    "RANGE_TRADING": {
        "rsi_overbought":  [65, 70, 75, 80, 85],
        "rsi_oversold":    [15, 20, 25, 30, 35],
        "rsi_period":      [7, 10, 14, 17, 21],
    },
    "WIN_REVERSION": {
        "rsi_period":      [7, 10, 14, 17, 21],
        "rsi_overbought":  [65, 70, 75, 80, 85],
        "rsi_oversold":    [15, 20, 25, 30, 35],
    },
    "HEIKIN_ASHI": {
        "ema_fast":        [5, 8, 10, 12],
        "ema_slow":        [15, 20, 26, 30],
    },
    "SMART_EMA": {
        "ema_fast":        [5, 8, 10, 12],
        "ema_slow":        [15, 20, 26, 30],
    },
    # Wave 13.5: SUPERTREND usa atr_period/multiplier reais (não adx_*).
    "SUPERTREND": {
        "atr_period":      [7, 10, 14, 18, 20],
        "multiplier":      [1.5, 2.0, 2.5, 3.0, 3.5],
    },
    "ICHIMOKU": {
        "tenkan_period":   [7, 9, 12],
        "kijun_period":    [22, 26, 30],
        "senkou_period":   [44, 52, 60],
    },
    "CANDLE_PATTERNS": {
        "body_ratio":      [0.4, 0.5, 0.6, 0.7],
        "wick_ratio":      [0.3, 0.5, 0.7],
    },
    # Wave 13.5: estratégias que não tinham grid mas usam params reais.
    "HTF_EMA_PULLBACK_TIGHT": {
        "adx_min":         [10, 15, 20, 25],
        "rsi_pullback_level": [35, 40, 45, 50],
        "volume_mult":     [1.0, 1.2, 1.5, 2.0],
        "ema_fast":        [5, 8, 10],
        "ema_slow":        [20, 26, 30],
    },
    "IND_INSTITUTIONAL_SELL": {
        "adx_min":         [15, 20, 25, 30],
        "rsi_pullback_high": [55, 60, 65, 70],
        "rsi_pullback_low":  [25, 30, 35, 40],
        "vwap_touch_atr":  [0.3, 0.5, 0.8, 1.0],
        "volume_mult":     [1.2, 1.5, 2.0, 2.5],
    },
    "TRAIL_HOLDERS_TREND": {
        "adx_min":         [10, 15, 20, 25, 30],
        "di_spread_min":   [5, 10, 15, 20],
        "volume_mult":     [1.0, 1.3, 1.5, 2.0],
        "ema_fast":        [5, 8, 10, 12],
        "ema_slow":        [20, 26, 30],
    },
    "VOLATILITY_BREAKOUT_TIGHT": {
        "adx_min":         [15, 20, 25, 30],
        "breakout_lookback": [10, 15, 20, 30],
        "rsi_overbought":  [65, 70, 75, 80],
        "rsi_oversold":    [20, 25, 30, 35],
        "volume_mult":     [1.0, 1.3, 1.5, 2.0],
    },
    "VOLATILITY_MEAN_REVERSION": {
        "adx_max":         [15, 20, 25, 30],
        "atr_ratio_max":   [0.8, 1.0, 1.2, 1.5],
        "max_ema_distance_pct": [0.3, 0.5, 0.8, 1.0],
        "rsi_overbought":  [65, 70, 75, 80],
        "rsi_oversold":    [20, 25, 30, 35],
    },
    "VOLATILITY_REGIME_TREND": {
        "adx_min":         [15, 20, 25, 30],
        "atr_ratio_min":   [1.0, 1.3, 1.5, 1.8],
        "ema_fast":        [5, 8, 10],
        "ema_slow":        [20, 26, 30],
        "volume_mult":     [1.0, 1.3, 1.5, 2.0],
    },
    "VWAP_RECLAIM": {
        "adx_min":         [15, 20, 25, 30],
        "deviation_atr_mult": [1.0, 1.5, 2.0, 2.5],
        "reclaim_atr_mult": [0.3, 0.5, 0.8, 1.0],
        "vwap_period":     [15, 20, 30, 40],
        "volume_mult":     [1.0, 1.3, 1.5, 2.0],
    },
    "OPENING_HOUR_EDGE": {
        "adx_min":         [15, 20, 25, 30],
        "rsi_high":        [60, 65, 70, 75],
        "rsi_low":         [25, 30, 35, 40],
        "volume_mult":     [1.0, 1.3, 1.5, 2.0],
        "ema_fast":        [5, 8, 10],
        "ema_slow":        [20, 26],
    },
}

# Max combos to test per strategy (cap to avoid explosion).
# Wave 13.5 (Bruno 13/07): 30 → 80. Com grids expandidos (~3x valores por
# dim), 30 era subsampling agressivo demais que descartava bons candidatos.
# 80 ainda cabe em ~33min para 16 pares × 43 estratégias no AGI v4.
MAX_COMBOS_PER_STRATEGY = 80


def _bootstrap_agi4_grids() -> None:
    """Popula STRATEGY_PARAM_GRIDS dinamicamente para AGI4_* promovidas.

    Wave AGI-param-tuning (Bruno 12/08): AGI4_* não têm entrada estática em
    STRATEGY_PARAM_GRIDS, então o Stage 3 só testa seus universais (sl_atr_mult
    × cooldown = 56 combos) — nunca os params próprios. Este bootstrap extrai os
    top-2 params próprios de cada AGI4 (via param_tuner) e os injeta no grid.

    LIMITAÇÃO deliberada: top-2 params × 1 valor (extremo oposto ao default) por
    estratégia. Com os 56 universais: 56 × 2 = 112 combos → cap de 80 do
    _generate_param_combos subamostra preservando extremos. Tuning fino fica no
    Stage 4b (estratégias novas) e no sweep manual. Não toca em
    _generate_param_combos nem nos caps — só popula o dict mutável existente.

    Roda module-level no import, então é visível em todos os processos
    (incl. workers do ProcessPoolExecutor do Stage 3).
    """
    try:
        from optimization.agi_v4.param_tuner import extract_tunable_params
    except ImportError:
        return  # param_tuner indisponível (ex: teste isolado) — sem bootstrap
    for name, path in _STRATEGY_PATHS.items():
        if not name.startswith("AGI4_") or name in STRATEGY_PARAM_GRIDS:
            continue  # só AGI4 sem entrada estática
        try:
            tunables = extract_tunable_params(path)
        except Exception:
            continue
        if not tunables:
            continue
        # Top-2 params próprios × extremo oposto ao default (1 valor por param).
        grid = {}
        for p, t in list(tunables.items())[:2]:
            kind, d, lo, hi = t["kind"], t["default"], t["lo"], t["hi"]
            # Extremo oposto: se default mais perto de lo, usa hi; vice-versa.
            far = hi if abs(d - lo) >= abs(d - hi) else lo
            if kind == "int":
                grid[p] = [int(round(far))]
            else:
                grid[p] = [round(float(far), 6)]
        if grid:
            STRATEGY_PARAM_GRIDS[name] = grid


_bootstrap_agi4_grids()


def _test_pair_worker(args):
    """Top-level worker function for multiprocessing (must be picklable).

    Args: (pair_key, sym, tf, bars, config)
    Returns: (pair_key, results_list)
    """
    pair_key, sym, tf, bars, config = args
    if not bars:
        return (pair_key, [])
    results = test_all_strategies_for_pair(sym, tf, bars, config)
    return (pair_key, results)


def _generate_param_combos(strat_name: str) -> list:
    """Generate param combos for a strategy: universal + strategy-specific.
    Returns list of dicts. Caps at MAX_COMBOS_PER_STRATEGY.
    """
    # Start with strategy-specific grid (or empty if none defined)
    strat_grid = STRATEGY_PARAM_GRIDS.get(strat_name, {})

    # Build combined grid: universal params + strategy-specific
    # FIX 2026-07-26 (Qwen Code + Bruno): backtest_v944 NÃO simula
    # max_consecutive_losses, halt_duration_minutes, profit_lock_r.
    # Essas 3 dims geravam 6×7×7=294 combos funcionalmente idênticos por
    # estratégia (mesmo PF/dd repetido dezenas de vezes). Só inclui as
    # dims que o backtest realmente usa: sl_atr_mult e cooldown_seconds.
    combined = {}
    _BACKTEST_ACTIVE_UNIVERSAL = {
        k: v for k, v in UNIVERSAL_PARAMS.items()
        if k in ("sl_atr_mult", "cooldown_seconds")
    }
    combined.update(_BACKTEST_ACTIVE_UNIVERSAL)
    combined.update(strat_grid)

    if not combined:
        return [{}]  # just defaults

    # Generate cartesian product
    keys = sorted(combined.keys())
    values_lists = [combined[k] for k in keys]
    all_combos = []
    for combo in itertools.product(*values_lists):
        all_combos.append(dict(zip(keys, combo)))

    # If too many, subsample evenly
    if len(all_combos) > MAX_COMBOS_PER_STRATEGY:
        step = len(all_combos) / MAX_COMBOS_PER_STRATEGY
        sampled = [all_combos[int(i * step)] for i in range(MAX_COMBOS_PER_STRATEGY)]
        # Always include the first combo (all-lowest) and last (all-highest)
        if all_combos[0] not in sampled:
            sampled.insert(0, all_combos[0])
        if all_combos[-1] not in sampled:
            sampled.append(all_combos[-1])
        all_combos = sampled[:MAX_COMBOS_PER_STRATEGY]

    return all_combos


def merge_params_by_tf_into_config(config):
    """Create a modified config where params_by_tf values are injected
    into config[sym.lower()][tf] so _resolve_pair_params picks them up.
    """
    pbt = config.get("params_by_tf", {})
    modified = json.loads(json.dumps(config))  # deep copy
    for pair_key, params in pbt.items():
        parts = pair_key.split("_", 1)
        if len(parts) == 2:
            sym, tf = parts
            sym_lower = sym.lower()
            if sym_lower not in modified:
                modified[sym_lower] = {}
            if tf not in modified[sym_lower]:
                modified[sym_lower][tf] = {}
            # Merge params_by_tf into the tf-specific section
            # (params_by_tf takes priority)
            for k, v in params.items():
                modified[sym_lower][tf][k] = v
    return modified


def test_strategy_with_optimization(sym, tf, bars, strat_name, config):
    """Test a single strategy with param optimization.

    Tests multiple param combinations (grid search) and returns the best result.

    Returns: (strategy_name, best_result_dict, best_params_dict)
    """
    param_combos = _generate_param_combos(strat_name)

    best_result = None
    best_params = {}
    best_pnl = -float("inf")

    for params in param_combos:
        try:
            result = simulate_forward(sym, tf, bars, strat_name, params, config=config)
        except Exception as e:
            result = {
                "pnl": 0.0, "n_trades": 0, "wr": 0.0, "max_dd": 0.0,
                "decision": f"error:{type(e).__name__}",
            }
        if result["pnl"] > best_pnl:
            best_pnl = result["pnl"]
            best_result = result
            best_params = params

    return strat_name, best_result, best_params


def test_all_strategies_for_pair(sym, tf, bars, config):
    """Test all 28 strategies for a single (sym, tf) pair.
    For each strategy, tests multiple param combinations via grid search.

    Returns sorted list of (strategy_name, result_dict, best_params) by PnL descending.
    """
    results = []
    for strat_name in ALL_STRATEGIES:
        strat_name, result, best_params = test_strategy_with_optimization(
            sym, tf, bars, strat_name, config
        )
        results.append((strat_name, result, best_params))

    # Sort by PnL descending
    results.sort(key=lambda x: x[1]["pnl"], reverse=True)
    return results


def _format_params(params: dict) -> str:
    """Format params dict for display: 'key=val key=val ...'"""
    if not params:
        return "defaults"
    return " ".join(f"{k}={v}" for k, v in sorted(params.items()))


def main():
    start_time = time.time()

    # Load config
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vt_config.json")
    with open(config_path) as f:
        config = json.load(f)

    # Merge params_by_tf into config for accurate testing
    test_config = merge_params_by_tf_into_config(config)

    # Count total param combos
    total_combos = 0
    for strat in ALL_STRATEGIES:
        total_combos += len(_generate_param_combos(strat))

    print("=" * 100)
    print("EXHAUSTIVE STRATEGY SEARCH WITH PARAMETER OPTIMIZATION")
    print(f"16 pairs × {len(ALL_STRATEGIES)} strategies × ~avg {total_combos // len(ALL_STRATEGIES)} param combos")
    print(f"Total simulations per pair: ~{total_combos}")
    print("=" * 100)

    # Phase 1: Fetch bars for all 16 pairs (one Wine call per pair)
    # FIX 2026-06-26: usar resolved_symbols (contratos REAIS como WINQ26)
    # em vez de f"{sym}$" (sintético/forward sem slippage real B3).
    # Antes: WIN$ (feed sintético) → AGI sugeria params baseados nele →
    # autotrader aplicava em WINQ26 (real) → 76% SL_SERVIDOR.
    print("\n📡 Phase 1: Fetching MT5 bars for all 16 pairs...")
    bars_cache = {}
    resolved = config.get("resolved_symbols", {})
    for sym in ALL_SYMBOLS:
        for tf in ALL_TIMEFRAMES:
            pair_key = f"{sym}_{tf}"
            # Resolve para o contrato real vigente (mesma fonte que o autotrader)
            full_symbol = resolved.get(sym, f"{sym}$")
            bar_count = BAR_COUNT_PER_TF.get(tf, DEFAULT_BAR_COUNT)
            print(f"  Fetching {pair_key} ({full_symbol}, {tf}, {bar_count} bars)...", end=" ", flush=True)
            bars = fetch_bars_for_backtest(full_symbol, tf, count=bar_count)
            bars_cache[pair_key] = bars
            print(f"{'✅ ' + str(len(bars)) + ' bars' if bars else '❌ NO DATA'}")

    # Phase 2: Test all strategies with param optimization for each pair (PARALLEL)
    # Cap workers to protect live trading (VT_MAX_WORKERS env, default 2)
    num_workers = min(int(os.environ.get("VT_MAX_WORKERS", "2")), os.cpu_count() or 1)
    print(f"\n🔬 Phase 2: Testing all {len(ALL_STRATEGIES)} strategies with param optimization per pair...")
    print(f"  Using {num_workers} parallel workers across {num_workers} CPU cores")

    # Prepare work items for all pairs
    work_items = []
    for sym in ALL_SYMBOLS:
        for tf in ALL_TIMEFRAMES:
            pair_key = f"{sym}_{tf}"
            bars = bars_cache[pair_key]
            work_items.append((pair_key, sym, tf, bars, test_config))

    # Execute in parallel using ProcessPoolExecutor
    all_results = {}
    completed = 0
    total_pairs = len(work_items)
    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        futures = {executor.submit(_test_pair_worker, item): item[0] for item in work_items}
        for future in as_completed(futures):
            pair_key = futures[future]
            completed += 1
            try:
                _, results = future.result()
                all_results[pair_key] = results
            except Exception as e:
                print(f"\n  [{completed}/{total_pairs}] {pair_key}: ❌ Error: {e}")
                all_results[pair_key] = []
                continue

            if not results:
                print(f"\n  [{completed}/{total_pairs}] {pair_key}: ❌ No bars available, skipped")
                continue

            print(f"\n  [{completed}/{total_pairs}] {pair_key}: ✅ tested {len(ALL_STRATEGIES)} strategies (done)")
            # Show top 3
            for i, (strat, res, params) in enumerate(results[:3]):
                emoji = "🟢" if res["pnl"] > 0 else "🔴"
                pstr = _format_params(params)
                print(f"    #{i+1}: {strat:30s} PnL={res['pnl']:>10.2f}R  trades={res['n_trades']:>3d}  WR={res['wr']:>5.1f}%  {emoji}")
                print(f"          params: {pstr}")

    # Phase 3: Select best strategy per pair
    print("\n" + "=" * 100)
    print("📊 Phase 3: BEST STRATEGY + PARAMS PER PAIR")
    print("=" * 100)

    best_config = {}  # {pair_key: {strategy, pnl, n_trades, wr, decision, params}}
    disabled_pairs = []

    print(f"\n{'Pair':<12} {'Best Strategy':<30} {'PnL':>10} {'Trades':>7} {'WR':>7} {'Status':<10}")
    print("-" * 80)

    for sym in ALL_SYMBOLS:
        for tf in ALL_TIMEFRAMES:
            pair_key = f"{sym}_{tf}"
            results = all_results.get(pair_key, [])
            if not results:
                print(f"{pair_key:<12} {'(no bars)':<30} {'N/A':>10} {'N/A':>7} {'N/A':>7} DISABLED")
                disabled_pairs.append((pair_key, "No MT5 data available"))
                continue

            # Find best strategy with PnL > 0 AND trades >= 1
            best_strat = None
            best_result = None
            best_params = {}
            for strat, res, params in results:
                if res["pnl"] > 0 and res["n_trades"] >= 1:
                    best_strat = strat
                    best_result = res
                    best_params = params
                    break

            if best_strat:
                emoji = "✅"
                print(f"{pair_key:<12} {best_strat:<30} {best_result['pnl']:>10.2f} {best_result['n_trades']:>7d} {best_result['wr']:>6.1f}% {emoji}")
                pstr = _format_params(best_params)
                print(f"{'':12} params: {pstr}")
                best_config[pair_key] = {
                    "strategy": best_strat,
                    "pnl": best_result["pnl"],
                    "n_trades": best_result["n_trades"],
                    "wr": best_result["wr"],
                    "max_dd": best_result["max_dd"],
                    "params": best_params,
                }
            else:
                # No profitable strategy found
                # Show the least-bad one
                least_bad_strat = results[0][0]  # already sorted by PnL desc
                least_bad = results[0][1]
                results[0][2]
                emoji = "❌"
                print(f"{pair_key:<12} {least_bad_strat:<30} {least_bad['pnl']:>10.2f} {least_bad['n_trades']:>7d} {least_bad['wr']:>6.1f}% {emoji}")
                disabled_pairs.append((pair_key, f"No profitable strategy (best: {least_bad_strat} PnL={least_bad['pnl']:.2f}R)"))

    # Summary
    active_count = len(best_config)
    disabled_count = len(disabled_pairs)
    total_pnl = sum(v["pnl"] for v in best_config.values())
    total_trades = sum(v["n_trades"] for v in best_config.values())

    print(f"\n{'=' * 80}")
    print(f"SUMMARY: {active_count} active / {disabled_count} disabled / 16 total")
    print(f"Total PnL: {total_pnl:.2f}R")
    print(f"Total trades: {total_trades}")

    if disabled_pairs:
        print("\nDisabled pairs:")
        for pair_key, reason in disabled_pairs:
            print(f"  {pair_key}: {reason}")

    # Phase 4: Apply winning config
    print(f"\n{'=' * 80}")
    print("🔧 Phase 4: Applying winning config...")

    # Update strategy_by_tf
    strategy_by_tf = config.get("strategy_by_tf", {})
    for pair_key, info in best_config.items():
        strategy_by_tf[pair_key] = info["strategy"]
    config["strategy_by_tf"] = strategy_by_tf

    # Update params_by_tf with optimized params
    params_by_tf = config.get("params_by_tf", {})
    for pair_key, info in best_config.items():
        if info.get("params"):
            if pair_key not in params_by_tf:
                params_by_tf[pair_key] = {}
            params_by_tf[pair_key].update(info["params"])
    config["params_by_tf"] = params_by_tf

    # Update disabled_timeframes
    config["disabled_timeframes"] = [pair_key for pair_key, _ in disabled_pairs]

    # Save
    config["_version"] = config.get("_version", 0) + 1
    config["_updated_at"] = __import__("datetime").datetime.now().isoformat()
    config["_updated_by"] = "exhaustive_strategy_search_v2"

    # Build notes
    notes_parts = []
    for pair_key, info in best_config.items():
        pstr = _format_params(info.get("params", {}))
        notes_parts.append(f"{pair_key}→{info['strategy']}({info['pnl']:.0f}R/{info['n_trades']}t/{info['wr']:.0f}%WR)[{pstr}]")
    if disabled_pairs:
        notes_parts.append(f"DISABLED: {', '.join(p for p, _ in disabled_pairs)}")
    config["_notes"] = f"Exhaustive 16×{len(ALL_STRATEGIES)} strategy+param search. Active: {active_count}, Disabled: {disabled_count}, Total PnL: {total_pnl:.0f}R. " + "; ".join(notes_parts)

    with open(config_path, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    print(f"✅ Config saved as v{config['_version']}")

    # Print full results table
    print(f"\n{'=' * 100}")
    print("📋 FULL RESULTS TABLE (Top 5 strategies per pair)")
    print("=" * 100)
    for sym in ALL_SYMBOLS:
        for tf in ALL_TIMEFRAMES:
            pair_key = f"{sym}_{tf}"
            results = all_results.get(pair_key, [])
            if not results:
                continue
            print(f"\n  {pair_key}:")
            for i, (strat, res, params) in enumerate(results[:5]):
                marker = " ← SELECTED" if pair_key in best_config and best_config[pair_key]["strategy"] == strat else ""
                emoji = "🟢" if res["pnl"] > 0 and res["n_trades"] >= 1 else "🔴"
                pstr = _format_params(params)
                print(f"    {i+1}. {strat:<30s} PnL={res['pnl']:>10.2f}R  trades={res['n_trades']:>3d}  WR={res['wr']:>5.1f}%  DD={res['max_dd']:>8.2f}R  {emoji}{marker}")
                print(f"       params: {pstr}")

    elapsed = time.time() - start_time
    print(f"\n⏱️  Total time: {elapsed:.0f}s ({elapsed/60:.1f}min)")
    print(f"✅ DONE: {active_count}/16 pairs active, total PnL = {total_pnl:.2f}R")


if __name__ == "__main__":
    main()
