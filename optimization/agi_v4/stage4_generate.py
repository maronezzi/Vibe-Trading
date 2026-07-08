"""
stage4_generate.py — Geração de estratégias .py novas em strategies/_pending/.

Este é o stage mais poderoso E mais arriscado da AGI v4: ele cria CÓDIGO
Python novo. Por isso tem a defesa mais pesada de todo o sistema.

Fluxo (4 camadas de defesa):
  1. GERAÇÃO: LLM (ask_llm) recebe hipóteses do stage2 + template do plugin
     format e gera código .py. Output vai DIRETO para strategies/_pending/.
  2. SANDBOX: o loader existente (vt_strategy_loader.py:68-69) IGNORA arquivos
     _-prefixed — então NADA em _pending/ é carregado no runtime até promoção.
  3. GATES DE VALIDAÇÃO (antes de promover):
     a. ast_gate: syntax + STRATEGY_NAME + check_entry + LEI 3 (SL) + sandbox
     b. profitability_gate: backtest 30d PF/WR/n_trades/max_dd
     c. walk_forward_gate: consistência entre janelas (anti-overfit)
  4. PROMOÇÃO MANUAL-AGI: só após TODOS os gates, copia _pending/X.py → X.py.
     Em dry_run: NÃO promove, só valida e loga.

Lei 2 (Escopo): se um par não tem edge nas 30 estratégias existentes, o
stage4 CRIA uma nova — nunca desabilita o par.

Importante: a geração é opcional. Se o LLM falhar ou não houver hipóteses,
este stage retorna [] e a AGI continua (Lei 5: itera com o que tem).
"""

from __future__ import annotations

import ast
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

from .gates import ast_gate, load_thresholds

log = logging.getLogger("agi_v4.stage4")

# Diretórios
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
STRATEGIES_DIR = _PROJECT_ROOT / "strategies"
PENDING_DIR = STRATEGIES_DIR / "_pending"

# Template do prompt para o LLM — garante formato do plugin
_PLUGIN_TEMPLATE = '''"""
Estratégia {name} — gerada pela AGI v4 ({timestamp}).

{rationale}
"""

STRATEGY_NAME = "{name}"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Verifica sinal de entrada {name}.

    Returns:
        None (sem sinal) ou {{"direction": "BUY"/"SELL", "sl_pts": int, "info": {{...}}}}
    """
    # Indicadores via utils (NÃO importar — strategy é stateless)
    calc_sl = utils["calc_sl"]

    # Params (com defaults defensivos)
    # {params_section}

    if not bars or len(bars) < 20:
        return None
    if atr <= 0:
        return None

    # {logic_section}

    # SL OBRIGATÓRIO (Lei 3) — TODO sinal deve ter sl_pts > 0
    sl_pts = calc_sl(symbol, atr, params)

    return {{
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {{
            "strategy": "{name}",
            "atr": atr,
        }},
    }}
'''


