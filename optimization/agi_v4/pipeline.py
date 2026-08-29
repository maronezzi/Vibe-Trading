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

    # Wave AGI-param-tuning (Bruno 12/08): registra sanctioned params das AGI4_*
    # promovidas no PROCESSO PRINCIPAL. O Stage 3 roda em ProcessPoolExecutor
    # (processos separados); o registry _SANCTIONED_PARAMS é por-processo, então
    # o registro tem que acontecer aqui — onde o Stage 5 (que valida a escrita)
    # roda. Sem isto, params próprios de AGI4 existentes seriam rejeitados pelo
    # guardrail (default-deny). Fail-safe: falha aqui só loga (não derruba).
    try:
        from optimization.agi_v4.param_tuner import sanctioned_spec
        from optimization.agi_v4.guardrails import register_sanctioned_params
        from optimization.exhaustive_strategy_search import (
            strategy_path_by_name, ALL_STRATEGIES,
        )
        n_reg = 0
        for _name in ALL_STRATEGIES:
            if not _name.startswith("AGI4_"):
                continue
            _path = strategy_path_by_name(_name)
            if not _path:
                continue
            _spec = sanctioned_spec(_path)
            if _spec:
                register_sanctioned_params(_name, _spec)
                n_reg += 1
        if n_reg:
            log.info(f"[{TAG}] bootstrap AGI4 sanctioned: {n_reg} estratégia(s) "
                     f"registrada(s) no processo principal")
    except Exception as e:
        log.warning(f"[{TAG}] bootstrap AGI4 sanctioned falhou: {e}")

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

    # ── Wave AGI-rollover (Bruno 2026-08-13): estado de rolagem dos símbolos ──
    # Consciência de vencimento no AGI: loga contrato/vencimento/dias úteis e
    # flags FREEZE (≤3d úteis p/ vencer) / GRACE (rolagem há ≤3d). O Stage 5
    # usa o mesmo guard para bloquear mudanças nesses símbolos.
    try:
        from . import rollover_guard
        ctx["rollover_state"] = rollover_guard.all_symbols_state(config)
        log.info(f"[{TAG}] Estado de rolagem (FREEZE_DAYS={rollover_guard.FREEZE_DAYS}, "
                 f"GRACE_DAYS={rollover_guard.GRACE_DAYS}):")
        for _st in ctx["rollover_state"].values():
            log.info("[{tag}] {line}".format(tag=TAG, line=rollover_guard.format_state_line(_st)))
        # Sanidade perpétua vs contrato live (incidente 05-12/08: WIN$=V26
        # enquanto o live operava Q26 com 2.500-4.000pts de diferença).
        ctx["series_sanity"] = rollover_guard.series_sanity(config)
        log.info(f"[{TAG}] Sanidade série perpétua vs contrato live:")
        for _se in ctx["series_sanity"].values():
            log.info("[{tag}] {line}".format(tag=TAG, line=rollover_guard.format_series_line(_se)))
    except Exception as _rg_e:
        log.warning(f"[{TAG}] rollover_state falhou: {_rg_e}")

    # ── Stage 1: Coleta (sempre roda) ──
    try:
        from .stage1_collect import run as stage1_run
        result = stage1_run(ctx)
        ctx["performance"] = result.get("performance", {})
        ctx["regime"] = result.get("regime", {})
        # BUG CRÍTICO CORRIGIDO (Wave LLM-AGI, 2026-07-17): antes o pipeline
        # NÃO copiava failing_pairs do result do stage1 para o ctx. Resultado:
        # o loop usava ctx["failing_pairs"] vazio → stage3 buscava em TODOS os
        # 16 pares em vez dos ~4 failing → 4x mais trabalho (runs de 1h30+).
        # Agora propagamos explicitamente. ctx["failing_pairs"] é a lista de
        # pares alvo (list[str]) lida por stage2/stage3/stage5.
        ctx["failing_pairs"] = result.get("failing_pairs", [])
        ctx["audit"].append({"stage": 1, "ok": True, "summary": result.get("summary", "")})
        log.info(f"[{TAG}] Stage 1 (collect) OK — {result.get('summary', '')}")
        # Wave AGI-comms: brief de diagnóstico (o que o AGI encontrou)
        try:
            _ts = datetime.now().strftime("%H:%M")
            _fp = _normalize_failing(ctx.get("failing_pairs", []))
            _rs = ctx.get("rollover_state") or {}
            _flags = [f"{'🧊' if _s.get('freeze') else '⏳'} {_s.get('symbol')}"
                      for _s in _rs.values()
                      if isinstance(_s, dict) and (_s.get("freeze") or _s.get("grace"))]
            _msg = (f"🔎 AGI v4 — diagnóstico ({_ts})\n"
                    f"• failing: {', '.join(_fp[:5]) if _fp else 'nenhum'}\n")
            if _flags:
                _msg += f"• rolagem protegida: {', '.join(_flags)}\n"
            _msg += "• em execução: busca exaustiva + geração de estratégias..."
            _notify_progress(ctx, _msg)
        except Exception:
            pass
    except Exception as e:
        log.error(f"[{TAG}] Stage 1 (collect) FALHOU: {e}", exc_info=True)
        ctx["audit"].append({"stage": 1, "ok": False, "error": str(e)})
        # Sem performance, não há o que otimizar — aborta limpo
        ctx["ended_at"] = datetime.now().isoformat()
        ctx["duration_s"] = time.time() - start_ts
        return ctx

    # ── Early-exit: se stage1 não achou pares perdedores, não há o que
    # otimizar (Wave LLM-AGI, 2026-07-17). Antes este caso caía no loop e
    # rodava stage3 (busca exaustiva) sobre TODOS os 16 pares mesmo sem
    # failing — desperdício massivo. Agora convergido de saída.
    initial_failing = _normalize_failing(ctx.get("failing_pairs", []))
    if not initial_failing:
        log.info(f"[{TAG}] ✅ Nenhum par perdedor identificado no stage1 — "
                 f"sem failing pairs. Otimizando lucrativos antes do report.")
        ctx["converged"] = True
        ctx["failing_pairs"] = []
        ctx["_loop_exhausted"] = True  # Wave 882: sem failing, nada a desativar
        # Wave 881: mesmo sem failing pairs, roda otimização dos lucrativos
        # (busca estratégias/params melhores que a baseline atual).
        _optimize_profitable_pairs(ctx)
        _run_sweep_pending(ctx)
        _run_tune_incumbents(ctx)
        _run_risk_calibrator(ctx)
        _run_live_kill_switch(ctx)
        _safe_run_stage(ctx, 6, "report", "stage6_report")
        ctx["ended_at"] = datetime.now().isoformat()
        ctx["duration_s"] = time.time() - start_ts
        log.info(f"[{TAG}] AGI v4 pipeline finalizado (sem failing pairs) — "
                 f"duration={ctx['duration_s']:.1f}s")
        return ctx

    # ── Loop de convergência SEM LIMITE (Lei 5): itera até TODOS os pares
    # positivos. Para por CONVERGÊNCIA (todo par PnL>0) ou ESTAGNAÇÃO
    # (uma iteração sem melhorar nenhum par = espaço de busca esgotado
    # nesta execução; próxima execução do cron retenta com dados frescos).
    # max_iterations é só teto de segurança anti-bug.
    # Wave LLM-AGI: inicializa prev_failing_pairs com os pares do stage1.
    # Antes era set() vazio → primeira iteração sempre via "improved=True"
    # e o log mostrava "0 par(es) pendentes" enganoso. Agora reflete realidade.
    prev_failing_pairs = set(initial_failing)
    stagnation_counter = 0
    # Wave 880.A3: deadline hard — impede LLM travado prender o cron.
    # Wave noturno-generoso (Bruno 01/08): "tempo não é problema para o AGI".
    # O AGI roda às 17:10 (pós-close) e tem a madrugada toda. Deadline subiu
    # de 90min para 8h (configurável via VT_AGI_DEADLINE_MINS). 8h cobre até
    # ~1h10 da manhã com folga — antes do pre-flight das 8:55.
    import os as _os
    _deadline_t0 = time.time()
    _DEADLINE_SECS = int(_os.environ.get("VT_AGI_DEADLINE_MINS", "480")) * 60

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
        # Wave noturno-generoso (Bruno 01/08): estagnação 2→3 (configurável).
        # Dá uma chance extra ao Stage 4 gerar estratégia nova. "Tempo não é
        # problema" — o AGI tem a madrugada toda para tentar exaustivamente.
        _MAX_STAGNATION = int(_os.environ.get("VT_AGI_MAX_STAGNATION", "3"))
        if stagnation_counter >= _MAX_STAGNATION:
            log.warning(f"[{TAG}] ⚠️ ESTAGNAÇÃO detectada ({stagnation_counter} iterações "
                        f"sem progresso). Espaço de busca esgotado nesta execução. "
                        f"{len(current_failing)} par(es) ainda negativos. "
                        f"Próxima execução do cron retenta com dados frescos.")
            ctx["stagnated"] = True
            break

        # Wave 880.A3/noturno-generoso: deadline hard (90min→8h configurável).
        # Bruno 01/08: "tempo não é problema para o AGI". O AGI roda às 17:10 e
        # tem a madrugada. Não conta como estagnação; é proteção contra LLM
        # travado. Próxima execução do cron
        # retoma com janelas de mercado roladas (30d).
        if time.time() - _deadline_t0 > _DEADLINE_SECS:
            log.warning(f"[{TAG}] ⏰ DEADLINE 90min atingido ({(time.time()-_deadline_t0)/60:.0f}min) "
                        f"na iteração {it}. Parando por tempo. "
                        f"{len(current_failing)} par(es) ainda negativos. "
                        f"Próxima execução do cron retenta.")
            ctx["deadline_hit"] = True
            break

        prev_failing_pairs = current_failing
        log.info(f"[{TAG}] {len(current_failing)} par(es) ainda negativos — próxima iteração")

    # ── Wave 881: Otimização dos pares lucrativos ──
    # Antes o AGI só consertava pares perdedores (gate pnl<=0); pares lucrativos
    # ficavam congelados no estado em que estavam — nenhuma otimização contínua.
    # Agora, após encerrar o loop de failing (por convergência, estagnação ou
    # deadline), varre os pares lucrativos buscando estratégias/params melhores.
    # Reusa Stage 3 (busca) + Stage 5 (apply). O Stage 5 já tem o gate
    # better_baseline_exists: só aplica se cand_score > base_score — estratégia
    # lucrativa nunca é trocada por pior. Guardrails (default-deny) protegem
    # kill switches/metadata.
    #
    # Wave 882 (Bruno 04/08): marca que o loop esgotou as tentativas de
    # otimização. O Stage 5 usa este flag para PERMITIR a desativação de pares
    # ainda failing — agora que o AGI tentou de tudo (busca + geração nas N
    # iterações do loop), faz sentido desativar o que persiste negativo.
    # Rodamos uma chamada final do Stage 5 aqui (com failing_pairs reais e
    # _loop_exhausted=True) para desativar os pares que o AGI não conseguiu
    # tornar lucrativos. Em seguida, _optimize_profitable_pairs reseta o flag
    # internamente para não desativar os lucrativos que ela está otimizando.
    ctx["_loop_exhausted"] = True
    # Wave AGI-comms: brief de fase final
    try:
        _ts = datetime.now().strftime("%H:%M")
        _notify_progress(ctx,
            f"⚙️ AGI v4 — fase final ({_ts})\n"
            f"• busca concluída em {ctx.get('current_iteration', 0)} iteração(ões)\n"
            f"• a seguir: otimizar lucrativos → sweep _pending → tune incumbentes → calibrar risco\n"
            f"• relatório completo em instantes")
    except Exception:
        pass
    _safe_run_stage(ctx, 5, "apply_final_deactivation", "stage5_apply")
    _optimize_profitable_pairs(ctx)

    # Wave AGI-sweep (Bruno 12/08): varre TODO o strategies/_pending/, testa em
    # todos os pares ativos, otimiza params e promove as que têm edge. Antes era
    # um script manual (e quebrado — writer não autorizado). Agora fecha o ciclo:
    # o AGI gera → põe em _pending → sweep testa tudo → promove as boas.
    _run_sweep_pending(ctx)

    # Wave AGI-tune-incumbents (Bruno 12/08): tuning FINO dos params próprios de
    # TODAS as AGI4 que JÁ OPERAM (incumbentes). Antes só recebiam o Stage 3
    # (grid ralo); agora tune_strategy (~40 combos) roda em cada uma, no par em
    # que opera, e aplica via stage5 se superar o better_baseline. Fecha
    # "otimizar todos os parâmetros" de verdade.
    _run_tune_incumbents(ctx)

    # Wave AGI-super (13/08): calibração de risco pelo próprio AGI (stop
    # diário por símbolo, alvo de lucro, slippage) — simulação counterfactual.
    _run_risk_calibrator(ctx)

    # Wave 880.II (Bruno 26/08): kill-switch live — pares sangrando no real
    # saem do ar mesmo com sim positiva (incidente WDO_M15 -R$337/14d).
    _run_live_kill_switch(ctx)

    # Wave AGI-backfill (16/08): inteligência de sessão pelo replay histórico
    # do forward_walker — propõe/valida/aplica time_blocks contrafactual.
    # Só age no cron pós-close (17h10); o do meio-dia pula (guarda interna).
    _run_backfill_intel(ctx)

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


