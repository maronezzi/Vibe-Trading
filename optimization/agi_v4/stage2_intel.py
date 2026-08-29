"""
stage2_intel.py — Inteligência externa: WebSearch + LLM gera hipóteses.

Pipeline do stage:
  1. Para cada par perdedor (do stage1), busca web por estratégias/thresholds
     conhecidos para aquele símbolo/regime (fatoss reais, sem alucinação)
  2. Envia os fatos web + dados do DB + estratégia atual para o LLM (ask_llm
     via hermes), pedindo variações de params ou novas lógicas
  3. Coleta as hipóteses em ctx["hypotheses"] para o stage3 testar

Importante: hipóteses NUNCA são aplicadas diretamente. Elas só sugerem o
QUE testar — o stage3 valida cada uma via backtest + gates. Web/LLM é
inspiração, não autoridade (Lei 4: broker-truth é autoritativo).

Fail-safe: se web cair ou LLM alucinar, stage2 retorna [] e a AGI continua
só com busca exaustiva (stage3) sobre as 30 estratégias existentes.
"""

from __future__ import annotations

import json
import logging

from . import web_search

log = logging.getLogger("agi_v4.stage2")

# Mapeia símbolo root → termos de busca relevantes (B3 futures)
_SYMBOL_SEARCH_TERMS = {
    "WIN": ["mini index B3 futures strategy", "WIN futures day trading backtest",
            "IBOVESPA mini contract RSI strategy"],
    "WDO": ["mini dollar B3 futures strategy", "WDO futures day trading",
            "dollar index DXY mean reversion backtest"],
    "BIT": ["bitcoin futures day trading strategy", "BTC futures RSI scalping",
            "crypto futures volatility breakout backtest"],
    "WSP": ["S&P 500 mini futures strategy", "ES futures EMA crossover backtest"],
}

# Mapeia estratégia → nome amigável para o prompt do LLM
_STRATEGY_LABELS = {
    "RSI_REVERSION": "RSI Reversion (mean reversion)",
    "BOLLINGER": "Bollinger Bands",
    "EMA_CROSSOVER": "EMA Crossover",
    "VWAP": "VWAP",
    "STRONG_TREND": "ADX-based trend following",
    "MACD_MOMENTUM": "MACD Momentum",
    "SMART_EMA": "Smart EMA",
}


def run(ctx: dict) -> dict:
    """Executa stage 2: web search + LLM → hipóteses.

    Args:
        ctx: contexto do pipeline. Usa:
            - ctx["failing_pairs"]: pares perdedores alvo
            - ctx["performance"]: dados do DB (PnL, WR, streak)
            - ctx["config"]: config (strategy_by_tf, params_by_tf)

    Returns:
        dict com "hypotheses" (lista de sugestões para stage3 testar).
    """
    config = ctx.get("config", {}) or {}
    performance = ctx.get("performance", {})
    failing = ctx.get("failing_pairs", [])

    # Normaliza failing pairs para list[str]
    target_pairs = []
    for f in failing:
        if isinstance(f, str):
            target_pairs.append(f)
        elif isinstance(f, dict):
            target_pairs.append(f.get("pair", ""))
    target_pairs = [p for p in target_pairs if p]

    if not target_pairs:
        log.info("Stage 2: sem pares perdedores — sem hipóteses a gerar")
        return {"hypotheses": [], "summary": "sem pares alvo"}

    # Para cada par perdedor: web search + LLM
    hypotheses = []
    web_results_total = 0

    # Limita a top 3 pares perdedores (evita excesso de buscas web/LLM)
    for pair in target_pairs[:3]:
        sym_root = pair.split("_", 1)[0] if "_" in pair else pair

        # 1. Web search — fatos sobre o símbolo/estratégia
        web_results = _search_for_pair(pair, sym_root, config)
        web_results_total += len(web_results)

        # 2. LLM — síntese com base nos fatos web + dados DB
        llm_hypotheses = _ask_llm_for_hypotheses(
            pair, sym_root, web_results, performance, config
        )

        hypotheses.extend(llm_hypotheses)

    ctx["hypotheses"] = hypotheses

    summary = (f"web: {web_results_total} resultados, "
               f"LLM: {len(hypotheses)} hipóteses para stage3 testar")
    return {"hypotheses": hypotheses, "summary": summary}


