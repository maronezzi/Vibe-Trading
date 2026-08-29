"""
pair_optimizer.py — Helpers de otimização focados em encontrar o melhor
(strategy + params) por par (symbol, timeframe), com edge estatístico.

Propósito: Fornecer ao AGI duas funções de análise rápida:

  1. find_best_strategies_for_pair(symbol, tf, n_top=3)
     Roda TODAS as estratégias (28+) em um par via simulate_forward() +
     fetch_bars_for_backtest(). Retorna top N ordenadas por avg_pnl
     (não PnL total — protege contra n pequeno).

  2. optimize_pair_with_evidence(symbol, tf)
     Pega top 3 estratégias e refina cada uma com "Bayesian-like" rápido
     (random weighted search com Occam's Razor). Retorna a melhor COM
     EVIDÊNCIA (raw score + complexity penalty + n_trades).

NÃO modifica vt_config.json — só análise. Use os resultados como input
para o AGI decidir se troca de estratégia ou ajusta params.

Design notes:
  - Fetch MT5 bars UMA vez por par (cache).
  - simulate_forward() é determinístico dado (bars, strategy, params) →
    cache de resultados por (strat, params_hash) opcional.
  - Bayesian fallback: random search estratificado + Occam's Razor penalty
    (não depende de Optuna).
  - Filtro mínimo: n_trades >= 5 (evita overfit em amostras pequenas).
  - Ordenação por avg_pnl (PnL total / n) — protege contra n pequeno
    inflar o score.
"""
from __future__ import annotations

import json
import logging
import random
import sys
import time
from pathlib import Path
from typing import Optional

# Ensure project root is on path (for core/ + vt_config_loader access)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
_AGI_DIR = Path(__file__).resolve().parent
if str(_AGI_DIR) not in sys.path:
    sys.path.insert(0, str(_AGI_DIR))

from optimization.strategy_explorer import discover_strategies  # noqa: E402
from optimization.vt_forward_backtest import (  # noqa: E402
    BAR_COUNT_PER_TF,
    DEFAULT_BAR_COUNT,
    fetch_bars_for_backtest,
    simulate_forward,
)

# vt_config_loader lives in core/ — load it explicitly via importlib to avoid
# coupling this module to a specific sys.path layout (and keep tests clean).
import importlib.util as _importlib_util  # noqa: E402
_vtcl_path = _PROJECT_ROOT / "core" / "vt_config_loader.py"
if str(_PROJECT_ROOT / "core") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "core"))
if "vt_config_loader" not in sys.modules:
    try:
        import vt_config_loader  # noqa: E402
    except Exception:
        # Fallback: explicit importlib load
        _spec = _importlib_util.spec_from_file_location("vt_config_loader", _vtcl_path)
        if _spec is not None and _spec.loader is not None:
            vt_config_loader = _importlib_util.module_from_spec(_spec)
            sys.modules["vt_config_loader"] = vt_config_loader
            _spec.loader.exec_module(vt_config_loader)
        else:
            vt_config_loader = None  # type: ignore

log = logging.getLogger("pair_optimizer")
if not log.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
    log.addHandler(h)
log.setLevel(logging.INFO)


# ── Constants ─────────────────────────────────────────────────────────────
MIN_N_TRADES = 5           # filtro estatístico mínimo (anti-overfit)
TOP_N_DEFAULT = 3          # default de top N
BAYESIAN_DEFAULT_EVALS = 50  # nº de avaliações Bayesian-like (rápido)
BAYESIAN_TIMEOUT_SEC = 120

# Occam's Razor penalty: penaliza params demais (score *= (1 - lambda*k))
OCCAM_LAMBDA = 0.02  # 2% de penalidade por param otimizado

# Param search space — apenas params UNIVERSALMENTE aplicáveis
# (não inventamos indicadores — só os já usados pelo bot)
PARAM_SPACE = {
    "sl_atr_mult":       [1.0, 1.2, 1.5, 1.8, 2.0, 2.5],
    "cooldown_seconds":  [120, 300, 600, 900, 1500],
    "max_daily_trades":  [2, 3, 4, 6, 10],
}

