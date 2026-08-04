"""
stage5_apply.py — Aplica candidatos aprovados, comparando contra BASELINE
SIMULADO (não trades passados do DB).

CORREÇÃO FUNDAMENTAL (2026-07-04): o baseline de comparação é a SIMULAÇÃO
da estratégia atual do config nas mesmas 30d de mercado — NÃO os trades
reais do DB. Trades do DB foram da estratégia antiga; não são referência
honesta para julgar um candidato novo.

Gates antes de aplicar:
  1. candidate já passou profitability + walk-forward no stage3 (simulação)
  2. regra1 honesta: PnL simulado do candidato > PnL simulado do baseline
     (estratégia atual do config nas mesmas 30d)

Lei 2: nunca desabilita símbolo/TF. Se candidato falha, mantém o atual.
"""
from __future__ import annotations

import logging
import os

from .gates import load_thresholds

log = logging.getLogger("agi_v4.stage5")

# Wave 882 (Bruno 04/08): o AGI NÃO desabilita um par failing na primeira
# iteração — só depois de tentar otimizar pelo menos N vezes. Antes, o
# _deactivate_failing_pairs rodava a cada chamada do Stage 5 (inclusive na
# iteração 1), desabilitando 13 pares às 12h42 baseado num PnL≤0 instantâneo
# que flutuava entre lucrativo/failing conforme o momento da simulação.
# Agora o AGI itera (Stage 3 busca exaustiva + Stage 4 geração) pelo menos
# MIN_ITERS_BEFORE_DEACTIVATE vezes antes de considerar desativar um par.
# Configurável via env (default 5, pedido Bruno 04/08: "tentar no mínimo 5").
MIN_ITERS_BEFORE_DEACTIVATE = int(
    os.environ.get("VT_AGI_MIN_ITERS_BEFORE_DEACTIVATE", "5")
)


