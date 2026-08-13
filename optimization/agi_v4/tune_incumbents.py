"""tune_incumbents.py — Tuning fino dos params das estratégias AGI4 INCUMBENTES.

PROBLEMA QUE RESOLVE
--------------------
O ``param_tuner.tune_strategy`` (tuning fino, ~40 combos por estratégia) só rodava
em estratégias NOVAS (Stage 4b) e vencedoras do sweep (_pending/). As estratégias
AGI4 que JÁ OPERAM (incumbentes em ``strategy_by_tf``) ficavam de fora — só
recebiam o Stage 3 (grid ralo: universais + 1-2 params). Resultado: os params das
estratégias em produção ficavam congelados no que o Stage 3 (ralo) achava, sem
tuning fino dos params próprios.

Esta fase, integrada ao pipeline (após ``_sweep_pending``, antes do Stage 6),
para cada par que opera uma AGI4:
  1. Roda ``tune_strategy`` nos params próprios da estratégia (no par em que opera).
  2. Se achar params melhores que o default, re-simula para obter as métricas.
  3. Delega ao ``stage5_apply`` (gate better_baseline_exists honesto + write + zombie).

Reuso puro de param_tuner + backtest_evaluator + stage5_apply. Nenhum gate
relaxado. Escrita sempre via stage5 (frame autorizado).

Custo: ~5 incumbentes × (~40 combos tune + 1 re-sim) ≈ 5-8min. Cabe no deadline.
"""
from __future__ import annotations

import logging

log = logging.getLogger("agi_v4.tune_incumbents")


def run(ctx: dict) -> dict:
    """Otimiza params próprios de TODAS as AGI4 incumbentes (que já operam).

    Lê ``ctx["config"]``, ``ctx["thresholds"]``, ``ctx["dry_run"]``.
    Escreve ``ctx["incumbent_tunings"]`` (lista de aplicações) para o relatório.
    Fail-safe: exceção só loga (não derruba o pipeline).

    Returns:
        dict com "incumbent_tunings" (lista) e "summary" (str).
    """
    ctx["incumbent_tunings"] = []
    try:
        from optimization.agi_v4.param_tuner import tune_strategy
        from optimization.agi_v4.backtest_evaluator import evaluate_candidate
        from optimization.agi_v4 import stage5_apply
        from optimization.agi_v4.gates import load_thresholds
        from optimization.exhaustive_strategy_search import strategy_path_by_name
    except ImportError as e:
        log.warning(f"tune_incumbents: dependências indisponíveis ({e}) — skip")
        return {"incumbent_tunings": [], "summary": f"skip (import: {e})"}

    config = ctx.get("config", {}) or {}
    thresholds = ctx.get("thresholds") or load_thresholds(config)
    sbt = config.get("strategy_by_tf", {}) or {}
    disabled = set(config.get("disabled_timeframes", []) or [])

    # Candidatos: incumbentes AGI4 que operam (não disabled) com path disponível.
    incumbents = []
    for pair, strat in sbt.items():
        if not isinstance(strat, str) or not strat.startswith("AGI4_"):
            continue
        if pair in disabled or "_" not in pair:
            continue
        path = strategy_path_by_name(strat)
        if path:
            incumbents.append((pair, strat, path))

    if not incumbents:
        return {"incumbent_tunings": [], "summary": "sem incumbentes AGI4"}

    log.info(f"── Tune incumbents: {len(incumbents)} AGI4 operando — "
             f"tuning fino dos params próprios")

    cands = []
    n_tuned = 0
    for pair, strat, path in incumbents:
        sym, tf = pair.split("_", 1)
        try:
            tuned = tune_strategy(sym, tf, strat, path, config, thresholds)
        except Exception as e:
            log.debug(f"tune_incumbents {strat}@{pair}: falhou ({e})")
            continue
        if not tuned:
            continue  # default já era melhor — mantém
        n_tuned += 1
        # Re-simula com os params otimizados para obter as métricas (full) que o
        # stage5._apply_one precisa para o gate better_baseline_exists.
        try:
            r = evaluate_candidate(sym, tf, strat, tuned, config, thresholds=thresholds)
        except Exception as e:
            log.debug(f"tune_incumbents {strat}@{pair}: re-sim falhou ({e})")
            continue
        if not r.get("passed"):
            continue
        cands.append({
            "pair": pair,
            "strategy": strat,        # incumbente — NÃO promover (sem generated)
            "params": tuned,
            "full": r.get("full", {}),
            "walk_forward": r.get("walk_forward", []),
            "gates_passed": ["ast", "profitability", "walk_forward"],
            # SEM "generated"/"pending_path": é incumbente, _maybe_promote_generated
            # retorna None (não move arquivo) — só atualiza params_by_tf.
        })

    # Delega ao stage5_apply (gate better_baseline_exists + _write_to_config + zombie).
    # Isola efeitos: _loop_exhausted=False (só atualiza params, não desativa pares).
    n_applied = 0
    if cands:
        saved_search = ctx.get("search_results")
        saved_loop = ctx.get("_loop_exhausted")
        try:
            ctx["_loop_exhausted"] = False
            ctx["search_results"] = cands
            result = stage5_apply.run(ctx)
            applied = result.get("applied_changes", []) if isinstance(result, dict) else []
            ctx["incumbent_tunings"] = applied
            n_applied = len(applied)
        except Exception as e:
            log.warning(f"tune_incumbents: stage5_apply falhou ({e})")
        finally:
            ctx["search_results"] = saved_search
            ctx["_loop_exhausted"] = saved_loop

    summary = (f"{len(incumbents)} incumbente(s) AGI4, {n_tuned} com tuning melhor "
               f"que default, {n_applied} aplicada(s) pelo gate better_baseline")
    log.info(f"── Tune incumbents concluído — {summary}")
    return {"incumbent_tunings": ctx.get("incumbent_tunings", []), "summary": summary}