# Strategy-specific params (opcional, só pra refinar)
STRATEGY_PARAMS = {
    "RSI_REVERSION":         ["rsi_period", "rsi_overbought", "rsi_oversold"],
    "ENHANCED_RSI_REVERSION": ["rsi_period", "rsi_overbought", "rsi_oversold"],
    "MACD_MOMENTUM":         ["macd_fast", "macd_slow", "macd_signal"],
    "ENHANCED_MACD_MOMENTUM": ["macd_fast", "macd_slow", "macd_signal"],
    "BOLLINGER":             ["bb_period", "bb_std"],
    "ENHANCED_BOLLINGER":    ["bb_period", "bb_std"],
    "EMA_CROSSOVER":         ["ema_fast", "ema_slow"],
    "EMA_PULLBACK":          ["ema_fast", "ema_slow"],
    "VWAP":                  ["vwap_buy_threshold", "vwap_sell_threshold"],
    "KELTNER_CHANNEL":       ["keltner_period", "keltner_atr_mult"],
    "STRONG_TREND":          ["adx_threshold", "adx_period"],
    "ADX_TREND":             ["adx_threshold", "adx_period"],
    "DONCHIAN_BREAKOUT":     ["donchian_period"],
    "RSI_DIVERGENCE":        ["rsi_period"],
    "DIVERGENCE_RSI":        ["rsi_period"],
}

STRATEGY_PARAM_VALUES = {
    "rsi_period":         [7, 10, 14, 21],
    "rsi_overbought":     [70, 75, 80],
    "rsi_oversold":       [20, 25, 30],
    "macd_fast":          [8, 10, 12],
    "macd_slow":          [20, 24, 26],
    "macd_signal":        [7, 9, 11],
    "bb_period":          [14, 20, 30],
    "bb_std":             [1.5, 2.0, 2.5, 3.0],
    "ema_fast":           [8, 10, 12],
    "ema_slow":           [20, 26, 30],
    "vwap_buy_threshold": [1.005, 1.010, 1.015],
    "vwap_sell_threshold":[0.985, 0.990, 0.995],
    "keltner_period":     [14, 20, 30],
    "keltner_atr_mult":   [1.5, 2.0, 2.5],
    "adx_threshold":      [15, 20, 25, 30],
    "adx_period":         [10, 14, 20],
    "donchian_period":    [14, 20, 30],
}


# ── Helpers ───────────────────────────────────────────────────────────────

def _load_config() -> dict:
    """Carrega vt_config.json via vt_config_loader (com cache invalidation)."""
    try:
        return vt_config_loader.load_config() or {}
    except Exception as e:
        log.warning(f"Falha ao carregar config: {e}")
        return {}


def _resolve_real_symbol(symbol_root: str, config: dict) -> str:
    """Resolve symbol_root para contrato real (WINQ26, etc.)."""
    resolved = config.get("resolved_symbols", {}) or {}
    if symbol_root in resolved:
        return resolved[symbol_root]
    return f"{symbol_root}$"


def _fetch_bars_cached(symbol_root: str, tf: str, config: dict) -> list:
    """Fetch MT5 bars (newest-first). Returns [] on failure."""
    full_symbol = _resolve_real_symbol(symbol_root, config)
    count = BAR_COUNT_PER_TF.get(tf, DEFAULT_BAR_COUNT)
    try:
        bars = fetch_bars_for_backtest(full_symbol, tf, count=count)
    except Exception as e:
        log.warning(f"fetch_bars_for_backtest falhou para {full_symbol}/{tf}: {e}")
        return []
    return bars or []


def _occam_penalize(raw_score: float, n_params: int) -> float:
    """Occam's Razor: penaliza complexidade."""
    if n_params <= 0:
        return raw_score
    return raw_score * max(0.0, 1.0 - OCCAM_LAMBDA * n_params)


def _avg_pnl(sim_result: dict) -> float:
    """avg_pnl = PnL total / n_trades. -inf se n=0."""
    n = sim_result.get("n_trades", 0)
    if n <= 0:
        return float("-inf")
    return sim_result.get("pnl", 0.0) / n


# ── Public API ────────────────────────────────────────────────────────────

