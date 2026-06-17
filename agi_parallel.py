#!/usr/bin/env python3
"""
AGI Parallel — Paralelização de backtests para o AGI das 17h.

Bruno 17/06: "Garanta que o agi das 17h, rode em múltiplas cpu para
rodar ele mais rápido, só tome cuidado com as chamadas a llm, pois
pode saturar ela, essa chamada tem que ser unitária e com um tempo
para a llm responder."

Arquitetura:
  - Backtests (CPU-bound): ProcessPoolExecutor com N workers
  - LLM calls (I/O-bound): sequencial com timeout de 300s
  - Grid search: paralelizado por par (cada par = 1 task)
  - Strategy swap: paralelizado por par (cada par = 1 task)

Uso:
  from agi_parallel import parallel_optimize_all_pairs, parallel_strategy_swap
"""

import copy
import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Optional

log = logging.getLogger("agi_parallel")


# ════════════════════════════════════════════════════════════════════════
# Constantes
# ════════════════════════════════════════════════════════════════════════

# Workers baseado em CPUs disponíveis
CPU_COUNT = os.cpu_count() or 4
MAX_WORKERS = min(CPU_COUNT - 1, 6)  # Deixa 1 CPU livre para o sistema
MIN_WORKERS = 2

# Timeout por backtest individual (segundos)
BACKTEST_TIMEOUT = 60

# Timeout para LLM (NUNCA paralelizar)
LLM_TIMEOUT = 300


# ════════════════════════════════════════════════════════════════════════
# Worker functions (devem ser top-level para multiprocessing)
# ════════════════════════════════════════════════════════════════════════

def _backtest_worker(args: tuple) -> dict:
    """Worker para backtest individual. Roda em processo separado.

    args = (sym, tf, strategy, config, days)
    """
    sym, tf, strategy, config, days = args
    try:
        import sys
        sys.path.insert(0, str(config.get("_project_dir", ".")))
        from vt_forward_backtest import run_mini_backtest_pair

        # Override strategy no config
        cfg = copy.deepcopy(config)
        cfg.setdefault("strategy", {})[sym] = strategy
        pair_key = f"{sym}_{tf}"
        cfg.setdefault("strategy_by_tf", {})[pair_key] = strategy

        result = run_mini_backtest_pair(sym, tf, cfg, days=days)
        return {
            "sym": sym,
            "tf": tf,
            "strategy": strategy,
            "pnl": result.get("pnl", 0),
            "n_trades": result.get("n_trades", 0),
            "wr": result.get("wr", 0),
            "decision": result.get("decision", "no_data"),
        }
    except Exception as e:
        return {
            "sym": sym,
            "tf": tf,
            "strategy": strategy,
            "pnl": 0,
            "n_trades": 0,
            "wr": 0,
            "decision": f"error:{type(e).__name__}",
        }


def _param_grid_worker(args: tuple) -> dict:
    """Worker para grid search de parâmetros. Roda em processo separado.

    args = (sym, tf, strategy, param_name, param_value, config, days)
    """
    sym, tf, strategy, param_name, param_value, config, days = args
    try:
        import sys
        sys.path.insert(0, str(config.get("_project_dir", ".")))
        from vt_forward_backtest import run_mini_backtest_pair

        cfg = copy.deepcopy(config)
        sym_lower = sym.lower()
        cfg.setdefault(sym_lower, {})[param_name] = param_value

        result = run_mini_backtest_pair(sym, tf, cfg, days=days)
        return {
            "sym": sym,
            "tf": tf,
            "strategy": strategy,
            "param_name": param_name,
            "param_value": param_value,
            "pnl": result.get("pnl", 0),
            "n_trades": result.get("n_trades", 0),
            "wr": result.get("wr", 0),
            "decision": result.get("decision", "no_data"),
        }
    except Exception as e:
        return {
            "sym": sym,
            "tf": tf,
            "strategy": strategy,
            "param_name": param_name,
            "param_value": param_value,
            "pnl": 0,
            "n_trades": 0,
            "wr": 0,
            "decision": f"error:{type(e).__name__}",
        }


# ════════════════════════════════════════════════════════════════════════
# Funções paralelas
# ════════════════════════════════════════════════════════════════════════