def run(ctx: dict) -> dict:
    """Aplica candidatos cuja simulação supera o baseline simulado.

    Args:
        ctx["search_results"]: candidatos aprovados pelo stage3
        ctx["config"]: config atual
        ctx["dry_run"]: se True, não escreve
    """
    config = ctx.get("config", {}) or {}
    thresholds = ctx.get("thresholds") or load_thresholds(config)
    dry_run = ctx.get("dry_run", True)
    candidates = ctx.get("search_results", []) or []

    applied = []
    rejected = []

    for cand in candidates:
        result = _apply_one(cand, config, thresholds, dry_run, ctx)
        (applied if result["applied"] else rejected).append(result)

    ctx["applied_changes"] = applied
    ctx["rejected_changes"] = rejected

    # Wave AGI-soberano (Bruno 01/08): reativa pares lucrativos que estão
    # bloqueados. O stage1 popula ctx["profitable_pairs"] com pares cuja
    # simulação 30d deu PnL>0. Se algum desses está em disabled_timeframes,
    # o AGI (soberano) remove o bloqueio e ativa day_trade_intent — não faz
    # sentido manter bloqueado um par que o próprio backtest validou como
    # lucrativo. Antes, pares otimizados ficavam presos no bloqueio.
    reactivated = []
    deactivated = []
    if not dry_run:
        # Reativação (lado entrada): sempre permitida desde a iteração 1.
        # Um par lucrativo não deve ficar bloqueado.
        reactivated = _reactivate_profitable_pairs(ctx)

        # Desativação (lado saída) — Wave 882 (Bruno 04/08): SÓ após o AGI
        # ter tentado otimizar pelo menos MIN_ITERS_BEFORE_DEACTIVATE vezes.
        # Antes deste gate, o AGI desabilitava pares failing já na iteração 1,
        # baseado num PnL≤0 instantâneo instável — matando pares que eram
        # lucrativos em re-simulação (incidente 12h42 de 04/08: 13 pares off).
        # Agora o AGI continua otimizando (busca + geração) antes de desativar.
        # O pipeline também seta ctx["_loop_exhausted"]=True quando o loop
        # termina (convergência/estagnação/deadline) — nesse momento a
        # desativação é permitida independentemente da contagem de iterações.
        current_iter = ctx.get("current_iteration", 1)
        loop_exhausted = ctx.get("_loop_exhausted", False)
        if current_iter >= MIN_ITERS_BEFORE_DEACTIVATE or loop_exhausted:
            deactivated = _deactivate_failing_pairs(ctx)
        else:
            n_failing = len(_normalize_pairs(ctx.get("failing_pairs", [])))
            if n_failing:
                log.info(
                    f"⏳ Desativação suprimida (iter {current_iter} < "
                    f"{MIN_ITERS_BEFORE_DEACTIVATE}): AGI ainda tentando "
                    f"otimizar {n_failing} par(es) failing. Reativação "
                    f"permanece ativa."
                )
    else:
        # Wave 881: normaliza profitable/failing para str. ctx["failing_pairs"]
        # pode vir como list[dict] ({"pair":..., "pnl":...}) do
        # _check_convergence_simulated, e list[str] do stage1. Antes a
        # list-comprehension abaixo fazia `p not in disabled` com p=dict,
        # levantando TypeError: unhashable type: 'dict' em dry-run (regressão
        # pós-commit 59cd6b31 que só corrigiu _deactivate_failing_pairs).
        profitable = _normalize_pairs(ctx.get("profitable_pairs", []))
        failing = _normalize_pairs(ctx.get("failing_pairs", []))
        disabled = config.get("disabled_timeframes", []) or []
        dti = config.get("day_trade_intent", {}) or {}
        would_reactivate = [p for p in profitable if p in disabled]
        would_deactivate = [p for p in failing if p not in disabled and dti.get(p, False)]
        if would_reactivate:
            log.info(f"[DRY-RUN] AGI-SOBERANO reativaria {len(would_reactivate)} par(es): {would_reactivate}")
        if would_deactivate:
            log.info(f"[DRY-RUN] AGI-SOBERANO desativaria {len(would_deactivate)} par(es): {would_deactivate}")

    mode = "DRY-RUN" if dry_run else "APLICADO"
    summary = f"{len(applied)} mudança(s) {mode}, {len(rejected)} rejeitada(s)"
    if reactivated:
        summary += f", {len(reactivated)} reativado(s)"
    if deactivated:
        summary += f", {len(deactivated)} desativado(s)"
    # Wave AGI-soberano (01/08): propaga decisões de entra/sai para o ctx,
    # para o stage6 (report) incluir na notificação Telegram.
    ctx["reactivated"] = reactivated
    ctx["deactivated"] = deactivated
    return {"applied_changes": applied, "rejected": rejected,
            "reactivated": reactivated, "deactivated": deactivated,
            "summary": summary}


def _reactivate_profitable_pairs(ctx: dict) -> list[str]:
    """Reativa pares lucrativos bloqueados (AGI soberano).

    Lê ctx["profitable_pairs"] (populado pelo stage1) e, para cada par que
    está em disabled_timeframes ou com day_trade_intent=false, remove o
    bloqueio. Persiste via save_full_config (stage5 é o writer autorizado).

    Returns:
        Lista de pares efetivamente reativados.
    """
    profitable = ctx.get("profitable_pairs", []) or []
    if not profitable:
        return []
    from core.vt_config_loader import load_config, save_full_config
    fresh = load_config(force=True)
    disabled = fresh.get("disabled_timeframes", []) or []
    dti = fresh.setdefault("day_trade_intent", {})
    changed = False
    reactivated = []
    for pair in profitable:
        was_blocked = pair in disabled or not dti.get(pair, False)
        if pair in disabled:
            disabled = [x for x in disabled if x != pair]
            changed = True
        if not dti.get(pair, False):
            dti[pair] = True
            changed = True
        if was_blocked:
            reactivated.append(pair)
    if changed:
        fresh["disabled_timeframes"] = disabled
        fresh["day_trade_intent"] = dti
        save_full_config(fresh, updated_by="agi_v4_stage5")
        # Sincroniza config em memória do ctx
        cfg = ctx.get("config", {}) or {}
        cfg.clear()
        cfg.update(fresh)
        ctx["config"] = cfg
        log.info(f"🔓 AGI-SOBERANO (stage5): reativou {len(reactivated)} "
                 f"par(es) lucrativo(s): {reactivated}")
    return reactivated