def _normalize_failing(failing) -> list[str]:
    """Normaliza failing_pairs (list[str] ou list[dict]) para list[str].

    Stage1 retorna list[str]; o loop de convergência (via
    _check_convergence_simulated) retorna list[dict] com chave "pair".
    Este helper unifica para comparação consistente entre iterações.
    """
    pairs = []
    for f in failing or []:
        if isinstance(f, str):
            pair = f
        elif isinstance(f, dict):
            pair = f.get("pair", "")
        else:
            pair = ""
        if pair:
            pairs.append(pair)
    return pairs


def _optimize_profitable_pairs(ctx: dict) -> None:
    """Wave 881: varre pares lucrativos buscando estratégias/params melhores.

    Antes desta wave o AGI só atuava sobre pares perdedores (failing_pairs),
    cuja convergência é definida por ``pnl <= 0`` em
    ``_check_convergence_simulated``. Pares lucrativos ficavam congelados no
    estado em que estavam — nenhuma otimização contínua, mesmo que existisse
    uma estratégia/params nitidamente melhores.

    Esta fase roda Stage 3 (busca exaustiva) + Stage 5 (apply) sobre os pares
    identificados como lucrativos pelo stage1 (``ctx["profitable_pairs"]``).
    O Stage 5 já tem o gate ``better_baseline_exists`` (só aplica se
    ``cand_score > base_score``), então uma estratégia lucrativa só é trocada
    por outra cuja simulação 30d (+ bônus hoje, hoje-conta-mais) supere a
    baseline atual. Em dry-run nada é escrito.

    O custo adicional por execução é ~40-110s por par lucrativo (fetch MT5 +
    grid de combos). Com ~13 lucrativos, são ~15-25 min a mais por cron —
    cobertos pelo deadline de 8h (``VT_AGI_DEADLINE_MINS``).
    """
    profitable = ctx.get("profitable_pairs", []) or []
    if not profitable:
        return  # stage1 não populou lucrativos — nada a otimizar

    log.info(f"[{TAG}] ── Otimização de {len(profitable)} par(es) lucrativo(s) ── "
             f"(busca estratégias/params melhores que a baseline atual)")

    # Stage 3 lê ctx["failing_pairs"] como pares-alvo. Salvamos o estado
    # atual (pares ainda failing, se houver) e apontamos temporariamente para
    # os lucrativos. O stage3 nunca escreve no config — só popula
    # ctx["search_results"]. Restauramos ctx["failing_pairs"] ao final para
    # preservar o histórico do loop de convergência (usado no stage6 report).
    saved_failing = ctx.get("failing_pairs", [])

    ctx["failing_pairs"] = profitable
    ctx["search_results"] = []  # reset: resultados são da otimização lucrativa

    # Wave 882 (Bruno 04/08): durante a otimização de lucrativos, ctx["failing_pairs"]
    # é temporariamente sobrescrito com os pares LUCRATIVOS (acima). Se
    # _loop_exhausted=True estiver setado, o Stage 5 os veria como "failing" e
    # tentaria DESATIVÁ-LOS — exatamente o oposto do desejado. Resetamos o flag
    # aqui para que o Stage 5 não desative nada durante esta fase; a desativação
    # real dos failing já aconteceu no loop acima (se aplicável). Restauramos
    # ao final para preservar o estado para o report.
    saved_loop_exhausted = ctx.get("_loop_exhausted", False)
    ctx["_loop_exhausted"] = False

    _safe_run_stage(ctx, 3, "search_profitable", "stage3_exhaustive")
    _safe_run_stage(ctx, 5, "apply_profitable", "stage5_apply")

    # Restaura _loop_exhausted (para o report/stage6 saber que o loop esgotou)
    ctx["_loop_exhausted"] = saved_loop_exhausted

    # Registra as otimizações aplicadas (separadas das mudanças do loop failing)
    ctx["profit_optimizations"] = list(ctx.get("applied_changes", []) or [])

    # Restaura estado para o report final refletir o loop de failing
    ctx["failing_pairs"] = saved_failing
    n_opt = len(ctx["profit_optimizations"])
    log.info(f"[{TAG}] Otimização de lucrativos concluída — "
             f"{n_opt} melhoria(s) aplicada(s)")


