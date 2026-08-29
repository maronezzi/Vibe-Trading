"""
stage3_exhaustive.py — Busca exaustiva por SIMULAÇÃO bar-by-bar.

CORREÇÃO FUNDAMENTAL (2026-07-04): cada candidato é avaliado por simulação
bar-by-bar sobre 30 dias de barras reais do MT5 + walk-forward por janelas.
NUNCA usa trades passados do DB como referência — esses trades pertenciam à
estratégia antiga e não dizem nada sobre um candidato novo.

Para cada par perdedor (do stage1):
  1. Itera as 30 estratégias existentes × combos de params (grid)
  2. Cada combinação é SIMULADA bar-by-bar em 30d do MT5 (contrato vigente)
     via backtest_evaluator.evaluate_candidate (fiel ao autotrader:
     SL/trailing/breakeven/sessão)
  3. Walk-forward: 30d divididos em 4 janelas, exige consistência
  4. O melhor candidato aprovado de cada par vai para ctx["search_results"]

Paralelismo: ProcessPoolExecutor sobre (sym, tf). Cada worker busca uma
vez as 30d de barras e testa todas as estratégias localmente (evita
fetch redundante).
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ProcessPoolExecutor, as_completed

from .gates import load_thresholds

log = logging.getLogger("agi_v4.stage3")

# Reusa o conhecimento dos grids (não duplica catálogo de estratégias/params)
from optimization.exhaustive_strategy_search import (
    ALL_STRATEGIES,
    _generate_param_combos,
)


def run(ctx: dict) -> dict:
    """Executa busca exaustiva por simulação para os pares perdedores.

    Args:
        ctx: contexto do pipeline. Usa:
            - ctx["failing_pairs"]: pares alvo (do stage1)
            - ctx["config"]: config (resolved_symbols, params_by_tf, thresholds)
            - ctx["thresholds"]: gates thresholds

    Returns:
        dict com "candidates" (aprovados por simulação + walk-forward).
    """
    config = ctx.get("config", {}) or {}
    thresholds = ctx.get("thresholds") or load_thresholds(config)

    target_pairs = _normalize_failing_pairs(ctx.get("failing_pairs", []))
    if not target_pairs:
        target_pairs = _all_pairs_from_config(config)
    if not target_pairs:
        return {"candidates": [], "summary": "sem pares alvo"}

    log.info(f"Busca exaustiva por simulação: {len(target_pairs)} par(es) "
             f"× {len(ALL_STRATEGIES)} estratégias")

    num_workers = _safe_worker_count()
    work_items = [
        (pair, pair.split("_", 1)[0], pair.split("_", 1)[1] if "_" in pair else "", config, thresholds)
        for pair in target_pairs
    ]

    candidates = []
    total_tested = 0

    try:
        with ProcessPoolExecutor(max_workers=num_workers) as pool:
            futures = {pool.submit(_search_pair_worker, item): item[0] for item in work_items}
            tried_map = ctx.setdefault("_tried_strategies", {})
            for future in as_completed(futures):
                pair_key = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    log.error(f"Worker {pair_key} falhou: {e}")
                    continue
                total_tested += result.get("n_tested", 0)
                # Registra estratégias já testadas para o stage4 (evita repetir)
                tried_map[pair_key] = result.get("tried_all", [])
                best = result.get("best_candidate")
                if best:
                    candidates.append(best)
                    log.info(f"✓ {pair_key}: melhor {best['strategy']} "
                             f"PF={best['full']['pf']:.2f} PnL=R${best['full']['total_pnl']:.2f}")
    except Exception as e:
        log.error(f"ProcessPoolExecutor falhou: {e}", exc_info=True)
        return {"candidates": [], "summary": f"pool error: {e}"}

    candidates.sort(key=lambda c: c["full"]["total_pnl"], reverse=True)
    ctx["search_results"] = candidates

    summary = (f"{total_tested} combinações simuladas em 30d MT5, "
               f"{len(candidates)} par(es) com candidato aprovado (full + walk-forward)")
    return {"candidates": candidates, "summary": summary}


# ═══════════════════════════════════════════════════════════════════
# Worker — busca o melhor candidato para UM par via simulação
# ═══════════════════════════════════════════════════════════════════

def _search_pair_worker(args) -> dict:
    """Top-level worker (picklable). Testa todas as estratégias de um par.

    Busca 30d de barras UMA VEZ, depois simula cada (estratégia × params)
    sobre essas mesmas barras — comparação justa (mesmo mercado).

    FIX 2026-07-26 (loop infinito — Bruno): max_attempts por par + dedup.
    Antes: 43 estratégias × ~80 combos × 6 pares = ~20k backtests sem teto.
    Agora: MAX_ATTEMPTS_PER_PAIR=600 com early-stop + cache de combos já
    testados (evita repetir a mesma combinação dezenas de vezes).
    Wave Stage3-justo (01/08): consecutive_rejects reseta POR estratégia, e
    MAX_ATTEMPTS subiu 300→600 (~14 combos/estratégia × 43). Cada estratégia
    recebe varredura justa — antes uma estratégia ruim saturava o contador e
    as outras 42 testavam só 1 combo cada (pares "travados").

    Args: (pair, sym, tf, config, thresholds)
    Returns: {"pair", "best_candidate"|"None", "n_tested"}
    """
    pair, sym, tf, config, thresholds = args

    # Import tardio: o evaluator importa backtest_v944 que importa pandas etc.
    from optimization.agi_v4.backtest_evaluator import evaluate_candidate

    MAX_ATTEMPTS_PER_PAIR = int(os.environ.get("VT_AGI_MAX_ATTEMPTS", "600"))
    MAX_CONSECUTIVE_REJECTS = 50  # Qwen Code: 50 rejeições seguidas = espaço esgotado

    n_tested = 0
    best = None
    best_pnl = -float("inf")
    _seen_combos: set[str] = set()  # dedup: evita testar a mesma combo 2x

    for strat in ALL_STRATEGIES:
        if n_tested >= MAX_ATTEMPTS_PER_PAIR:
            log.info(f"  {pair}: MAX_ATTEMPTS={MAX_ATTEMPTS_PER_PAIR} atingido "
                     f"({n_tested} testados, melhor PnL={best_pnl:.2f})")
            break
        # Wave Stage3-justo (Bruno 01/08): consecutive_rejects reseta POR
        # ESTRATÉGIA, não globalmente. Antes o contador vivia fora do loop —
        # uma estratégia ruim (sempre ADX_TREND, primeiro alfabético) saturava
        # o contador em 50 e as outras 42 estratégias testavam só 1 combo cada
        # (break imediato). Resultado: pares "travados" na baseline sem nunca
        # ter chance real de testar alternativas. Agora cada estratégia recebe
        # uma varredura justa independentemente das anteriores.
        consecutive_rejects = 0
        for params in _generate_param_combos(strat):
            if n_tested >= MAX_ATTEMPTS_PER_PAIR:
                break

            # Dedup: hash da combo (strategy + params ordenados)
            combo_key = f"{strat}|{sorted(params.items())}"
            if combo_key in _seen_combos:
                continue
            _seen_combos.add(combo_key)

            n_tested += 1
            try:
                result = evaluate_candidate(sym, tf, strat, params, config,
                                            thresholds=thresholds)
            except Exception as e:
                log.debug(f"  {pair} {strat} params={params}: erro {e}")
                consecutive_rejects += 1
                if consecutive_rejects >= MAX_CONSECUTIVE_REJECTS:
                    log.info(f"  {pair} {strat}: {MAX_CONSECUTIVE_REJECTS} rejeições "
                             f"seguidas — estratégia sem edge, pulando próx.")
                    break
                continue

            if not result["passed"]:
                consecutive_rejects += 1
                if consecutive_rejects >= MAX_CONSECUTIVE_REJECTS:
                    log.info(f"  {pair} {strat}: {MAX_CONSECUTIVE_REJECTS} rejeições "
                             f"seguidas — estratégia sem edge, pulando próx.")
                    break
                continue

            consecutive_rejects = 0  # reset: achou um aprovado
            pnl = result["full"]["total_pnl"]
            if pnl > best_pnl:
                best_pnl = pnl
                best = {
                    "pair": pair,
                    "strategy": strat,
                    "params": params,
                    "full": result["full"],
                    "walk_forward": result["walk_forward"],
                    "gates_passed": ["profitability_full", "walk_forward"],
                }

    # Lista de estratégias testadas que NÃO passaram (para o stage4 saber
    # o que já falhou e o LLM não repetir as mesmas ideias).
    return {"pair": pair, "best_candidate": best, "n_tested": n_tested,
            "tried_all": list(ALL_STRATEGIES)}


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def _normalize_failing_pairs(failing) -> list[str]:
    """Normaliza failing_pairs (list[str] ou list[dict]) para list[str]."""
    pairs = []
    for f in failing:
        if isinstance(f, str):
            pairs.append(f)
        elif isinstance(f, dict):
            pairs.append(f.get("pair", ""))
    return [p for p in pairs if p]


def _all_pairs_from_config(config: dict) -> list[str]:
    symbols = config.get("symbols", [])
    tfs_by_sym = config.get("timeframes_by_symbol", {})
    global_tfs = config.get("timeframes", [])
    pairs = []
    for sym in symbols:
        for tf in tfs_by_sym.get(sym, global_tfs):
            pairs.append(f"{sym}_{tf}")
    return pairs


def _safe_worker_count() -> int:
    """Determine safe worker count, respecting VT_MAX_WORKERS env cap.

    During market hours the autotrader + MT5 + watchdog need CPU headroom.
    Set VT_MAX_WORKERS=2 (or any int) to hard-cap the pool regardless of
    the auto-detection logic. Default cap: 2 workers (safe for i5-10210U
    with live trading running).
    """
    try:
        import os as _os
        from optimization.vt_forward_backtest import _get_safe_max_workers
        cpu = _os.cpu_count() or 1
        try:
            load_avg = _os.getloadavg()[0]
        except (OSError, AttributeError):
            load_avg = 0.0
        # Hard cap via env — defaults to 2 to protect live trading
        env_cap = int(_os.environ.get("VT_MAX_WORKERS", "2"))
        configured = min(env_cap, cpu)
        return _get_safe_max_workers(configured, cpu, load_avg)
    except (ImportError, TypeError, ValueError):
        return min(2, os.cpu_count() or 1)