def find_best_strategies_for_pair(
    symbol: str,
    tf: str,
    n_top: int = TOP_N_DEFAULT,
    bar_count: Optional[int] = None,
) -> list[dict]:
    """Testa TODAS as estratégias para (symbol, tf) e retorna top N.

    Critérios:
      - min n_trades >= MIN_N_TRADES (filtro estatístico)
      - decision != "no_data" / "no_trades" / "strategy_load_failed"
      - Ordenado por avg_pnl DESC (PnL/n — protege contra n pequeno)

    Returns:
        Lista de dicts com:
          {
            "strategy":      "BOLLINGER",
            "pnl":           150.0,
            "n_trades":      8,
            "wr":            62.5,
            "avg_pnl":       18.75,     # PnL / n_trades
            "max_dd":        30.0,
            "decision":      "ok",
            "params":        {"sl_atr_mult": 1.5, ...},  # default (sem otimização)
          }
        Tamanho: <= n_top. Se nenhuma passar o filtro, retorna [].
    """
    config = _load_config()
    bars = _fetch_bars_cached(symbol, tf, config)
    if not bars:
        log.warning(f"[{symbol}_{tf}] Sem bars — fetch MT5 falhou.")
        return []

    # Fetch OK: roda TODAS as 28+ estratégias com params default
    all_strats = discover_strategies()
    if not all_strats:
        log.error("discover_strategies() retornou lista vazia.")
        return []

    log.info(
        f"[{symbol}_{tf}] Testando {len(all_strats)} estratégias com {len(bars)} bars..."
    )

    # Default params (universal — não inventamos indicadores)
    default_params = {
        "sl_atr_mult":       1.5,
        "cooldown_seconds":  600,
        "max_daily_trades":  6,
    }

    results: list[dict] = []
    for strat_name in all_strats:
        try:
            sim = simulate_forward(
                symbol, tf, bars, strat_name, default_params, config=config
            )
        except Exception as e:
            log.debug(f"  {strat_name}: exceção {type(e).__name__}: {e}")
            continue

        # Filtro: precisa ter passado pelo filtro estatístico mínimo
        n = sim.get("n_trades", 0)
        if n < MIN_N_TRADES:
            continue
        decision = sim.get("decision", "")
        if decision in ("no_data", "no_trades", "strategy_load_failed",
                        "utils_load_failed", ""):
            continue

        ap = _avg_pnl(sim)
        if ap == float("-inf"):
            continue

        results.append({
            "strategy": strat_name,
            "pnl":      sim.get("pnl", 0.0),
            "n_trades": n,
            "wr":       sim.get("wr", 0.0),
            "avg_pnl":  round(ap, 4),
            "max_dd":   sim.get("max_dd", 0.0),
            "decision": decision,
            "params":   dict(default_params),  # default (não otimizado)
        })

    # Ordenar por avg_pnl DESC (PnL/n — não total — anti-overfit)
    results.sort(key=lambda r: r["avg_pnl"], reverse=True)

    top = results[:n_top]
    log.info(
        f"[{symbol}_{tf}] {len(results)}/{len(all_strats)} estratégias "
        f"passaram filtro n>={MIN_N_TRADES}. Top {len(top)} por avg_pnl."
    )
    for i, r in enumerate(top, 1):
        log.info(
            f"  #{i} {r['strategy']:30s} avg_pnl=R${r['avg_pnl']:>8.2f} "
            f"pnl=R${r['pnl']:>8.2f} n={r['n_trades']:>2d} WR={r['wr']:>5.1f}%"
        )
    return top


def _random_param_combo(strategy_name: str, rng: random.Random) -> dict:
    """Gera 1 combinação aleatória de params (universal + strategy-specific)."""
    combo = {
        "sl_atr_mult":      rng.choice(PARAM_SPACE["sl_atr_mult"]),
        "cooldown_seconds": rng.choice(PARAM_SPACE["cooldown_seconds"]),
        "max_daily_trades": rng.choice(PARAM_SPACE["max_daily_trades"]),
    }
    # Strategy-specific params (se definidos)
    sparam_keys = STRATEGY_PARAMS.get(strategy_name, [])
    for k in sparam_keys:
        vals = STRATEGY_PARAM_VALUES.get(k)
        if vals:
            combo[k] = rng.choice(vals)
    return combo