def get_safe_workers(requested: int = None) -> int:
    """Retorna número seguro de workers baseado em CPU + load."""
    try:
        load_avg = os.getloadavg()[0]
    except (OSError, AttributeError):
        load_avg = 0.0

    if requested:
        return max(MIN_WORKERS, min(requested, MAX_WORKERS))

    # Auto-ajuste: se load > CPUs, reduz workers
    if load_avg > CPU_COUNT * 0.8:
        return max(MIN_WORKERS, CPU_COUNT // 2)
    return MAX_WORKERS


def parallel_backtest_strategies(
    pairs: list[tuple[str, str]],
    strategies: list[str],
    config: dict,
    days: int = 7,
    max_workers: int = None,
) -> dict[str, list[dict]]:
    """Roda backtests de múltiplas estratégias para múltiplos pares em paralelo.

    Args:
        pairs: lista de (sym, tf)
        strategies: lista de estratégias a testar
        config: vt_config dict
        days: janela de backtest
        max_workers: número de workers (auto se None)

    Returns:
        dict keyed por "SYM_TF" com lista de resultados por estratégia
    """
    workers = get_safe_workers(max_workers)
    tasks = []
    for sym, tf in pairs:
        for strat in strategies:
            tasks.append((sym, tf, strat, config, days))

    log.info(f"🔄 Parallel backtest: {len(tasks)} tasks com {workers} workers")
    start = time.time()

    results = {}
    with ProcessPoolExecutor(max_workers=workers) as executor:
        future_to_task = {
            executor.submit(_backtest_worker, args): args
            for args in tasks
        }
        for future in as_completed(future_to_task, timeout=BACKTEST_TIMEOUT * len(tasks) + 60):
            args = future_to_task[future]
            sym, tf = args[0], args[1]
            pair_key = f"{sym}_{tf}"
            try:
                result = future.result(timeout=BACKTEST_TIMEOUT)
                results.setdefault(pair_key, []).append(result)
            except Exception as e:
                log.warning(f"Backtest falhou para {pair_key}: {e}")
                results.setdefault(pair_key, []).append({
                    "sym": sym, "tf": tf, "strategy": args[2],
                    "pnl": 0, "n_trades": 0, "wr": 0,
                    "decision": f"error:{type(e).__name__}",
                })

    elapsed = time.time() - start
    log.info(f"✅ Parallel backtest: {len(tasks)} tasks em {elapsed:.1f}s "
             f"({len(tasks)/elapsed:.1f} tasks/s)")
    return results


def parallel_grid_search(
    sym: str,
    tf: str,
    strategy: str,
    param_grid: dict,
    config: dict,
    days: int = 7,
    max_workers: int = None,
) -> dict:
    """Grid search paralelo de parâmetros para um par específico.

    Args:
        sym: símbolo
        tf: timeframe
        strategy: estratégia
        param_grid: dict de {param_name: [values]}
        config: vt_config dict
        days: janela de backtest
        max_workers: número de workers

    Returns:
        dict com best_params, best_pnl, delta
    """
    workers = get_safe_workers(max_workers)
    tasks = []
    for param_name, values in param_grid.items():
        for val in values:
            tasks.append((sym, tf, strategy, param_name, val, config, days))

    if not tasks:
        return {"best_params": {}, "best_pnl": 0, "default_pnl": 0, "delta": 0}

    log.info(f"🔧 Grid search {sym}_{tf} ({strategy}): {len(tasks)} variações com {workers} workers")
    start = time.time()

    best_pnl = 0
    best_params = {}
    best_n = 0
    best_wr = 0

    with ProcessPoolExecutor(max_workers=workers) as executor:
        future_to_task = {
            executor.submit(_param_grid_worker, args): args
            for args in tasks
        }
        for future in as_completed(future_to_task, timeout=BACKTEST_TIMEOUT * len(tasks) + 60):
            try:
                result = future.result(timeout=BACKTEST_TIMEOUT)
                pnl = result.get("pnl", 0)
                n = result.get("n_trades", 0)
                if pnl > best_pnl and result.get("decision") == "ok" and n > 0:
                    best_pnl = pnl
                    best_params = {result["param_name"]: result["param_value"]}
                    best_n = n
                    best_wr = result.get("wr", 0)
            except Exception as e:
                log.warning(f"Grid search task falhou: {e}")

    # Get default PnL
    try:
        from vt_forward_backtest import run_mini_backtest_pair
        default_result = run_mini_backtest_pair(sym, tf, config, days=days)
        default_pnl = default_result.get("pnl", 0)
    except Exception:
        default_pnl = 0

    elapsed = time.time() - start
    delta = best_pnl - default_pnl
    log.info(f"✅ Grid search {sym}_{tf}: {elapsed:.1f}s, "
             f"best=R${best_pnl:+.2f}, delta=R${delta:+.2f}")

    return {
        "best_params": best_params,
        "best_pnl": best_pnl,
        "default_pnl": default_pnl,
        "delta": delta,
        "best_n_trades": best_n,
        "best_wr": best_wr,
    }


def parallel_optimize_all_pairs(
    config: dict,
    param_grid: dict,
    days: int = 7,
    max_workers: int = None,
) -> list[dict]:
    """Otimiza parâmetros de TODOS os pares ativos em paralelo.

    Args:
        config: vt_config dict
        param_grid: dict de {strategy: {param: [values]}}
        days: janela de backtest
        max_workers: número de workers

    Returns:
        lista de dicts com resultados da otimização
    """
    symbols = config.get("symbols", [])
    timeframes = config.get("timeframes", [])
    disabled = set(config.get("disabled_timeframes", []))
    strategy_by_tf = config.get("strategy_by_tf", {})

    # Coleta tasks: cada (pair, strategy) → grid search
    all_tasks = []
    for sym in symbols:
        for tf in timeframes:
            pair = f"{sym}_{tf}"
            if pair in disabled:
                continue
            strategy = strategy_by_tf.get(pair, config.get("strategy", {}).get(sym))
            if not strategy or strategy not in param_grid:
                continue
            all_tasks.append((sym, tf, strategy))

    log.info(f"🔧 Otimizando {len(all_tasks)} pares em paralelo")
    start = time.time()

    results = []
    for sym, tf, strategy in all_tasks:
        pair = f"{sym}_{tf}"
        result = parallel_grid_search(
            sym, tf, strategy, param_grid[strategy], config, days, max_workers
        )
        result["pair"] = pair
        result["strategy"] = strategy
        results.append(result)

    elapsed = time.time() - start
    improved = [r for r in results if r["delta"] > 50]
    log.info(f"✅ Otimização completa: {elapsed:.1f}s, "
             f"{len(improved)}/{len(results)} pares melhorados")

    return results


def parallel_strategy_swap(
    pairs: list[str],
    config: dict,
    strategies: list[str],
    days: int = 7,
    max_workers: int = None,
) -> list[dict]:
    """Testa múltiplas estratégias para múltiplos pares em paralelo.

    Args:
        pairs: lista de "SYM_TF"
        config: vt_config dict
        strategies: lista de estratégias a testar
        days: janela de backtest
        max_workers: número de workers

    Returns:
        lista de dicts com resultados do experimento
    """
    workers = get_safe_workers(max_workers)

    # Parse pairs
    pair_tuples = []
    for pair in pairs:
        parts = pair.split("_", 1)
        if len(parts) == 2:
            pair_tuples.append((parts[0], parts[1]))

    # Roda backtests em paralelo
    results = parallel_backtest_strategies(
        pair_tuples, strategies, config, days, max_workers
    )

    # Processa resultados
    experiments = []
    for pair in pairs:
        pair_results = results.get(pair, [])
        sorted_results = sorted(pair_results, key=lambda x: x["pnl"], reverse=True)

        # Winner = melhor PnL com decisão "ok" e n_trades > 0
        winner = None
        for r in sorted_results:
            if r["decision"] == "ok" and r["n_trades"] > 0 and r["pnl"] > 0:
                winner = r
                break

        experiments.append({
            "pair": pair,
            "original_strategy": config.get("strategy_by_tf", {}).get(
                pair, config.get("strategy", {}).get(pair.split("_")[0], "?")
            ),
            "candidates": sorted_results,
            "winner": winner,
        })

    return experiments
