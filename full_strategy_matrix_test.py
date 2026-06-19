#!/usr/bin/env python3
"""
Full Strategy Matrix Test — Testa TODAS as estratégias × TODOS os símbolos × TODOS os TFs.

Gera matriz completa de resultados e identifica o melhor combo para cada par.

Uso:
    python3 full_strategy_matrix_test.py [--days 7] [--output results_matrix.json]
"""
import json
import sys
import os
import time
import logging
from pathlib import Path
from typing import Optional
from datetime import datetime

PROJECT_DIR = Path(__file__).parent
sys.path.insert(0, str(PROJECT_DIR))

from vt_forward_backtest import (
    simulate_forward, fetch_bars_cached, clear_bar_cache,
    _load_strategy_utils, _load_strategy_module, _CONTRACT_SPECS,
    BAR_COUNT_PER_TF, DEFAULT_BAR_COUNT, SIM_WARMUP_BARS, SIM_MIN_BARS
)
from agi_tuning_17h import VALID_STRATEGIES

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] MATRIX: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("matrix_test")

# ════════════════════════════════════════════════════════════════════════
# Configuração do teste
# ════════════════════════════════════════════════════════════════════════

# Todos os símbolos do Vibe-Trading
# 2026-06-19: IND e DOL removidos por decisão do Bruno (contratos cheios fora de circulação).
SYMBOLS = ["WIN", "WDO", "BIT", "WSP"]

# Todos os timeframes
TIMEFRAMES = ["M5", "M15", "M30", "H1"]

# Todas as estratégias (15 com implementação .py)
STRATEGIES = sorted(VALID_STRATEGIES)

# Dias de backtest
DEFAULT_DAYS = 7

# Output file
OUTPUT_FILE = "strategy_matrix_results.json"


def prefetchAllBars(symbols: list, timeframes: list) -> dict:
    """Pré-busca barras para todos os (symbol, tf) pairs.
    
    Returns dict: (symbol, tf) → bars list
    """
    log.info(f"📦 Pré-buscando barras para {len(symbols)} símbolos × {len(timeframes)} TFs...")
    bars_cache = {}
    total = len(symbols) * len(timeframes)
    done = 0
    
    for sym in symbols:
        for tf in timeframes:
            full_symbol = f"{sym}$"
            bar_count = BAR_COUNT_PER_TF.get(tf, DEFAULT_BAR_COUNT)
            bars = fetch_bars_cached(full_symbol, tf, count=bar_count)
            bars_cache[(sym, tf)] = bars
            done += 1
            status = f"✅ {len(bars):3d} bars" if bars else "❌ offline"
            log.info(f"  [{done:2d}/{total}] {sym}_{tf}: {status}")
    
    return bars_cache


def runMatrixTest(
    symbols: list,
    timeframes: list,
    strategies: list,
    bars_cache: dict,
    days: int = DEFAULT_DAYS
) -> list:
    """Roda a matriz completa: strategies × symbols × timeframes.
    
    Returns lista de resultados com {sym, tf, strategy, pnl, n_trades, wr, max_dd, decision}.
    """
    total = len(strategies) * len(symbols) * len(timeframes)
    log.info(f"🧪 Rodando matriz: {len(strategies)} estratégias × {len(symbols)} símb × {len(timeframes)} TFs = {total} combinações")
    
    results = []
    done = 0
    errors = 0
    start = time.time()
    
    # Pre-load strategy utils once
    utils = _load_strategy_utils()
    if utils is None:
        log.error("❌ Não conseguiu carregar strategy utils!")
        return results
    
    for strat_name in strategies:
        # Load strategy module once per strategy
        strat_mod = _load_strategy_module(strat_name)
        if strat_mod is None:
            log.warning(f"  ⚠️  {strat_name}: módulo não encontrado, pulando")
            done += len(symbols) * len(timeframes)
            continue
        
        for sym in symbols:
            for tf in timeframes:
                done += 1
                bars = bars_cache.get((sym, tf), [])
                
                if not bars or len(bars) < SIM_MIN_BARS:
                    results.append({
                        "sym": sym, "tf": tf, "strategy": strat_name,
                        "pnl": 0.0, "n_trades": 0, "wr": 0.0, "max_dd": 0.0,
                        "decision": "no_data",
                    })
                    continue
                
                # Run simulation
                try:
                    # Build params (empty for generic test)
                    params = {}
                    result = simulate_forward(sym, tf, bars, strat_name, params)
                    results.append({
                        "sym": sym, "tf": tf, "strategy": strat_name,
                        **result,
                    })
                    
                    if result["decision"] == "ok":
                        log.info(f"  [{done:3d}/{total}] {strat_name:20s} {sym}_{tf}: "
                                f"PnL=R$ {result['pnl']:+8.2f}, n={result['n_trades']:2d}, "
                                f"WR={result['wr']:.0f}%")
                    elif result["n_trades"] > 0:
                        log.info(f"  [{done:3d}/{total}] {strat_name:20s} {sym}_{tf}: "
                                f"PnL=R$ {result['pnl']:+8.2f} (negativo)")
                    
                except Exception as e:
                    errors += 1
                    results.append({
                        "sym": sym, "tf": tf, "strategy": strat_name,
                        "pnl": 0.0, "n_trades": 0, "wr": 0.0, "max_dd": 0.0,
                        "decision": f"error:{type(e).__name__}",
                        "error": str(e)[:200],
                    })
    
    elapsed = time.time() - start
    log.info(f"✅ Matriz concluída em {elapsed:.1f}s ({errors} erros)")
    return results