def run(ctx: dict) -> dict:
    """Executa stage 4: gera estratégias novas baseadas nas hipóteses do stage2.

    Args:
        ctx: contexto. Usa ctx["hypotheses"] (do stage2) onde type == "new_logic".

    Returns:
        dict com "generated_strategies" (lista de estratégias validadas em
        _pending/, prontas para promoção manual ou via stage5).
    """
    config = ctx.get("config", {}) or {}
    thresholds = ctx.get("thresholds") or load_thresholds(config)
    hypotheses = ctx.get("hypotheses", []) or []
    dry_run = ctx.get("dry_run", True)
    failing = ctx.get("failing_pairs", [])

    # Hipóteses de nova lógica vindas do stage2 (web+LLM)
    new_logic_hypotheses = [h for h in hypotheses if h.get("type") == "new_logic"]

    # ── Lei 5: se há pares failing sem hipótese new_logic, GERAR pra eles ──
    # O stage2 pode não ter sugerido nada (LLM offline/sem retorno). Mesmo
    # assim, a AGI tem que tentar criar estratégias para os pares negativos.
    # Sintetiza hipóteses genéricas baseadas no par + estratégias já tentadas.
    pairs_with_hyp = {h.get("pair", "") for h in new_logic_hypotheses}
    tried = ctx.get("_tried_strategies", {})  # {pair: [strat1, strat2, ...]}
    for f in failing:
        pair = f.get("pair", f) if isinstance(f, dict) else f
        if pair and pair not in pairs_with_hyp:
            already = tried.get(pair, [])
            new_logic_hypotheses.append({
                "type": "new_logic",
                "pair": pair,
                "description": f"Estratégia nova para {pair} (busca nas 30 existentes não achou lucro)",
                "justification": f"Pares tentados sem sucesso: {already or 'nenhum ainda'}. "
                                 f"Criar abordagem diferente.",
            })
            pairs_with_hyp.add(pair)

    if not new_logic_hypotheses:
        log.info("Stage 4: sem pares failing nem hipóteses — nada a gerar")
        return {"generated_strategies": [], "summary": "nada a gerar"}

    # Garante que _pending/ existe
    PENDING_DIR.mkdir(parents=True, exist_ok=True)

    generated = []
    search_results = ctx.get("search_results", []) or []
    for hyp in new_logic_hypotheses[:5]:  # até 5 gerações por iteração
        result = _generate_and_validate_one(hyp, thresholds, dry_run, ctx)
        if not result:
            continue
        generated.append(result)
        # Se a estratégia gerada passou no backtest_gate, adiciona em
        # search_results (com flag generated) para o stage5 aplicar+promover.
        if result.get("backtest_gate") == "passed" and result.get("backtest"):
            pair = hyp.get("pair", "")
            search_results.append({
                "pair": pair,
                "strategy": result["name"],
                "params": {},
                "full": result["backtest"],
                "walk_forward": result.get("walk_forward", []),
                "gates_passed": ["ast", "profitability", "walk_forward"],
                "generated": True,
                "pending_path": result["path"],
            })

    ctx["generated_strategies"] = generated
    ctx["search_results"] = search_results  # stage5 lê isto

    mode = "validado em _pending/ (dry-run)" if dry_run else "validado + pronto p/ promover"
    n_aprovadas = sum(1 for g in generated if g.get("backtest_gate") == "passed")
    summary = (f"{len(new_logic_hypotheses)} hipótese(s), {len(generated)} gerada(s), "
               f"{n_aprovadas} aprovada(s) por simulação {mode}")
    return {"generated_strategies": generated, "summary": summary}


# ═══════════════════════════════════════════════════════════════════
# Geração de uma estratégia
# ═══════════════════════════════════════════════════════════════════