def _deactivate_failing_pairs(ctx: dict) -> list[str]:
    """Desativa pares failing que estão ativos (AGI soberano — lado saída).

    Simétrico à _reactivate_profitable_pairs. Lê ctx["failing_pairs"]
    (populado pelo stage1) e, para cada par que está ATIVO (não em
    disabled_timeframes e day_trade_intent=true) mas está perdendo (PnL<=0
    na sim 30d), o AGI desativa — adiciona em disabled_timeframes e seta
    day_trade_intent=false. Não faz sentido operar um par que o backtest
    mostra ser perdedor.

    Bruno 01/08: "se ele decidir que deve entrar deve entrar, se deve sair
    deve sair, a cada iteração do AGI ele decide o que fazer."

    Returns:
        Lista de pares efetivamente desativados.
    """
    failing = ctx.get("failing_pairs", []) or []
    if not failing:
        return []
    # Normaliza: failing_pairs pode vir como list[str] (stage1) ou
    # list[dict] ({"pair":..., "pnl":...} do _check_convergence_simulated).
    # Extrai sempre o nome do par (str) para comparar com disabled_timeframes.
    failing_pairs = []
    for f in failing:
        if isinstance(f, str):
            failing_pairs.append(f)
        elif isinstance(f, dict):
            failing_pairs.append(f.get("pair", ""))
    failing_pairs = [p for p in failing_pairs if p]
    if not failing_pairs:
        return []
    from core.vt_config_loader import load_config, save_full_config
    fresh = load_config(force=True)
    disabled = fresh.get("disabled_timeframes", []) or []
    dti = fresh.setdefault("day_trade_intent", {})
    changed = False
    deactivated = []
    for pair in failing_pairs:
        was_active = pair not in disabled and dti.get(pair, False)
        if pair not in disabled:
            disabled = disabled + [pair]
            changed = True
        if dti.get(pair, False):
            dti[pair] = False
            changed = True
        if was_active:
            deactivated.append(pair)
    if changed:
        fresh["disabled_timeframes"] = disabled
        fresh["day_trade_intent"] = dti
        save_full_config(fresh, updated_by="agi_v4_stage5")
        # Sincroniza config em memória do ctx
        cfg = ctx.get("config", {}) or {}
        cfg.clear()
        cfg.update(fresh)
        ctx["config"] = cfg
        log.info(f"🔒 AGI-SOBERANO (stage5): desativou {len(deactivated)} "
                 f"par(es) failing: {deactivated}")
    return deactivated


