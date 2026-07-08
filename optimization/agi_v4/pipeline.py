"""
pipeline.py — Orquestra os 6 estágios da AGI v4.

Cada stage é uma função run(ctx: dict) -> dict que:
  - Lê do ctx (config, db path, modo dry-run, etc.)
  - Retorna um dict de resultados que entra de volta no ctx
  - Não levanta exceção em falha (loga + retorna erro estruturado)

O pipeline roda os stages em sequência (1→6) e no final retorna um relatório
consolidado. O loop de convergência (Lei 5) envolve re-rodar stage 3+5 com
config atualizado até nenhum par ter PnL ≤ 0.

Contrato do ctx (contexto compartilhado):
  ctx = {
      "config": dict,              # vt_config.json carregado
      "days": int,                 # janela de análise (default 7)
      "dry_run": bool,             # não aplica mudanças
      "max_iterations": int,       # loop de convergência (default 3)
      "audit": list,               # append de eventos para forensics
      "performance": dict,         # populado por stage1
      "regime": dict,              # populado por stage1
      "hypotheses": list,          # populado por stage2 (web+llm)
      "search_results": list,      # populado por stage3
      "generated_strategies": list,# populado por stage4
      "applied_changes": list,     # populado por stage5
      "converged": bool,           # populado pelo loop
  }
"""

from __future__ import annotations

import importlib
import logging
import time
from datetime import datetime
from typing import Any

from .gates import load_thresholds

log = logging.getLogger("agi_v4.pipeline")

# Tag para auditoria — correlaciona com a versão do __init__
TAG = "W871"