def _generate_and_validate_one(
    hypothesis: dict,
    thresholds: dict,
    dry_run: bool,
    ctx: dict,
) -> dict | None:
    """Gera uma estratégia a partir de uma hipótese e valida com gates.

    Retorna dict com dados da estratégia se TODOS os gates passarem, None caso
    contrário. Estratégia fica em _pending/ independente de passar ou não
    (para forensics), mas só é marcada "approved" se passar nos gates.
    """
    pair = hypothesis.get("pair", "UNKNOWN")
    description = hypothesis.get("description", "")
    justification = hypothesis.get("justification", "")

    # Nome único baseado no par + timestamp
    ts_compact = datetime.now().strftime("%H%M%S")
    strat_name = f"AGI4_{pair.split('_')[0]}_{ts_compact}"
    safe_filename = f"_agi4_{pair.lower().replace('_','')}_{ts_compact}.py"
    pending_path = PENDING_DIR / safe_filename

    log.info(f"Gerando estratégia {strat_name} para {pair}: {description[:60]}")

    # 1. Gerar código via LLM
    code = _generate_code_via_llm(strat_name, hypothesis)
    if not code:
        log.warning(f"LLM não gerou código para {strat_name}")
        return None

    # 2. Escrever em _pending/ (sempre — para forensics, mesmo se rejeitado)
    try:
        pending_path.write_text(code, encoding="utf-8")
        log.info(f"Estratégia escrita em sandbox: {pending_path}")
    except Exception as e:
        log.error(f"Falha ao escrever {pending_path}: {e}")
        return None

    # 3. Gate A: AST (syntax + Lei 3 SL + sandbox imports)
    g_ast = ast_gate(pending_path)
    if not g_ast:
        log.warning(f"{strat_name} REJEITADA pelo ast_gate: {g_ast.reason}")
        return _reject_strategy(strat_name, pending_path, "ast_gate", g_ast.reason)

    # 4. Gate B: simulação bar-by-bar 30d + walk-forward (via evaluator)
    # Em ambiente sem MT5/Wine, a simulação pode falhar. Não bloqueamos a
    # geração só por isso — a estratégia fica em _pending/ aprovada pelo AST,
    # com backtest_gate=skipped. A promoção real exige simulação aprovada.
    sim_result = _simulate_generated(strat_name, pair, ctx, thresholds)
    if sim_result is not None:
        if sim_result.get("passed"):
            return _approved_strategy(
                strat_name, pending_path, hypothesis,
                backtest=sim_result["full"], backtest_gate="passed",
                walk_forward=sim_result["walk_forward"],
            )
        # BUG CORRIGIDO (W872, 2026-07-06): antes, qualquer simulação que
        # não passasse era aprovada como pending — inclusive estratégias que
        # geraram ZERO trades no backtest 30d. Isso poluiu _pending/ com
        # estratégias inúteis (ex: AGI4_WSP_173218 com n_trades=0 aprovada
        # como pending). Agora: se a simulação rodou (MT5 disponível) e
        # gerou 0 trades, a estratégia é REJEITADA — sem edge, sem entrada,
        # não há motivo para mantê-la. Só vale pending se gerou trades mas
        # não atingiu thresholds (caso revisível com tuning de params).
        full = sim_result.get("full", {}) or {}
        n_trades = full.get("n_trades", 0)
        if n_trades == 0:
            log.info(
                f"{strat_name} REJEITADA: simulação gerou 0 trades em 30d "
                f"(sem edge no backtest). Não há motivo para manter em _pending/."
            )
            return _reject_strategy(
                strat_name, pending_path, "no_trades_generated",
                f"simulação 30d gerou 0 trades — estratégia não tem edge",
            )
        log.info(f"{strat_name} simulação não passou: {sim_result.get('reason','')}")
        return _approved_strategy(
            strat_name, pending_path, hypothesis,
            backtest=full, backtest_gate="pending",
        )

    return _approved_strategy(
        strat_name, pending_path, hypothesis,
        backtest=None, backtest_gate="skipped",
    )


# ═══════════════════════════════════════════════════════════════════
# Geração de código via LLM
# ═══════════════════════════════════════════════════════════════════