def _run_sweep_pending(ctx: dict) -> None:
    """Wave AGI-sweep (Bruno 12/08): varre strategies/_pending/ no pipeline.

    Delega a ``sweep_pending.run(ctx)`` (smoke + cross-evaluate em todos os pares
    + tune + promote via stage5). Fail-safe: exceção só loga — nunca derruba o
    pipeline. O sweep testa as estratégias acumuladas em _pending/ em TODOS os
    índices/TFs ativos, otimiza params das que têm edge e promove as melhores.
    """
    try:
        from optimization.agi_v4 import sweep_pending
        result = sweep_pending.run(ctx)
        summary = result.get("summary", "") if isinstance(result, dict) else ""
        ctx["audit"].append({"stage": "sweep_pending", "ok": True, "summary": summary})
        log.info(f"[{TAG}] Sweep _pending/ OK — {summary}")
    except Exception as e:
        log.error(f"[{TAG}] Sweep _pending/ FALHOU: {e}", exc_info=True)
        ctx["audit"].append({"stage": "sweep_pending", "ok": False, "error": str(e)})


def _run_risk_calibrator(ctx: dict) -> None:
    """Wave AGI-super (Bruno 13/08): o AGI calibra os próprios parâmetros de
    risco — stop diário por símbolo, alvo diário de lucro (profit lock) e
    tolerância de slippage — por simulação counterfactual nos trades reais.
    Roda pós-mercado, com evidência mínima p/ mexer (anti-churn). Fail-safe.
    """
    try:
        from optimization.agi_v4 import risk_calibrator
        result = risk_calibrator.run(ctx)
        summary = result.get("summary", "") if isinstance(result, dict) else ""
        ctx["audit"].append({"stage": "risk_calibrator", "ok": True, "summary": summary})
        log.info(f"[{TAG}] Risk calibrator OK — {summary}")
    except Exception as e:
        log.error(f"[{TAG}] Risk calibrator FALHOU: {e}", exc_info=True)
        ctx["audit"].append({"stage": "risk_calibrator", "ok": False, "error": str(e)})