def _bayesian_refine(
    symbol: str,
    tf: str,
    strategy_name: str,
    bars: list,
    config: dict,
    max_evals: int = BAYESIAN_DEFAULT_EVALS,
    seed_params: Optional[dict] = None,
    timeout_sec: int = BAYESIAN_TIMEOUT_SEC,
) -> dict:
    """Refina params com search weighted (Bayesian-like).

    Algoritmo (sem dependência de Optuna):
      1. Avalia params iniciais (seed ou default) como trial 0.
      2. Para cada trial:
         - Sorteia 1 dim aleatória e mexe ±1 step na direção do melhor.
         - Reavalia. Se melhorar → aceito. Se piorar → aceito com
           probabilidade (annealing — aumenta exploração nos primeiros trials).
      3. Retorna o melhor (com evidence: raw_score, complexity_penalty,
         n_params, n_trials).

    Critério de fitness:
      score = avg_pnl (PnL/n)  [anti-overfit]
      score com Occam's Razor = score * (1 - lambda*n_params)

    Returns:
        {
          "strategy":       strategy_name,
          "best_params":    dict,
          "best_avg_pnl":   float,
          "best_pnl":       float,
          "best_n_trades":  int,
          "best_wr":        float,
          "best_max_dd":    float,
          "raw_score":      float,   # sem Occam
          "complexity_penalty": float,  # (1 - lambda*n_params)
          "n_trials":       int,
          "elapsed_seconds": float,
          "decision":       "ok"|"negative"|"no_trades"|"timeout",
        }
    """
    start = time.time()
    rng = random.Random(42)

    # Caching simples: chave = tuple(sorted(items)) dos params
    cache: dict[tuple, dict] = {}

    def _eval(params: dict) -> dict:
        key = tuple(sorted(params.items()))
        if key in cache:
            return cache[key]
        try:
            sim = simulate_forward(symbol, tf, bars, strategy_name, params, config=config)
        except Exception as e:
            sim = {
                "pnl": 0.0, "n_trades": 0, "wr": 0.0, "max_dd": 0.0,
                "decision": f"error:{type(e).__name__}",
            }
        cache[key] = sim
        return sim

    # Trial 0: seed (default ou passado)
    seed = seed_params or {
        "sl_atr_mult":      1.5,
        "cooldown_seconds": 600,
        "max_daily_trades": 6,
    }
    best_params = dict(seed)
    best_sim = _eval(best_params)
    best_score = _avg_pnl(best_sim)  # avg_pnl como fitness
    best_n = best_sim.get("n_trades", 0)

    n_trials = 1
    trial = 0
    while trial < max_evals:
        if time.time() - start > timeout_sec:
            break
        # Mutação: muda 1-2 dims
        new_params = dict(best_params)
        keys = list(new_params.keys())
        rng.shuffle(keys)
        n_mut = rng.choice([1, 1, 1, 2])
        for k in keys[:n_mut]:
            if k in PARAM_SPACE:
                idx = PARAM_SPACE[k].index(new_params[k]) if new_params[k] in PARAM_SPACE[k] else 0
                # Move na direção do melhor (ou aleatório)
                if best_score > 0 and rng.random() < 0.6:
                    step = rng.choice([-1, 1])
                    new_idx = max(0, min(len(PARAM_SPACE[k]) - 1, idx + step))
                else:
                    new_idx = rng.randint(0, len(PARAM_SPACE[k]) - 1)
                new_params[k] = PARAM_SPACE[k][new_idx]
            elif k in STRATEGY_PARAM_VALUES:
                vals = STRATEGY_PARAM_VALUES[k]
                new_params[k] = rng.choice(vals)

        sim = _eval(new_params)
        score = _avg_pnl(sim)

        # Aceita se score melhor OU annealing
        accept_prob = max(0.05, 0.4 * (1 - trial / max(1, max_evals)))
        if score > best_score:
            best_score = score
            best_params = new_params
            best_sim = sim
            best_n = sim.get("n_trades", 0)
        elif score != float("-inf") and rng.random() < accept_prob:
            best_score = score
            best_params = new_params
            best_sim = sim
            best_n = sim.get("n_trades", 0)

        trial += 1
        n_trials = trial + 1

    elapsed = time.time() - start
    n_params = len(best_params)
    raw_score = best_score
    adjusted = _occam_penalize(raw_score, n_params)
    pnl = best_sim.get("pnl", 0.0)
    decision = best_sim.get("decision", "ok")

    return {
        "strategy":       strategy_name,
        "best_params":    dict(best_params),
        "best_avg_pnl":   round(raw_score, 4),
        "best_pnl":       round(pnl, 2),
        "best_n_trades":  int(best_n),
        "best_wr":        round(best_sim.get("wr", 0.0), 1),
        "best_max_dd":    round(best_sim.get("max_dd", 0.0), 2),
        "raw_score":      round(raw_score, 4),
        "complexity_penalty": round(adjusted / raw_score, 4) if raw_score != 0 else 1.0,
        "n_params":       n_params,
        "n_trials":       n_trials,
        "elapsed_seconds": round(elapsed, 2),
        "decision":       decision,
    }