def _format_winners_for_prompt(performance: dict) -> str:
    """Top setups lucrativos do período para cross-pollination.

    Wave AGI-super (Bruno 13/08): alimenta o prompt do LLM com o que JÁ
    FUNCIONA no portfólio, para as hipóteses adaptarem lógica vencedora
    aos pares perdedores em vez de partir do zero.
    """
    by_tf = (performance or {}).get("by_symbol_tf", {}) or {}
    winners = []
    for p, d in by_tf.items():
        if not isinstance(d, dict):
            continue
        pnl = d.get("total_pnl") or 0
        n = d.get("n_trades") or 0
        if pnl > 0 and n >= 5:
            winners.append((p, d.get("strategy", "?"), pnl,
                            d.get("win_rate", 0), n))
    winners.sort(key=lambda x: -x[2])
    if not winners:
        return "(nenhum setup lucrativo com trades suficientes no período)"
    lines = []
    for p, strat, pnl, wr, n in winners[:6]:
        lines.append(f"- {p}: {strat} | R$ {pnl:+.0f} | WR {wr:.0f}% ({n} trades)")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# Web search por par
# ═══════════════════════════════════════════════════════════════════

def _search_for_pair(pair: str, sym_root: str, config: dict) -> list[dict]:
    """Busca web por estratégias/thresholds para o símbolo.

    Combina termo do símbolo + estratégia atual do par.
    """
    terms = _SYMBOL_SEARCH_TERMS.get(sym_root, [f"{sym_root} futures trading strategy"])

    # Adiciona contexto da estratégia atual
    strategy_by_tf = config.get("strategy_by_tf", {})
    current_strat = strategy_by_tf.get(pair, "")
    if current_strat:
        strat_label = _STRATEGY_LABELS.get(current_strat, current_strat)
        terms = terms[:1] + [f"{strat_label} futures backtest parameters"]

    all_results = []
    for term in terms[:2]:  # max 2 buscas por par
        results = web_search.search(term, max_results=3)
        all_results.extend(results)

    return all_results


# ═══════════════════════════════════════════════════════════════════
# LLM — síntese de hipóteses
# ═══════════════════════════════════════════════════════════════════

def _ask_llm_for_hypotheses(
    pair: str,
    sym_root: str,
    web_results: list[dict],
    performance: dict,
    config: dict,
) -> list[dict]:
    """Pede ao LLM (via hermes ask_llm) hipóteses baseadas em fatos web + DB.

    Retorna lista de hipóteses: [{"type": "param_variation"|"new_logic",
                                  "pair", "description", "source": "web+llm"}]

    Fail-safe: se LLM falhar ou retornar lixo, retorna [].
    """
    try:
        from core.vt_hermes_helper import ask_llm
    except ImportError:
        # Wave 875.0: este caminho só ocorre se vt_hermes_helper.ask_llm for
        # removido por regressão. Se cair aqui, é bug — alerta explícito.
        log.warning(
            "ask_llm não disponível em vt_hermes_helper — stage2 sem hipóteses LLM. "
            "Regressão? Ver Wave 875.0 fix-llm-bridge."
        )
        return []  # Fallback silencioso era o problema raiz — Wave 875.0 corrige


    # Construir prompt com fatos web + dados DB
    web_summary = _format_web_for_prompt(web_results)
    db_summary = _format_db_for_prompt(pair, performance)
    current_config = _format_config_for_prompt(pair, config)
    # Wave AGI-super (Bruno 13/08): cross-pollination — o LLM recebe os setups
    # vencedores do portfólio para ADAPTAR lógica vencedora ao par perdedor,
    # em vez de inventar do zero (as vencedoras já passaram pelo gate de 30d).
    winners_summary = _format_winners_for_prompt(performance)

    prompt = f"""Você é um analista quantitativo sênior de futuros B3. Analise este par que está perdendo e proponha melhorias.

PAR: {pair} ({sym_root} — contrato B3 futures)
ESTRATÉGIA ATUAL: {current_config}

DADOS REAIS (últimos 7 dias do banco de dados broker-reconciled):
{db_summary}

SETUPS VENCEDORES ATUAIS DO PORTFÓLIO (referência — adapte a lógica vencedora
deles para este par; não invente do zero o que já funciona em outro par):
{winners_summary}

PESQUISA WEB (fatos confirmados sobre estratégias para este ativo):
{web_summary}

Com base NOS FATOS acima (não invente), proponha ATÉ 3 melhorias. Para cada uma:
1. Tipo: "param_variation" (ajustar params da estratégia atual) ou "new_logic" (estratégia diferente)
2. Descrição objetiva do que mudar — para "new_logic", descreva a TESE do setup
   (gatilho + filtro de tendência + gate de regime/volatilidade) com detalhes
   suficientes para outro analista implementar (indicadores, condições, direção)
3. Justificativa citando o fato web, dado DB ou setup vencedor que sustenta

PRIORIZE hipóteses que adaptem padrões dos setups vencedores listados acima.

Responda APENAS em JSON válido, sem markdown:
{{"hypotheses": [{{"type": "...", "description": "...", "justification": "..."}}]}}"""

    try:
        # Wave noturno-generoso (Bruno 01/08): AGI roda às 17:10 com a madrugada.
        # Mesmo hipótese curta timed out em 44s (cold-start do qwen). Budget 120s.
        response = ask_llm(prompt, timeout=120)
    except Exception as e:
        log.warning(f"ask_llm falhou para {pair}: {e}")
        return []

    if not response:
        log.debug(f"LLM sem resposta para {pair}")
        return []

    return _parse_llm_hypotheses(response, pair)


