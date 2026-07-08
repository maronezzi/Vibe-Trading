"""
web_search.py — Wrapper de busca web para a AGI v4.

Busca fatos reais (estudos de backtest, thresholds de indicadores, estratégias
conhecidas) para alimentar o LLM com contexto fundamentado — evita alucinação.

Implementação: DuckDuckGo HTML search via requests (sem API key, sem login).
Por que DuckDuckGo: gratuito, sem rate-limit agressivo para uso leve (algumas
buscas/dia pela AGI), retorna resultados reais dos EUA.

Fail-safe em TODOS os níveis: se a web cair, se o DuckDuckGo bloquear, se o
parse falhar — retorna [] e a AGI continua sem web (só com backtest stage3).
A web é BÔNUS, não dependência.

Cache: resultados cacheados por query em memória (TTL = duração da execução).
Não persiste cache em disco (cada execução da AGI é isolada).
"""

from __future__ import annotations

import logging
import re
from html import unescape
from typing import Any
from urllib.parse import quote_plus

log = logging.getLogger("agi_v4.web_search")

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False
    log.debug("requests não disponível — web_search retorna []")

# Cache em memória (chave = query)
_cache: dict[str, list[dict]] = {}

# User-Agent — sem ele, DuckDuckGo pode bloquear. Identifica como ferramenta
# de pesquisa de estratégias (uso legítimo, não scraping em massa).
_USER_AGENT = (
    "Mozilla/5.0 (compatible; VibeTradingAGI/4.0; +https://github.com/agi-research)"
)

# Timeout conservador — web não pode travar a AGI
_TIMEOUT = 10


def search(query: str, max_results: int = 5) -> list[dict]:
    """Busca no DuckDuckGo e retorna resultados estruturados.

    Args:
        query: termo de busca (ex: "RSI reversion backtest mini index").
        max_results: limite de resultados.

    Returns:
        Lista de dicts: [{"title", "url", "snippet"}]. Vazio se falhar.

    Fail-safe: qualquer erro retorna []. Nunca levanta.
    """
    if not _HAS_REQUESTS:
        return []

    query = query.strip()
    if not query:
        return []

    # Cache hit?
    cache_key = f"{query}:{max_results}"
    if cache_key in _cache:
        log.debug(f"web_search cache hit: {query[:50]}")
        return _cache[cache_key]

    results = _ddg_html_search(query, max_results)
    _cache[cache_key] = results
    log.info(f"web_search '{query[:50]}': {len(results)} resultados")
    return results


def _ddg_html_search(query: str, max_results: int) -> list[dict]:
    """Parse do HTML do DuckDuckGo (html.duckduckgo.com).

    DuckDuckGo não tem API key. O endpoint HTML retorna resultados parseáveis.
    """
    url = "https://html.duckduckgo.com/html/"
    data = {"q": query, "b": ""}  # b=vazio desliga bootstrap redirect
    headers = {"User-Agent": _USER_AGENT}

    try:
        resp = requests.post(url, data=data, headers=headers, timeout=_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.debug(f"web_search request falhou: {e}")
        return []

    return _parse_ddg_html(resp.text, max_results)


def _parse_ddg_html(html: str, max_results: int) -> list[dict]:
    """Extrai título, URL, snippet do HTML do DuckDuckGo.

    DuckDuckGo HTML tem estrutura estável com div.result. Usamos regex
    simples (sem BeautifulSoup para evitar dependência). Robusto o suficiente
    para o uso leve da AGI.
    """
    results = []

    # Padrões do DDG HTML: cada resultado tem <a class="result__a" href="...">title</a>
    # e <a class="result__snippet">snippet</a>
    # href é redirect (//duckduckgo.com/l/?uddg=ENCODED_URL) — extraímos uddg.

    # Bloco de resultado (tudo entre result__a e próximo result__url)
    result_blocks = re.split(r'class="result ', html)[1:]

    for block in result_blocks[:max_results * 2]:  # margem p/ descartes
        if len(results) >= max_results:
            break

        # Título + URL
        title_match = re.search(
            r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            block, re.DOTALL,
        )
        if not title_match:
            continue

        raw_url = title_match.group(1)
        title_html = title_match.group(2)

        # Decodificar URL do redirect DDG (uddg=ENCODED)
        uddg_match = re.search(r'uddg=([^&]+)', raw_url)
        if uddg_match:
            from urllib.parse import unquote
            url = unquote(uddg_match.group(1))
        else:
            url = raw_url

        # Limpar título (remover tags HTML, unescape entities)
        title = unescape(re.sub(r'<[^>]+>', '', title_html)).strip()
        if not title:
            continue

        # Snippet
        snippet_match = re.search(
            r'class="result__snippet"[^>]*>(.*?)</(?:a|span)>',
            block, re.DOTALL,
        )
        snippet = ""
        if snippet_match:
            snippet = unescape(re.sub(r'<[^>]+>', '', snippet_match.group(1))).strip()

        # Filtrar resultados óbvios de lixo
        if any(bad in url.lower() for bad in ("youtube.com/watch", "facebook.com", "pinterest.")):
            continue

        results.append({"title": title, "url": url, "snippet": snippet})

    return results


def clear_cache() -> None:
    """Limpa o cache em memória (útil entre execuções de teste)."""
    _cache.clear()