def analyzeResults(results: list) -> dict:
    """Analisa resultados e encontra o melhor combo para cada par.
    
    Returns dict com:
      - best_per_pair: {(sym, tf): {strategy, pnl, n_trades, wr, max_dd}}
      - global_ranking: lista de todos os resultados ordenados por PnL
      - strategy_summary: {strategy: {total_pnl, n_wins, n_pairs, avg_wr}}
      - matrix: tabela pivô (sym_tf → strategy → pnl)
    """
    # Best per pair
    best_per_pair = {}
    for r in results:
        if r["decision"] not in ("ok", "negative"):
            continue
        key = (r["sym"], r["tf"])
        if key not in best_per_pair or r["pnl"] > best_per_pair[key]["pnl"]:
            best_per_pair[key] = r
    
    # Global ranking (positive PnL only)
    positive = [r for r in results if r["pnl"] > 0 and r["decision"] == "ok"]
    global_ranking = sorted(positive, key=lambda r: r["pnl"], reverse=True)
    
    # Strategy summary
    strategy_summary = {}
    for r in results:
        strat = r["strategy"]
        if strat not in strategy_summary:
            strategy_summary[strat] = {
                "total_pnl": 0.0, "n_wins": 0, "n_pairs": 0,
                "pairs_with_trades": 0, "avg_wr": 0.0, "wr_sum": 0.0,
            }
        s = strategy_summary[strat]
        s["total_pnl"] += r["pnl"]
        s["n_pairs"] += 1
        if r["pnl"] > 0 and r["decision"] == "ok":
            s["n_wins"] += 1
        if r["n_trades"] > 0:
            s["pairs_with_trades"] += 1
            s["wr_sum"] += r["wr"]
    
    # Calculate average WR per strategy
    for strat, s in strategy_summary.items():
        if s["pairs_with_trades"] > 0:
            s["avg_wr"] = round(s["wr_sum"] / s["pairs_with_trades"], 1)
        del s["wr_sum"]
    
    # Matrix (pivot table)
    matrix = {}
    for r in results:
        pair_key = f"{r['sym']}_{r['tf']}"
        if pair_key not in matrix:
            matrix[pair_key] = {}
        matrix[pair_key][r["strategy"]] = {
            "pnl": r["pnl"],
            "n_trades": r["n_trades"],
            "wr": r["wr"],
            "decision": r["decision"],
        }
    
    return {
        "best_per_pair": best_per_pair,
        "global_ranking": global_ranking[:50],  # top 50
        "strategy_summary": strategy_summary,
        "matrix": matrix,
    }