def _apply_one(cand: dict, config: dict, thresholds: dict, dry_run: bool, ctx: dict) -> dict:
    pair = cand.get("pair", "")
    sym = pair.split("_", 1)[0] if "_" in pair else pair
    tf = pair.split("_", 1)[1] if "_" in pair else ""
    strategy = cand.get("strategy", "")
    params = cand.get("params", {})
    cand_pnl = cand.get("full", {}).get("total_pnl", 0)

    # ── REGRA ABSOLUTA (2026-07-04): a função da AGI é SEMPRE achar
    # estratégia + params que deem LUCRO (PnL > 0) e WR alto. Nunca aceitar
    # negativo. Um candidato negativo nunca é aplicado, mesmo que seja "menos
    # pior" que o baseline. Se o baseline também é negativo, o par continua
    # failing e o pipeline força geração de estratégia nova (Lei 5).
    if cand_pnl <= 0:
        return _reject(cand, "must_be_profitable",
                       f"candidato R${cand_pnl:.2f} não é lucrativo — AGI só aplica positivo")

    # Baseline simulado só pra registro/comparação (não relaxa o gate).
    try:
        from optimization.agi_v4.backtest_evaluator import evaluate_baseline
        baseline = evaluate_baseline(sym, tf, config)
        baseline_pnl = baseline.get("total_pnl", 0)
    except Exception as e:
        log.warning(f"baseline {pair} falhou ({e}) — fail-safe: aplica")
        baseline_pnl = 0
        baseline = {}

    # ── Score blended: hoje conta extra (Wave hoje-conta-mais) ──
    # Invariante "deve ser lucrativo em 30d real" já passou acima (cand_pnl>0).
    # Aqui comparamos cand vs baseline com um BÔNUS pelo PnL do pregão atual,
    # pra aproveitar a informação real intradia. só aplicamos o bônus se o par
    # tiver today_min_trades hoje (evita overfit numa meia-sessão vazia).
    # today_weight=0 reduz ao comportamento original (comparar total_pnl puro).
    today_weight = float(thresholds.get("today_weight", 0.3))
    today_min = int(thresholds.get("today_min_trades", 3))

    def _blended(total_pnl, metrics):
        if today_weight <= 0:
            return total_pnl
        tp = metrics.get("today_pnl", 0)
        tn = metrics.get("today_n_trades", 0)
        if tn < today_min:
            return total_pnl  # poucas trades hoje → bônus não conta
        return total_pnl + today_weight * tp

    cand_score = _blended(cand_pnl, cand.get("full", {}))
    base_score = _blended(baseline_pnl, baseline)

    # Candidato positivo mas PIOR que baseline positivo: mantém o atual.
    # (ambos positivos → prefere o maior score; nunca troca positivo por menos)
    if baseline_pnl > 0 and cand_score < base_score:
        return _reject(cand, "better_baseline_exists",
                       f"score cand R${cand_score:.2f} < baseline R${base_score:.2f} "
                       f"(cand 30d R${cand_pnl:.2f}/hoje R${cand.get('full', {}).get('today_pnl', 0):.2f}, "
                       f"base 30d R${baseline_pnl:.2f}/hoje R${baseline.get('today_pnl', 0):.2f}) — mantém atual")

    change = _build_change(pair, strategy, params, cand.get("full", {}))
    change["baseline_simulated_pnl"] = baseline_pnl

    # ── Promoção de estratégia gerada ──
    # Se o candidato veio do stage4 (arquivo em strategies/_pending/), ele
    # precisa ser PROMOVIDO para strategies/ antes de atualizar o config.
    # Senão o loader (que ignora _-prefixed) não carrega na próxima
    # simulação, e o par volta a dar 0 trades.
    promoted_path = _maybe_promote_generated(strategy, cand, dry_run)
    change["promoted_from_pending"] = promoted_path

    if not dry_run:
        try:
            _write_to_config(config, change, pair)
            change["written"] = True
            # Atualiza o config em memória do ctx para que mudanças seguintes
            # (e a checagem de convergência) vejam esta mudança. read-modify-
            # write no disco já garante persistência; isto sincroniza o ctx.
            try:
                from core.vt_config_loader import load_config
                config.clear()
                config.update(load_config(force=True))
            except Exception:
                pass
            log.info(f"APLICADO {pair}: {strategy} (cand R${cand_pnl:.2f} > base R${baseline_pnl:.2f})")
        except Exception as e:
            return _reject(cand, "write_error", str(e))
    else:
        change["written"] = False
        log.info(f"[DRY-RUN] {pair}: aplicaria {strategy} (cand R${cand_pnl:.2f} > base R${baseline_pnl:.2f})")

    return {"applied": True, "candidate": cand, "change": change,
            "gates_passed": ["profitability", "walk_forward", "regra1_simulated"]}