def _run_live_kill_switch(ctx: dict) -> None:
    """Wave 880.II (Bruno 26/08): kill-switch LIVE — desativa pares com
    sangramento real persistente (tabela trades), independente da sim.
    Decide em optimization/agi_v4/live_kill_switch (puro); aplica em
    stage5_apply.live_kill_switch_pass (único writer autorizado). Fail-safe.
    """
    try:
        from optimization.agi_v4 import stage5_apply
        killed = stage5_apply.live_kill_switch_pass(ctx)
        summary = (f"{len(killed)} par(es) desativado(s)"
                   if killed else "nenhum par sangrando")
        ctx["audit"].append({"stage": "live_kill_switch", "ok": True,
                             "summary": summary})
        log.info(f"[{TAG}] Kill-switch live OK — {summary}")
    except Exception as e:
        log.error(f"[{TAG}] Kill-switch live FALHOU: {e}", exc_info=True)
        ctx["audit"].append({"stage": "live_kill_switch", "ok": False,
                             "error": str(e)})


def _run_backfill_intel(ctx: dict) -> None:
    """Wave AGI-backfill (Bruno 16/08): o AGI valida e calibra filtros de
    sessão (time_blocks) por replay histórico contrafactual do
    forward_walker. Delega a ``backfill_intel.run(ctx)``. Fail-safe.
    """
    try:
        from optimization.agi_v4 import backfill_intel
        result = backfill_intel.run(ctx)
        summary = result.get("summary", "") if isinstance(result, dict) else ""
        ctx["audit"].append({"stage": "backfill_intel", "ok": True, "summary": summary})
        log.info(f"[{TAG}] Backfill intel OK — {summary}")
    except Exception as e:
        log.error(f"[{TAG}] Backfill intel FALHOU: {e}", exc_info=True)
        ctx["audit"].append({"stage": "backfill_intel", "ok": False, "error": str(e)})