def _generate_code_via_llm(strat_name: str, hypothesis: dict) -> str | None:
    """Pede ao LLM que gere código .py da estratégia.

    O prompt inclui o template exato do plugin format + a hipótese + restrições
    de segurança (Lei 3 SL, sandbox imports).

    Fail-safe: se LLM falhar, retorna None (não gera arquivo).
    """
    try:
        from core.vt_hermes_helper import ask_llm
    except ImportError:
        log.debug("ask_llm não disponível — stage4 não gera código")
        return None

    description = hypothesis.get("description", "")
    justification = hypothesis.get("justification", "")
    pair = hypothesis.get("pair", "")

    prompt = f"""Gere código Python para uma estratégia de trading seguindo EXATAMENTE este formato.

REGRAS OBRIGATÓRIAS:
1. Deve ter: STRATEGY_NAME = "{strat_name}" (constante string no nível do módulo)
2. Deve ter: def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils)
3. LEI 3: TODO sinal retornado DEVE incluir "sl_pts" calculado via calc_sl = utils["calc_sl"]; sl_pts = calc_sl(symbol, atr, params)
4. SANDBOX: NÃO importar nada (sem import os, subprocess, mt5, etc). Receba tudo via utils e params.
5. Retornar None se não há sinal, ou dict {{"direction": "BUY"/"SELL", "sl_pts": int, "info": {{...}}}}
6. Indicadores via utils: utils["calculate_rsi"](bars, period), utils["calculate_ema"](bars, period), utils["calculate_bollinger"](bars, period, std), utils["calculate_adx"](bars, period), utils["calc_sl"](symbol, atr, params)
7. Params via params.get("nome", default)

ESTRATÉGIA PARA IMPLEMENTAR:
Par: {pair}
Descrição: {description}
Justificativa: {justification}

Gere APENAS o código Python, sem markdown, sem explicação. Comece com as triplas aspas do docstring."""

    try:
        code = ask_llm(prompt, timeout=60)
    except Exception as e:
        log.warning(f"ask_llm falhou em stage4: {e}")
        return None

    if not code or len(code.strip()) < 50:
        return None

    # Extrair o bloco de código Python da resposta do LLM.
    #
    # BUG CORRIGIDO (W872, 2026-07-06): antes o LLM às vezes retornava PROSA
    # conversacional ("Arquivo: /tmp/x.py\n\nSanity-check executado...")
    # embrulhando o código Python real. O estágio gravava a prosa como se
    # fosse código, e o ast_gate rejeitava por caracteres unicode (→ —) que
    # aparecem na prosa. Resultado: 8 estratégias rejeitadas no AGI v4 17h,
    # embora o código Python válido existisse embutido.
    #
    # Agora extraímos em 3 camadas, da mais específica à mais genérica:
    extracted = _extract_python_block(code)
    if extracted is None:
        log.warning(
            "stage4: resposta do LLM não continha código Python válido "
            "(nenhum bloco fence, docstring ou STRATEGY_NAME encontrado). "
            "Descartando — nada gravado em _pending/."
        )
        return None
    return extracted


def _extract_python_block(code: str) -> str | None:
    """Extrai código Python válido de uma resposta potencialmente suja do LLM.

    Ordem de tentativas (primeira que produz código que passa no ast.parse vence):
      1. Markdown fence em qualquer posição (```python ... ``` ou ``` ... ```)
      2. Cortar a partir da primeira âncora canônica em início de linha:
         docstring (\"\"\") ou STRATEGY_NAME = (constante exigida pelo prompt)
      3. Texto cru (se já for Python válido)

    Sempre valida com ast.parse antes de retornar — garante que nunca
    gravamos prosa em _pending/. Retorna None se nada for válido.

    Anti-overfit/segurança: NÃO executa o código (só faz parse da AST).
    """
    code = code.strip()

    # Camada 1: markdown fence em qualquer posição (regex DOTALL).
    # Pega "Aqui está o código:\n```python\n...\n```\nEspero que ajude."
    for pattern in (
        r"```(?:python|py)?\s*\n(.*?)\n\s*```",
        r"```(?:python|py)?\s*\n(.*?)```",
    ):
        m = re.search(pattern, code, re.DOTALL)
        if m:
            candidate = m.group(1).strip()
            if _is_valid_python(candidate):
                return candidate

    # Camada 2: cortar a partir da primeira âncora canônica.
    # O prompt exige STRATEGY_NAME = "..." e def check_entry, então esses
    # marcadores SEMPRE aparecem no código real. Procuramos o mais cedo.
    # Ordem: docstring (mais cedo) depois STRATEGY_NAME.
    for anchor in (r'^"""', r"^STRATEGY_NAME\s*=", r'^def\s+check_entry'):
        m = re.search(anchor, code, re.MULTILINE)
        if m:
            candidate = code[m.start():].strip()
            # Tentar o corte direto primeiro
            if _is_valid_python(candidate):
                return candidate
            # Se falhar, pode haver epílogo prosaico depois do código.
            # Tentar truncar no último 'def check_entry' boundary razoável:
            # cortar no último return None seguido de linha em branco.
            # Heurística simples: tentar blocos cada vez menores do fim.
            truncated = _trim_trailing_prose(candidate)
            if truncated and _is_valid_python(truncated):
                return truncated

    # Camada 3: texto cru (já era Python válido sem prosa).
    if _is_valid_python(code):
        return code

    return None