def _parse_llm_hypotheses(response: str, pair: str) -> list[dict]:
    """Parse da resposta JSON do LLM. Robusto a markdown/lixo ao redor.

    Extrai o JSON mesmo se o LLM cercar com ```json ... ```.
    """
    # Tentar extrair JSON de dentro da resposta (LLM pode cercar com markdown)
    json_str = response.strip()

    # Remover markdown code fences se presentes
    if json_str.startswith("```"):
        json_str = re.sub(r"^```(?:json)?\s*", "", json_str)
        json_str = re.sub(r"\s*```$", "", json_str)

    # Encontrar primeiro { e último }
    start = json_str.find("{")
    end = json_str.rfind("}")
    if start == -1 or end == -1:
        log.debug(f"LLM resposta sem JSON para {pair}: {response[:100]}")
        return []

    try:
        parsed = json.loads(json_str[start:end + 1])
    except json.JSONDecodeError as e:
        log.debug(f"LLM JSON inválido para {pair}: {e}")
        return []

    hypotheses = []
    for h in parsed.get("hypotheses", []):
        if not isinstance(h, dict):
            continue
        hypotheses.append({
            "type": h.get("type", "unknown"),
            "pair": pair,
            "description": h.get("description", ""),
            "justification": h.get("justification", ""),
            "source": "web+llm",
        })
    return hypotheses


# ═══════════════════════════════════════════════════════════════════
# Formatação de contexto para o prompt
# ═══════════════════════════════════════════════════════════════════

def _format_web_for_prompt(web_results: list[dict]) -> str:
    """Formata resultados web para o prompt do LLM."""
    if not web_results:
        return "(nenhum resultado web encontrado)"
    lines = []
    for i, r in enumerate(web_results[:5], 1):
        title = r.get("title", "")[:80]
        snippet = r.get("snippet", "")[:150]
        lines.append(f"{i}. {title}\n   {snippet}")
    return "\n".join(lines)


def _format_db_for_prompt(pair: str, performance: dict) -> str:
    """Formata dados do DB para o prompt do LLM."""
    by_tf = performance.get("by_symbol_tf", {}) if isinstance(performance, dict) else {}
    stats = by_tf.get(pair, {})
    if not stats:
        return f"(sem dados DB para {pair})"

    return (f"- Trades: {stats.get('n_trades', 0)}\n"
            f"- Win Rate: {stats.get('win_rate', 0)}%\n"
            f"- PnL total: R$ {stats.get('total_pnl', 0)}\n"
            f"- PnL médio/trade: R$ {stats.get('avg_pnl', 0)}\n"
            f"- Estratégia: {stats.get('strategy', '?')}")


def _format_config_for_prompt(pair: str, config: dict) -> str:
    """Formata config atual do par para o prompt do LLM."""
    strategy = config.get("strategy_by_tf", {}).get(pair, "?")
    params = config.get("params_by_tf", {}).get(pair, {})

    if params:
        params_str = ", ".join(f"{k}={v}" for k, v in list(params.items())[:6])
        return f"{strategy} ({params_str})"
    return strategy


# Import tardio para evitar circular import (re usado só no parse)
import re