def _build_change(pair, strategy, params, full):
    return {
        "pair": pair, "strategy": strategy, "params": params,
        "backtest": {
            "pnl": full.get("total_pnl", 0),
            "pf": full.get("pf", 0),
            "wr": full.get("wr", 0),
            "n_trades": full.get("n_trades", 0),
            "sharpe": full.get("sharpe", 0),
            "max_dd": full.get("max_dd", 0),
        },
        "target": {
            "strategy_by_tf": {pair: strategy},
            "params_by_tf": {pair: params} if params else {},
        },
    }


def _write_to_config(config, change, pair):
    """Salva mudança no config. SEMPRE recarrega do disco antes (read-modify-write).

    BUG CORRIGIDO (2026-07-04): antes fazia deepcopy(config) do caller — mas
    o config em memória do ctx fica STALE entre mudanças do mesmo stage5.run().
    Resultado: aplicar WDO_M5 (salva no disco) e depois WDO_M30 (deepcopy do
    ctx VELHO sem WDO_M5) sobrescrevia o disco e APAGAVA a mudança de WDO_M5.
    Causa raiz do bug que deixou WDO_M5/M30 negativos no config final.

    Agora: load_config(force=True) busca sempre a versão mais recente do disco,
    aplica a mudança por cima, e salva. Cada mudança é atômica e incremental.

    Wave 875.G (2026-07-08): adicionado guardrail de write — toda chave passa
    por ``validate_write_target`` antes de tocar o config. Default-deny: target
    fora de SAFE_WRITE_TARGETS é rejeitado com log warning + gate="guardrail_reject"
    (Lei 1 + segurança contra regressão silenciosa do AGI).
    """
    from core.vt_config_loader import save_full_config, load_config
    # Wave 875.G — guardrail integration (ver optimization/agi_v4/guardrails.py)
    try:
        from optimization.agi_v4.guardrails import (
            GuardrailReject,
            validate_target_block,
        )
        _GUARDRAILS_AVAILABLE = True
    except ImportError:
        _GUARDRAILS_AVAILABLE = False

    # SEMPRE recarregar do disco — nunca confiar no config em memória do caller
    new_cfg = load_config(force=True)
    target = change.get("target", {})

    # Wave 875.G: validar ANTES de aplicar. Se violar guardrail, abort sem save.
    if _GUARDRAILS_AVAILABLE:
        try:
            validate_target_block(target, new_cfg)
        except GuardrailReject as exc:
            log.warning(
                f"AGI GUARDRAIL rejeitou {_format_target_for_log(target, pair)}: "
                f"{exc.reason}"
            )
            return  # não escreve nada; mantém estado anterior do disco

    for k, v in target.get("strategy_by_tf", {}).items():
        new_cfg.setdefault("strategy_by_tf", {})[k] = v
    for k, v in target.get("params_by_tf", {}).items():
        new_cfg.setdefault("params_by_tf", {}).setdefault(k, {}).update(v)

    # Wave AGI-soberano (Bruno 01/08): se o AGI validou que um par é lucrativo
    # (passou profitability + walk-forward + regra1), ele é SOBERANO para decidir
    # se o par opera. Remove o par de disabled_timeframes e ativa day_trade_intent.
    # Antes: o AGI otimizava a estratégia mas deixava o par bloqueado — pares
    # lucrativos (WSP/WDO após a correção de mult) ficavam sem operar. Agora:
    # estratégia lucrativa validada → par reativado automaticamente.
    pair_reactivated = []
    for pair_key in target.get("strategy_by_tf", {}):
        dt = new_cfg.get("disabled_timeframes", [])
        if pair_key in dt:
            dt = [x for x in dt if x != pair_key]
            new_cfg["disabled_timeframes"] = dt
            pair_reactivated.append(pair_key)
        dti = new_cfg.setdefault("day_trade_intent", {})
        if not dti.get(pair_key, False):
            dti[pair_key] = True
            if pair_key not in pair_reactivated:
                pair_reactivated.append(pair_key)
    if pair_reactivated:
        log.info(f"🔓 AGI-SOBERANO: reativou par(es) lucrativo(s): {pair_reactivated}")

    save_full_config(new_cfg, updated_by="agi_v4_stage5")