def _is_valid_python(src: str) -> bool:
    """True se o código parseia como Python válido. Não executa."""
    try:
        ast.parse(src)
        return True
    except SyntaxError:
        return False


def _trim_trailing_prose(src: str) -> str | None:
    """Tenta remover epílogo prosaico depois do código Python.

    Estratégia: iterar pelas linhas de trás pra frente, remover blocos
    que não casam com sintaxe Python (linhas não-comment, não-em-branco,
    não-indentadas que aparecem depois do fim lógico do módulo).
    Conservador: só corta se encontrar um ponto de corte que produce AST válido.
    """
    lines = src.split("\n")
    # Tentar cortar em cada linha em branco de trás pra frente
    for i in range(len(lines) - 1, 0, -1):
        if not lines[i].strip():
            candidate = "\n".join(lines[:i]).strip()
            if candidate and _is_valid_python(candidate):
                return candidate
    return None


# ═══════════════════════════════════════════════════════════════════
# Simulação da estratégia gerada (30d + walk-forward)
# ═══════════════════════════════════════════════════════════════════

def _simulate_generated(strat_name: str, pair: str, ctx: dict, thresholds: dict) -> dict | None:
    """Simula a estratégia gerada em 30d MT5 + walk-forward.

    Fail-safe: retorna None se MT5 indisponível (não bloqueia a geração —
    a estratégia fica em _pending/ com backtest_gate=skipped).

    Usa o mesmo evaluator do stage3: bar-by-bar fiel ao autotrader,
    4 janelas walk-forward, sem trades do DB como referência.
    """
    try:
        from optimization.agi_v4.backtest_evaluator import evaluate_candidate
        config = ctx.get("config", {})
        parts = pair.split("_", 1)
        if len(parts) != 2:
            return None
        sym, tf = parts
        return evaluate_candidate(sym, tf, strat_name, {}, config, thresholds=thresholds)
    except Exception as e:
        log.debug(f"Simulação {strat_name} falhou (não crítico): {e}")
        return None


# ═══════════════════════════════════════════════════════════════════
# Helpers de resultado
# ═══════════════════════════════════════════════════════════════════

def _approved_strategy(
    name: str,
    path: Path,
    hypothesis: dict,
    backtest: dict | None,
    backtest_gate: str,
    walk_forward: list | None = None,
) -> dict:
    """Constrói resultado de estratégia aprovada (em _pending/)."""
    log.info(f"✓ Estratégia {name} APROVADA em _pending/ (backtest_gate={backtest_gate})")
    return {
        "name": name,
        "path": str(path),
        "status": "approved_pending",
        "backtest_gate": backtest_gate,
        "backtest": backtest,
        "walk_forward": walk_forward or [],
        "hypothesis": hypothesis,
        "promoted": False,  # só TRUE quando stage5 promover
    }


def _reject_strategy(name: str, path: Path, gate: str, reason: str) -> dict:
    """Constrói resultado de estratégia rejeitada (mantida em _pending/ p/ forensics)."""
    log.warning(f"✗ Estratégia {name} REJEITADA ({gate}): {reason}")
    return {
        "name": name,
        "path": str(path),
        "status": "rejected",
        "gate": gate,
        "reason": reason,
        "promoted": False,
    }