def printReport(analysis: dict):
    """Imprime relatório formatado no terminal."""
    print("\n" + "=" * 100)
    print("📊 FULL STRATEGY MATRIX TEST — RESULTADOS")
    print("=" * 100)
    
    # 1. Best per pair
    print("\n🏆 MELHOR ESTRATÉGIA POR PAR:")
    print(f"{'PAR':<12} {'ESTRATÉGIA':<20} {'PnL':>10} {'TRades':>7} {'WR':>6} {'MaxDD':>10}")
    print("-" * 70)
    
    best_per_pair = analysis["best_per_pair"]
    for (sym, tf), r in sorted(best_per_pair.items()):
        pair_key = f"{sym}_{tf}"
        print(f"{pair_key:<12} {r['strategy']:<20} R$ {r['pnl']:+8.2f} {r['n_trades']:>5d} {r['wr']:>5.0f}% R$ {r['max_dd']:>8.2f}")
    
    # 2. Strategy ranking
    print("\n📈 RANKING DE ESTRATÉGIAS (PnL total across all pairs):")
    print(f"{'#':<4} {'ESTRATÉGIA':<20} {'PnL Total':>12} {'Wins':>6} {'Pairs':>7} {'Avg WR':>8}")
    print("-" * 65)
    
    summary = analysis["strategy_summary"]
    ranked = sorted(summary.items(), key=lambda x: x[1]["total_pnl"], reverse=True)
    for i, (strat, s) in enumerate(ranked, 1):
        emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "  "
        print(f"{emoji}{i:<2} {strat:<20} R$ {s['total_pnl']:+10.2f} {s['n_wins']:>4d} {s['n_pairs']:>5d} {s['avg_wr']:>7.1f}%")
    
    # 3. Top 20 individual combos
    print("\n🔥 TOP 20 COMBINAÇÕES (Strategy × Pair):")
    print(f"{'#':<4} {'PAR':<12} {'ESTRATÉGIA':<20} {'PnL':>10} {'Trades':>7} {'WR':>6}")
    print("-" * 65)
    
    for i, r in enumerate(analysis["global_ranking"][:20], 1):
        pair_key = f"{r['sym']}_{r['tf']}"
        print(f"{i:<4} {pair_key:<12} {r['strategy']:<20} R$ {r['pnl']:+8.2f} {r['n_trades']:>5d} {r['wr']:>5.0f}%")
    
    # 4. Pairs with NO positive strategy
    print("\n⚠️  PARES SEM ESTRATÉGIA POSITIVA:")
    all_pairs = {(s, t) for s in SYMBOLS for t in TIMEFRAMES}
    no_positive = all_pairs - set(best_per_pair.keys())
    if no_positive:
        for sym, tf in sorted(no_positive):
            print(f"  {sym}_{tf}")
    else:
        print("  (todos os pares têm pelo menos 1 estratégia positiva!)")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Full Strategy Matrix Test")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="Backtest days")
    parser.add_argument("--output", type=str, default=OUTPUT_FILE, help="Output JSON file")
    args = parser.parse_args()
    
    log.info(f"🚀 Iniciando teste massivo: {len(STRATEGIES)} estratégias × {len(SYMBOLS)} símb × {len(TIMEFRAMES)} TFs")
    log.info(f"   Total: {len(STRATEGIES) * len(SYMBOLS) * len(TIMEFRAMES)} combinações")
    log.info(f"   Estratégias: {', '.join(STRATEGIES)}")
    
    # 1. Pre-fetch bars
    clear_bar_cache()
    bars_cache = prefetchAllBars(SYMBOLS, TIMEFRAMES)
    
    # Count how many have data
    with_data = sum(1 for b in bars_cache.values() if b and len(b) >= SIM_MIN_BARS)
    log.info(f"📊 {with_data}/{len(bars_cache)} pares com dados suficientes")
    
    # 2. Run matrix test
    results = runMatrixTest(SYMBOLS, TIMEFRAMES, STRATEGIES, bars_cache, days=args.days)
    
    # 3. Analyze
    analysis = analyzeResults(results)
    
    # 4. Print report
    printReport(analysis)
    
    # 5. Save to JSON
    output_path = os.path.join(PROJECT_DIR, args.output)
    
    # Convert tuples to strings for JSON
    best_json = {}
    for (sym, tf), r in analysis["best_per_pair"].items():
        best_json[f"{sym}_{tf}"] = r
    
    output_data = {
        "timestamp": datetime.now().isoformat(),
        "config": {
            "symbols": SYMBOLS,
            "timeframes": TIMEFRAMES,
            "strategies": STRATEGIES,
            "days": args.days,
        },
        "total_combinations": len(results),
        "best_per_pair": best_json,
        "strategy_summary": analysis["strategy_summary"],
        "global_ranking_top50": analysis["global_ranking"],
        "full_matrix": analysis["matrix"],
    }
    
    with open(output_path, "w") as f:
        json.dump(output_data, f, indent=2, default=str)
    
    log.info(f"💾 Resultados salvos em: {output_path}")
    
    # 6. Generate config recommendations
    print("\n" + "=" * 100)
    print("🔧 RECOMENDAÇÕES DE CONFIG (vt_config.json):")
    print("=" * 100)
    print()
    
    for (sym, tf), r in sorted(analysis["best_per_pair"].items()):
        pair_key = f"{sym}_{tf}"
        if r["pnl"] > 0 and r["n_trades"] >= 3:
            print(f'  strategy_by_tf["{pair_key}"] = "{r["strategy"]}"  # PnL=R$ {r["pnl"]:+.2f}, WR={r["wr"]:.0f}%, n={r["n_trades"]}')
    
    return output_data


if __name__ == "__main__":
    main()
