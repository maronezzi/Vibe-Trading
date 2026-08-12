"""sweep_pending.py — Varredura automática do strategies/_pending/ (Wave AGI-sweep).

PROBLEMA QUE RESOLVE
--------------------
O AGI gera estratégias (Stage 4) e as põe em ``strategies/_pending/``, mas ninguém
as processava — acumularam 174+. O ``scripts/sweep_pending_strategies.py`` é
manual E estava quebrado (chamava save_full_config de um frame não autorizado).
Resultado: o AGI não "testava todas as estratégias em todos os índices/TFs".

Esta fase, integrada ao pipeline (após _optimize_profitable_pairs, antes do
Stage 6), faz o AGI, todo cron:

  1. Varre ``strategies/_pending/*.py``.
  2. Filtra com ``smoke_check`` (ast + runtime).
  3. Testa cada sobrevivente em TODOS os pares ativos via ``cross_evaluate``.
  4. Best-per-pair (cada par fica com a melhor; min-advantage R$20).
  5. Otimiza params próprios do vencedor via ``param_tuner.tune_strategy``.
  6. Promove e aplica via ``stage5_apply`` (frame autorizado em ALLOWED_WRITERS).

Reuso puro de cross_pair_evaluator + param_tuner + stage5_apply. Nenhum gate
relaxado. Escrita sempre via stage5_apply (nunca save_full_config direto).

Custo: ~30-90min numa run típica (smoke rejeita 30-50%, cache de barras MT5).
Cabe no deadline de 8h do conservative.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

log = logging.getLogger("agi_v4.sweep_pending")

# Diretório de estratégias geradas (sandbox do Stage 4).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PENDING_DIR = _PROJECT_ROOT / "strategies" / "_pending"

# Vantagem mínima (R$) para promover sobre o incumbente — evita trocas marginais
# que não justificam mudar uma estratégia estável. Igual ao sweep manual.
_MIN_ADVANTAGE = float(__import__("os").environ.get("VT_AGI_SWEEP_MIN_ADV", "20"))

# Budget de tempo (min) do sweep. O sweep roda APÓS o loop de convergência (sem
# deadline check do pipeline), então tem seu próprio guard para não ultrapassar
# a madrugada e atrapalhar o pre-flight das 8:55. Default 90min cobre ~167 estr.
# × 12 pares com cache de barras; configurável via env.
_BUDGET_SECS = int(__import__("os").environ.get("VT_AGI_SWEEP_BUDGET_MINS", "90")) * 60


def run(ctx: dict) -> dict:
    """Varre _pending/, testa em todos os pares ativos, otimiza e promove.

    Lê ``ctx["config"]``, ``ctx["thresholds"]``, ``ctx["dry_run"]``.
    Escreve ``ctx["sweep_promotions"]`` (lista de promoções aplicadas) para o
    relatório Stage 6. Fail-safe: exceção só loga (não derruba o pipeline).

    Returns:
        dict com "sweep_promotions" (lista) e "summary" (str).
    """
    ctx["sweep_promotions"] = []
    if not PENDING_DIR.exists():
        return {"sweep_promotions": [], "summary": "_pending/ não existe"}

    try:
        from optimization.agi_v4.cross_pair_evaluator import (
            cross_evaluate, active_pairs, smoke_check,
        )
        from optimization.agi_v4.param_tuner import tune_strategy
        from optimization.agi_v4 import stage5_apply
        from optimization.agi_v4.gates import load_thresholds
    except ImportError as e:
        log.warning(f"sweep _pending/: dependências indisponíveis ({e}) — skip")
        return {"sweep_promotions": [], "summary": f"skip (import: {e})"}

    config = ctx.get("config", {}) or {}
    thresholds = ctx.get("thresholds") or load_thresholds(config)
    dry_run = ctx.get("dry_run", True)
    pairs = active_pairs(config)
    if not pairs:
        return {"sweep_promotions": [], "summary": "sem pares ativos"}

    # ── 1. Enumerar + 2. smoke filter ──
    files = sorted(f for f in PENDING_DIR.glob("*.py") if f.name != "__init__.py")
    if not files:
        return {"sweep_promotions": [], "summary": "_pending/ vazio"}

    survivors = []
    rejected_smoke = 0
    for f in files:
        if smoke_check(f):
            survivors.append(f)
        else:
            rejected_smoke += 1
    log.info(f"── Sweep _pending/: {len(files)} arquivo(s), {len(survivors)} "
             f"passaram smoke, {rejected_smoke} rejeitada(s) — testando em "
             f"{len(pairs)} par(es) ativos")

    # ── 3. cross-evaluate + 4. best-per-pair (com min-advantage) ──
    best_per_pair: dict[str, dict] = {}
    n_winners = 0
    sweep_t0 = time.time()
    budget_hit = False
    for i, f in enumerate(survivors, 1):
        # Guard de tempo: para antes de ultrapassar o budget (não atrapalha o
        # pre-flight da madrugada). As restantes ficam para a próxima run.
        if time.time() - sweep_t0 > _BUDGET_SECS:
            log.warning(f"sweep _pending/: budget de {_BUDGET_SECS//60}min atingido "
                        f"após {i-1}/{len(survivors)} — restantes na próxima run")
            budget_hit = True
            break
        name = _extract_strategy_name(f)
        if not name:
            continue
        try:
            winner = cross_evaluate(name, f, pairs, config, thresholds)
        except Exception as e:
            log.debug(f"sweep {f.name}: cross_evaluate falhou ({e})")
            continue
        if winner is None:
            continue
        n_winners += 1
        pair = winner["pair"]
        pnl = winner.get("full", {}).get("total_pnl", 0)
        # Best-per-pair: só fica o de maior total_pnl (com min-advantage sobre
        # o incumbente atual do par, checado abaixo via melhor_existente).
        cur = best_per_pair.get(pair)
        if cur is None or pnl > cur.get("full", {}).get("total_pnl", 0) + _MIN_ADVANTAGE:
            best_per_pair[pair] = winner
        if i % 20 == 0:
            elapsed = (time.time() - sweep_t0) / 60
            log.info(f"sweep progresso: {i}/{len(survivors)} testada(s), "
                     f"{n_winners} com winner, {elapsed:.1f}min")

    if not best_per_pair:
        log.info(f"sweep _pending/: 0 promoções ({len(survivors)} testada(s), "
                 f"nenhuma superou incumbente em algum par)")
        return {"sweep_promotions": [],
                "summary": f"{len(survivors)} testada(s), 0 promoção(ões)"}

    # ── 5. tune params próprios + montar candidatos no formato _apply_one ──
    cands = []
    for pair, w in best_per_pair.items():
        sym, tf = (pair.split("_", 1) + [""])[:2]
        tuned = None
        if not dry_run and sym and tf:
            try:
                tuned = tune_strategy(sym, tf, w["strategy"],
                                      w.get("pending_path", ""), config, thresholds)
            except Exception as e:
                log.debug(f"sweep tune {w['strategy']}: falhou ({e})")
        cands.append({
            "pair": pair,
            "strategy": w["strategy"],
            "params": tuned or {},
            "full": w.get("full", {}),
            "walk_forward": w.get("walk_forward", []),
            "gates_passed": ["ast", "profitability", "walk_forward"],
            "generated": True,  # dispara _maybe_promote_generated no stage5
            "pending_path": w.get("pending_path", ""),
        })

    # ── 6. delegar ao stage5_apply (frame autorizado) ──
    # Isola efeitos colaterais: salva/restaura search_results; _loop_exhausted=False
    # para o stage5 NÃO desativar pares (o sweep só promove). O stage5 fará o gate
    # honesto better_baseline_exists + _maybe_promote + _write_to_config + zombie.
    n_applied = 0
    if cands:
        saved_search = ctx.get("search_results")
        saved_loop = ctx.get("_loop_exhausted")
        try:
            ctx["_loop_exhausted"] = False
            ctx["search_results"] = cands
            result = stage5_apply.run(ctx)
            applied = result.get("applied_changes", []) if isinstance(result, dict) else []
            ctx["sweep_promotions"] = applied
            n_applied = len(applied)
        except Exception as e:
            log.warning(f"sweep _pending/: stage5_apply falhou ({e}) — "
                        f"promoções não aplicadas")
        finally:
            ctx["search_results"] = saved_search
            ctx["_loop_exhausted"] = saved_loop

    summary = (f"{len(survivors)} testada(s) em {len(pairs)} par(es), "
               f"{len(best_per_pair)} vencedora(s), {n_applied} promovida(s)"
               + (" [BUDGET atingido — restantes na próxima run]" if budget_hit else ""))
    log.info(f"── Sweep _pending/ concluído — {summary}")
    return {"sweep_promotions": ctx.get("sweep_promotions", []), "summary": summary}


def _extract_strategy_name(path: Path) -> str | None:
    """Lê STRATEGY_NAME do .py (regex — mesma lógica de _discover_all_strategies)."""
    try:
        text = path.read_text(encoding="utf-8")
        m = re.search(r'^STRATEGY_NAME\s*=\s*["\'](.+?)["\']', text, re.MULTILINE)
        return m.group(1) if m else None
    except Exception:
        return None