def run(days: int = 7,
        dry_run: bool = True,
        max_iterations: int = 1000,
        config: dict | None = None) -> dict:
    """Executa o pipeline completo da AGI v4.

    Args:
        days: janela de análise em dias (PnL real do DB).
        dry_run: se True, não aplica mudanças no vt_config.json.
        max_iterations: TETO DE SEGURANÇA anti-loop-infinito (default 1000).
            O loop para por CONVERGÊNCIA (todo par positivo) ou por
            ESTAGNAÇÃO (uma iteração inteira sem melhorar nenhum par). O
            teto existe só pra garantir que um bug nunca prenda o cron.
        config: config pré-carregado (se None, carrega via vt_config_loader).

    Returns:
        ctx final com todos os resultados + audit trail.
    """
    start_ts = time.time()
    log.info(f"[{TAG}] AGI v4 pipeline iniciado — days={days} dry_run={dry_run} "
             f"(loop sem limite até convergir ou estagnar)")

    # Carregar config se não fornecido
    if config is None:
        try:
            from core.vt_config_loader import load_config
            config = load_config(force=True)
        except Exception as e:
            log.error(f"[{TAG}] falha ao carregar config: {e}")
            config = {}

    # Contexto inicial
    ctx: dict[str, Any] = {
        "tag": TAG,
        "started_at": datetime.now().isoformat(),
        "days": days,
        "dry_run": dry_run,
        "max_iterations": max_iterations,
        "config": config,
        "thresholds": load_thresholds(config),
        "audit": [],
        "performance": {},
        "regime": {},
        "hypotheses": [],
        "search_results": [],
        "generated_strategies": [],
        "applied_changes": [],
        "converged": False,
    }

    # ── Stage 1: Coleta (sempre roda) ──
    try:
        from .stage1_collect import run as stage1_run
        result = stage1_run(ctx)
        ctx["performance"] = result.get("performance", {})
        ctx["regime"] = result.get("regime", {})
        ctx["audit"].append({"stage": 1, "ok": True, "summary": result.get("summary", "")})
        log.info(f"[{TAG}] Stage 1 (collect) OK — {result.get('summary', '')}")
    except Exception as e:
        log.error(f"[{TAG}] Stage 1 (collect) FALHOU: {e}", exc_info=True)
        ctx["audit"].append({"stage": 1, "ok": False, "error": str(e)})
        # Sem performance, não há o que otimizar — aborta limpo
        ctx["ended_at"] = datetime.now().isoformat()
        ctx["duration_s"] = time.time() - start_ts
        return ctx

    # ── Loop de convergência SEM LIMITE (Lei 5): itera até TODOS os pares
    # positivos. Para por CONVERGÊNCIA (todo par PnL>0) ou ESTAGNAÇÃO
    # (uma iteração sem melhorar nenhum par = espaço de busca esgotado
    # nesta execução; próxima execução do cron retenta com dados frescos).
    # max_iterations é só teto de segurança anti-bug.
    prev_failing_count = None
    prev_failing_pairs = set()
    stagnation_counter = 0

    for it in range(1, max_iterations + 1):
        ctx["current_iteration"] = it
        log.info(f"[{TAG}] ── Iteração {it} ── ({len(prev_failing_pairs)} par(es) pendentes)")

        # Stage 2: Intel (web + llm) — gera hipóteses (pula se já convergiu)
        if prev_failing_pairs:
            _safe_run_stage(ctx, 2, "intel", "stage2_intel")

        # Stage 3: Busca exaustiva paralela nos pares ainda negativos
        _safe_run_stage(ctx, 3, "search", "stage3_exhaustive")

        # Stage 5: Aplicação com gates (só aplica PnL > 0)
        _safe_run_stage(ctx, 5, "apply", "stage5_apply")

        # Checar convergência por SIMULAÇÃO: todo par com PnL > 0?
        converged, failing_pairs = _check_convergence_simulated(ctx)
        ctx["failing_pairs"] = failing_pairs

        if converged:
            ctx["converged"] = True
            log.info(f"[{TAG}] ✅ CONVERGÊNCIA na iteração {it} — "
                     f"todos os {len(_all_pairs(ctx))} pares lucrativos por simulação")
            break

        current_failing = {f["pair"] for f in failing_pairs}

        # ── Detecção de estagnação ──
        # Se uma iteração inteira (busca + apply) não melhorou NENHUM par
        # (mesmo conjunto de failing), o espaço de busca dessa execução se
        # esgotou. Em vez de girar pra sempre, força geração de estratégias
        # novas (stage4) e checa novamente.
        improved = len(current_failing) < len(prev_failing_pairs) if prev_failing_pairs else True
        same_set = current_failing == prev_failing_pairs

        if improved:
            stagnation_counter = 0
        else:
            stagnation_counter += 1

        # Stage 4: Geração de estratégias novas para os pares ainda negativos.
        # Roda sempre que há failing pairs (Lei 5: se busca não achou, gera).
        log.info(f"[{TAG}] {len(current_failing)} par(es) sem lucro — "
                 f"forçando stage4 (geração de estratégias novas)")
        _safe_run_stage(ctx, 4, "generate", "stage4_generate")

        # Re-aplicar após geração (as novas podem passar nos gates)
        _safe_run_stage(ctx, 5, "apply", "stage5_apply")

        # Re-checar convergência após geração
        converged, failing_pairs = _check_convergence_simulated(ctx)
        ctx["failing_pairs"] = failing_pairs
        if converged:
            ctx["converged"] = True
            log.info(f"[{TAG}] ✅ CONVERGÊNCIA após stage4 na iteração {it}")
            break

        current_failing = {f["pair"] for f in failing_pairs}
        improved_after_gen = len(current_failing) < len(prev_failing_pairs) if prev_failing_pairs else True

        if improved_after_gen:
            stagnation_counter = 0
        else:
            stagnation_counter += 1

        # ESTAGNAÇÃO: 2 iterações seguidas sem melhorar = espaço esgotado.
        # O LLM pode estar gerando a mesma estratégia; o grid já foi varrido.
        # Parar e reportar — a próxima execução do cron retoma com dados
        # de mercado frescos (30d rolam) e novas ideias web/LLM.
        if stagnation_counter >= 2:
            log.warning(f"[{TAG}] ⚠️ ESTAGNAÇÃO detectada ({stagnation_counter} iterações "
                        f"sem progresso). Espaço de busca esgotado nesta execução. "
                        f"{len(current_failing)} par(es) ainda negativos. "
                        f"Próxima execução do cron retenta com dados frescos.")
            ctx["stagnated"] = True
            break

        prev_failing_pairs = current_failing
        prev_failing_count = len(current_failing)
        log.info(f"[{TAG}] {len(current_failing)} par(es) ainda negativos — próxima iteração")

    # ── Stage 6: Relatório (sempre roda) ──
    _safe_run_stage(ctx, 6, "report", "stage6_report")

    ctx["ended_at"] = datetime.now().isoformat()
    ctx["duration_s"] = time.time() - start_ts
    log.info(f"[{TAG}] AGI v4 pipeline finalizado — "
             f"converged={ctx['converged']} stagnated={ctx.get('stagnated', False)} "
             f"iterations={ctx.get('current_iteration', 0)} "
             f"duration={ctx['duration_s']:.1f}s "
             f"applied_changes={len(ctx.get('applied_changes', []))} "
             f"failing={len(ctx.get('failing_pairs', []))}")
    return ctx