def optimize_pair_with_evidence(
    symbol: str,
    tf: str,
    n_top: int = TOP_N_DEFAULT,
    max_evals: int = BAYESIAN_DEFAULT_EVALS,
    timeout_sec: int = BAYESIAN_TIMEOUT_SEC,
) -> Optional[dict]:
    """Top estratégias com Bayesian optimization rápido + EVIDÊNCIA.

    Fluxo:
      1. find_best_strategies_for_pair(symbol, tf, n_top=n_top) → top N
         com edge estatístico (avg_pnl, n_trades>=5).
      2. Para cada uma das top N, roda Bayesian refine com `max_evals`.
      3. Escolhe a de melhor score COM OCCAM PENALTY (anti-complexidade).
      4. Retorna tupla completa: (strategy, params, pnl, wr, n_trades, evidence).

    Returns:
        dict com:
          {
            "symbol": "WIN",
            "tf": "M5",
            "best_strategy": "BOLLINGER",
            "best_params":   {"sl_atr_mult": 1.5, ...},
            "best_pnl":      120.0,
            "best_n_trades": 8,
            "best_wr":       62.5,
            "best_avg_pnl":  15.0,
            "raw_score":     15.0,
            "complexity_penalty": 0.94,  # (1 - lambda*n_params)
            "evidence": {
                "n_trials_per_strategy": 50,
                "top_n_evaluated": 3,
                "all_results": [...],   # lista completa
                "n_candidates_passed_filter": 5,
            },
            "decision": "ok"|"negative"|"no_data",
          }
        Ou None se nenhuma estratégia passou o filtro n_trades>=5.
    """
    config = _load_config()
    bars = _fetch_bars_cached(symbol, tf, config)
    if not bars:
        log.warning(f"[{symbol}_{tf}] Sem bars — pulando optimize_pair_with_evidence.")
        return None

    # Phase 1: top N com default params
    top = find_best_strategies_for_pair(symbol, tf, n_top=n_top)
    if not top:
        log.info(f"[{symbol}_{tf}] Nenhuma estratégia passou filtro n>={MIN_N_TRADES}.")
        return {
            "symbol":    symbol,
            "tf":        tf,
            "best_strategy": None,
            "decision":  "no_data",
            "evidence":  {"n_candidates_passed_filter": 0},
        }

    log.info(
        f"[{symbol}_{tf}] Refinando {len(top)} estratégias com Bayesian "
        f"({max_evals} evals cada, timeout={timeout_sec}s)..."
    )

    # Phase 2: Bayesian refine em cada uma
    refined = []
    for entry in top:
        strat = entry["strategy"]
        log.info(f"  → Bayesian refine: {strat}")
        result = _bayesian_refine(
            symbol, tf, strat, bars, config,
            max_evals=max_evals,
            seed_params=entry.get("params"),
            timeout_sec=timeout_sec,
        )
        refined.append(result)

    # Phase 3: pick best (score com Occam penalty — não raw)
    # Garante consistência: usa avg_pnl * complexity_penalty
    def _score(r: dict) -> float:
        if r.get("best_n_trades", 0) < MIN_N_TRADES:
            return float("-inf")
        return r.get("raw_score", 0.0) * r.get("complexity_penalty", 1.0)

    refined.sort(key=_score, reverse=True)
    winner = refined[0] if refined else None

    if not winner or winner.get("best_n_trades", 0) < MIN_N_TRADES:
        log.info(f"[{symbol}_{tf}] Nenhuma passou refine com edge estatístico.")
        return {
            "symbol":         symbol,
            "tf":             tf,
            "best_strategy":  None,
            "decision":       "no_data",
            "evidence": {
                "n_candidates_passed_filter": len(top),
                "all_results":                refined,
            },
        }

    decision = "ok" if winner["best_pnl"] > 0 else "negative"
    log.info(
        f"[{symbol}_{tf}] WINNER: {winner['strategy']} "
        f"avg_pnl=R${winner['best_avg_pnl']:.2f} pnl=R${winner['best_pnl']:.2f} "
        f"n={winner['best_n_trades']} WR={winner['best_wr']:.1f}% "
        f"params={winner['best_params']}"
    )

    return {
        "symbol":             symbol,
        "tf":                 tf,
        "best_strategy":      winner["strategy"],
        "best_params":        winner["best_params"],
        "best_pnl":           winner["best_pnl"],
        "best_n_trades":      winner["best_n_trades"],
        "best_wr":            winner["best_wr"],
        "best_avg_pnl":       winner["best_avg_pnl"],
        "best_max_dd":        winner["best_max_dd"],
        "raw_score":          winner["raw_score"],
        "complexity_penalty": winner["complexity_penalty"],
        "decision":           decision,
        "evidence": {
            "n_trials_per_strategy":      winner["n_trials"],
            "elapsed_seconds_per_strategy": winner["elapsed_seconds"],
            "top_n_evaluated":            len(refined),
            "n_candidates_passed_filter": len(top),
            "all_results":                refined,
        },
    }