def _run_tune_incumbents(ctx: dict) -> None:
    """Wave AGI-tune-incumbents (Bruno 12/08): tuning fino dos params das AGI4
    incumbentes (que já operam). Delega a ``tune_incumbents.run(ctx)``. Fail-safe.
    """
    try:
        from optimization.agi_v4 import tune_incumbents
        result = tune_incumbents.run(ctx)
        summary = result.get("summary", "") if isinstance(result, dict) else ""
        ctx["audit"].append({"stage": "tune_incumbents", "ok": True, "summary": summary})
        log.info(f"[{TAG}] Tune incumbents OK — {summary}")
    except Exception as e:
        log.error(f"[{TAG}] Tune incumbents FALHOU: {e}", exc_info=True)
        ctx["audit"].append({"stage": "tune_incumbents", "ok": False, "error": str(e)})


def _notify_progress(ctx: dict, msg: str) -> None:
    """Brief de progresso no Telegram — 'o que está sendo feito agora'.

    Wave AGI-comms (Bruno 13/08): runs de ~20min eram uma caixa-preta entre
    o START e o relatório final. Máximo de 2 briefs intermediários por run
    (diagnóstico + fase final) para informar sem spam. Fail-safe.
    """
    try:
        from .stage6_report import send_brief
        send_brief(msg, retries=0)
    except Exception as _e:
        log.debug(f"notify_progress falhou (não crítico): {_e}")


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

    # ── Cache por _version (Wave LLM-AGI, 2026-07-17) ──
    # Esta função é chamada 2x por iteração do loop de convergência. Cada
    # chamada re-simula TODOS os pares via evaluate_baseline (fetch MT5 Wine
    # + backtest bar-by-bar) = ~16 fetches lentos por chamada. Se o config
    # NÃO mudou desde a última checagem (mesmo _version), o resultado é
    # idêntico — pular a re-simulação. Só re-simula se stage5 aplicou algo.
    config_version = config.get("_version", 0)
    cache_key = f"conv_cache_v{config_version}"
    cached = ctx.get(cache_key)
    if cached is not None:
        log.debug(f"convergência: cache HIT (_version={config_version}) — "
                  f"{len(cached)} failing pairs")
        return (len(cached) == 0), cached

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

    # Salvar no cache para esta versão do config (próxima checagem no mesmo
    # _version pula as 16 simulações). Limpa caches de versões antigas.
    ctx[cache_key] = failing
    for key in list(ctx.keys()):
        if key.startswith("conv_cache_v") and key != cache_key:
            del ctx[key]

    converged = len(failing) == 0
    if failing:
        log.info(f"convergência: {len(failing)} par(es) não lucrativos: "
                 f"{[f['pair'] for f in failing]}")
    return converged, failing