def _all_pairs(ctx: dict) -> list[str]:
    """Lista todos os pares SYM_TF do config."""
    config = ctx.get("config", {}) or {}
    symbols = config.get("symbols", [])
    tfs_by_sym = config.get("timeframes_by_symbol", {})
    global_tfs = config.get("timeframes", [])
    pairs = []
    for sym in symbols:
        for tf in tfs_by_sym.get(sym, global_tfs):
            pairs.append(f"{sym}_{tf}")
    return pairs


def _safe_run_stage(ctx: dict, stage_num: int, stage_name: str, module_name: str) -> None:
    """Executa um stage com tratamento de erro isolado.

    Um stage que falha NÃO derruba o pipeline — loga o erro e segue. Isto é
    intencional: se web_search (stage 2) cair, ainda fazemos busca exaustiva
    (stage 3) com os params existentes.
    """
    try:
        module = importlib.import_module(f".{module_name}", package=__package__)
        result = module.run(ctx)
        ctx["audit"].append({
            "stage": stage_num,
            "iteration": ctx.get("current_iteration"),
            "ok": True,
            "summary": result.get("summary", "") if isinstance(result, dict) else "",
        })
        log.info(f"[{TAG}] Stage {stage_num} ({stage_name}) OK — "
                 f"{result.get('summary', '') if isinstance(result, dict) else 'done'}")
    except ModuleNotFoundError as e:
        # Stage ainda não implementado (Fase A: stubs) — não é erro, é WIP
        log.info(f"[{TAG}] Stage {stage_num} ({stage_name}) não implementado ainda: {e.name}")
        ctx["audit"].append({
            "stage": stage_num,
            "iteration": ctx.get("current_iteration"),
            "ok": None,
            "reason": f"stage não implementado: {e.name}",
        })
    except Exception as e:
        log.error(f"[{TAG}] Stage {stage_num} ({stage_name}) FALHOU: {e}", exc_info=True)
        ctx["audit"].append({
            "stage": stage_num,
            "iteration": ctx.get("current_iteration"),
            "ok": False,
            "error": str(e),
        })


def _check_convergence_simulated(ctx: dict) -> tuple[bool, list[dict]]:
    """Verifica convergência por SIMULAÇÃO bar-by-bar (Lei 5 absoluta).

    A função da AGI é SEMPRE achar estratégia + params lucrativos. Um par
    só é considerado convergido se a estratégia atualmente atribuída no
    config produzir PnL > 0 na simulação 30d. Sem trades = não convergiu
    (precisa de uma estratégia que opere). PnL = 0 = não convergiu.

    Lê o config atual (pós stage5 apply) e simula cada par via
    evaluate_baseline — nunca lê trades passados do DB.

    Returns:
        (converged: bool, failing_pairs: list[{"pair", "pnl", "n_trades"}])
    """
    config = ctx.get("config", {}) or {}

    # SEMPRE recarregar do disco quando não é dry-run — o stage5 pode ter
    # aplicado mudanças que ainda não refletem no config em memória do ctx.
    # BUG CORRIGIDO (2026-07-04): antes só recarregava se applied_changes
    # não-vazio, mas ctx["applied_changes"] podia estar stale/vazio mesmo
    # após escritas. Agora sempre busca a versão mais recente do disco.
    if not ctx.get("dry_run", True):
        try:
            from core.vt_config_loader import load_config
            config = load_config(force=True)
            ctx["config"] = config
        except Exception:
            pass

    symbols = config.get("symbols", [])
    tfs_by_sym = config.get("timeframes_by_symbol", {})
    global_tfs = config.get("timeframes", [])

    failing = []
    try:
        from optimization.agi_v4.backtest_evaluator import evaluate_baseline
    except ImportError:
        log.error("backtest_evaluator não disponível — convergência indisponível")
        return False, [{"pair": "?", "pnl": 0, "n_trades": 0, "error": "no evaluator"}]

    for sym in symbols:
        for tf in tfs_by_sym.get(sym, global_tfs):
            pair = f"{sym}_{tf}"
            try:
                m = evaluate_baseline(sym, tf, config)
                pnl = m.get("total_pnl", 0)
                n_trades = m.get("n_trades", 0)
            except Exception as e:
                log.warning(f"convergência {pair}: simulação falhou ({e})")
                failing.append({"pair": pair, "pnl": 0, "n_trades": 0, "error": str(e)})
                continue

            # REGRA ABSOLUTA: par só converge se PnL > 0.
            if pnl <= 0:
                failing.append({"pair": pair, "pnl": pnl, "n_trades": n_trades})

    converged = len(failing) == 0
    if failing:
        log.info(f"convergência: {len(failing)} par(es) não lucrativos: "
                 f"{[f['pair'] for f in failing]}")
    return converged, failing