# ── CLI (smoke test) ──────────────────────────────────────────────────────

def _cli() -> int:
    """Run smoke test: WIN_M5 com n_top=2 e max_evals=20 (~quick)."""
    import argparse
    p = argparse.ArgumentParser(description="Pair optimizer smoke test")
    p.add_argument("--symbol", default="WIN")
    p.add_argument("--tf", default="M5")
    p.add_argument("--n-top", type=int, default=3)
    p.add_argument("--max-evals", type=int, default=30)
    p.add_argument("--skip-evidence", action="store_true",
                   help="Só roda find_best_strategies_for_pair (sem Bayesian)")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    if args.skip_evidence:
        top = find_best_strategies_for_pair(args.symbol, args.tf, n_top=args.n_top)
        out = {"symbol": args.symbol, "tf": args.tf, "top": top}
    else:
        result = optimize_pair_with_evidence(
            args.symbol, args.tf, n_top=args.n_top, max_evals=args.max_evals
        )
        out = result

    if args.json:
        # Strip "all_results" de dentro de evidence para JSON limpo
        if out and isinstance(out.get("evidence"), dict):
            out["evidence"] = {
                k: ("[...]" if k == "all_results" else v)
                for k, v in out["evidence"].items()
            }
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    else:
        print("=" * 80)
        print(f"PAIR OPTIMIZER — {args.symbol}_{args.tf}")
        print("=" * 80)
        if out is None:
            print("Nenhum resultado.")
            return 1
        if "top" in out:
            print(f"Top {len(out['top'])} estratégias (default params):")
            for i, r in enumerate(out["top"], 1):
                print(
                    f"  #{i} {r['strategy']:30s} "
                    f"avg_pnl=R${r['avg_pnl']:>8.2f} "
                    f"pnl=R${r['pnl']:>8.2f} "
                    f"n={r['n_trades']:>2d} "
                    f"WR={r['wr']:>5.1f}%"
                )
        else:
            bs = out.get("best_strategy")
            if not bs:
                print(f"Sem estratégia com edge estatístico (n<{MIN_N_TRADES}).")
                return 0
            ev = out.get("evidence", {})
            print(f"WINNER:  {bs}")
            print(f"  params:        {out.get('best_params')}")
            print(f"  avg_pnl:       R${out.get('best_avg_pnl', 0):.2f}")
            print(f"  pnl:           R${out.get('best_pnl', 0):.2f}")
            print(f"  n_trades:      {out.get('best_n_trades', 0)}")
            print(f"  wr:            {out.get('best_wr', 0):.1f}%")
            print(f"  max_dd:        R${out.get('best_max_dd', 0):.2f}")
            print(f"  complexity:    {out.get('complexity_penalty', 1.0):.3f}")
            print(f"  decision:      {out.get('decision')}")
            print(f"  trials/strat:  {ev.get('n_trials_per_strategy', 0)}")
            print(f"  evaluated:     {ev.get('top_n_evaluated', 0)}")
            print(f"  passed filter: {ev.get('n_candidates_passed_filter', 0)}")
            # Resumo curto das outras
            others = ev.get("all_results", [])
            if others:
                print("\nOutras avaliadas:")
                for r in others:
                    print(
                        f"    {r['strategy']:30s} "
                        f"avg_pnl=R${r.get('best_avg_pnl', 0):>8.2f} "
                        f"n={r.get('best_n_trades', 0):>2d} "
                        f"WR={r.get('best_wr', 0):>5.1f}% "
                        f"trials={r.get('n_trials', 0)}"
                    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