def _format_target_for_log(target: dict, pair: str) -> str:
    """Compacta o target para log — não vaza valores grandes."""
    parts = []
    for k, v in (target.get("strategy_by_tf") or {}).items():
        parts.append(f"strategy_by_tf[{k}]={v}")
    for k, v in (target.get("params_by_tf") or {}).items():
        sub = ",".join(f"{sk}={sv}" for sk, sv in v.items())
        parts.append(f"params_by_tf[{k}]{{{sub}}}")
    return f"{pair}({';'.join(parts) or 'noop'})"


def _maybe_promote_generated(strategy_name: str, cand: dict, dry_run: bool) -> str | None:
    """Se o candidato é uma estratégia gerada (em _pending/), promove para strategies/.

    O loader (vt_strategy_loader.py:68-69) ignora arquivos _-prefixed, então
    uma estratégia em _pending/ nunca é carregada no runtime. Para que o
    config update funcione (strategy_by_tf aponta para ela), o arquivo .py
    precisa ser movido para strategies/ com nome NÃO _-prefixed.

    Detecção: o stage4 marca candidatos gerados com cand["generated"]=True e
    cand["pending_path"]. Também procuramos em _pending/ por nome.

    Returns:
        Caminho promovido (str) se promoveu, None se não era gerada.
    """
    if not cand.get("generated") and not cand.get("pending_path"):
        return None  # estratégia existente, nada a promover

    from pathlib import Path
    import shutil

    project_root = Path(__file__).resolve().parent.parent.parent
    pending_dir = project_root / "strategies" / "_pending"
    strategies_dir = project_root / "strategies"

    # Achar o arquivo fonte em _pending/
    pending_path = cand.get("pending_path")
    if pending_path:
        src = Path(pending_path)
    else:
        # Procurar por nome da estratégia
        src = None
        if pending_dir.exists():
            for f in pending_dir.glob("*.py"):
                try:
                    content = f.read_text(encoding="utf-8")
                    if f'STRATEGY_NAME = "{strategy_name}"' in content:
                        src = f
                        break
                except Exception:
                    continue

    if src is None or not src.exists():
        log.warning(f"promoção {strategy_name}: arquivo não encontrado em _pending/")
        return None

    # Nome de destino: nome da estratégia em lowercase .py (sem underscore)
    dest = strategies_dir / f"{strategy_name.lower()}.py"

    if dry_run:
        log.info(f"[DRY-RUN] promoveria {src.name} -> {dest.name}")
        return str(dest)

    try:
        shutil.move(str(src), str(dest))
        log.info(f"⬆️ PROMOVIDO: {src.name} -> strategies/{dest.name}")
        return str(dest)
    except Exception as e:
        log.error(f"promoção {strategy_name} falhou: {e}")
        return None


def _reject(cand, gate, reason):
    log.info(f"REJEITADO {cand.get('pair','')} {cand.get('strategy','')}: {gate} {reason[:80]}")
    return {"applied": False, "candidate": cand, "gate": gate, "reason": reason}


def _normalize_pairs(pairs) -> list[str]:
    """Normaliza lista de pares (list[str] ou list[dict]) para list[str].

    ctx["failing_pairs"] e ctx["profitable_pairs"] podem vir em dois formatos:
      - list[str] (do stage1): ["WIN_H1", "BIT_H1"]
      - list[dict] (do _check_convergence_simulated): [{"pair": "WIN_H1", "pnl": ...}]
    Este helper extrai sempre o nome do par (str) para operações de set/list
    que exigem pares hashable (ex.: ``p in disabled_timeframes``).
    Wave 881 (03/08/2026): corrige TypeError unhashable type: 'dict' que
    quebrava o Stage 5 em dry-run quando failing_pairs vinha como dicts.
    """
    result = []
    for p in pairs or []:
        if isinstance(p, str):
            if p:
                result.append(p)
        elif isinstance(p, dict):
            name = p.get("pair", "")
            if name:
                result.append(name)
    return result
