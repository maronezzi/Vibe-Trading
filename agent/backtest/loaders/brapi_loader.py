"""Brapi.dev loader: B3 (Brasil) OHLCV + fundamentals.

API gratuita brasileira (https://brapi.dev) — sem token necessário para
endpoints públicos. Suporta cotação, histórico e fundamentalistas.

Endpoints usados:
  /api/quote/{symbol}?range=1y&interval=1d → histórico de preços
  /api/quote/{symbol} → cotação + fundamentalistas

Vantagens vs yfinance(.SA):
  - 5-10x mais rápido (sem proxy Yahoo)
  - Sem rate limit agressivo (free: 25 req/min)
  - Dados B3-nativos (P/L, dividend yield em formato B3)
  - API simples e bem documentada em PT-BR

Limitações:
  - Apenas B3 (não cobre US/HK/crypto)
  - Range máximo histórico: 5 anos (free tier)
  - Algumas ações small-cap podem ter histórico curto
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_BASE_URL = "https://brapi.dev/api"
_TIMEOUT = 15  # segundos
_RANGE_INTERVAL = {
    "1d": ("1mo", "1d"),
    "5d": ("1mo", "1d"),
    "1mo": ("1mo", "1d"),
    "3mo": ("3mo", "1d"),
    "6mo": ("6mo", "1d"),
    "1y": ("1y", "1d"),
    "2y": ("2y", "1d"),
    "5y": ("5y", "1d"),
}


def _http_get(url: str) -> dict:
    """GET com User-Agent e tratamento de erro."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Vibe-Trading/1.0 (BrapiLoader)"},
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.loads(r.read())


def _is_b3(code: str) -> bool:
    """Aceita VALE3, PETR4, ITUB3, BBDC4 etc. (3-6 chars alfanum)."""
    upper = code.upper()
    return 3 <= len(upper) <= 6 and upper.isalnum()


def fetch_ohlcv(
    code: str,
    *,
    range_: str = "1y",
    interval: str = "1d",
) -> Optional[pd.DataFrame]:
    """Baixa histórico OHLCV de uma ação B3 via brapi.dev.

    Args:
        code: Ticker B3 sem prefixo (ex: 'VALE3', 'PETR4').
        range_: Janela — '1mo' | '3mo' | '6mo' | '1y' | '2y' | '5y'.
        interval: '1d' (free) | '1h' | '5m' etc. (plano pago).

    Returns:
        DataFrame com colunas [open, high, low, close, volume] indexado por
        trade_date, ou None se falhar.
    """
    code = code.upper()
    if not _is_b3(code):
        raise ValueError(f"Código B3 inválido: {code!r}")

    if range_ not in _RANGE_INTERVAL:
        raise ValueError(f"Range inválido: {range_!r}. Use um de {list(_RANGE_INTERVAL)}")

    url = f"{_BASE_URL}/quote/{urllib.parse.quote(code)}?range={range_}&interval={interval}"
    t0 = time.time()
    try:
        data = _http_get(url)
    except Exception as exc:
        logger.error("brapi %s: HTTP falhou (%s)", code, exc)
        return None

    results = data.get("results") or []
    if not results:
        logger.warning("brapi %s: resposta vazia", code)
        return None

    hist = results[0].get("historicalDataPrice") or []
    if not hist:
        logger.warning("brapi %s: sem histórico (acao pode ser nova)", code)
        return None

    rows = []
    for h in hist:
        ts = pd.to_datetime(h["date"], unit="s", utc=True).tz_convert("America/Sao_Paulo")
        rows.append({
            "trade_date": ts.tz_localize(None).normalize(),
            "open": float(h["open"]) if h.get("open") else None,
            "high": float(h["high"]) if h.get("high") else None,
            "low": float(h["low"]) if h.get("low") else None,
            "close": float(h["close"]) if h.get("close") else None,
            "volume": int(h["volume"]) if h.get("volume") else 0,
        })

    df = pd.DataFrame(rows).set_index("trade_date").sort_index()
    df = df.dropna(subset=["open", "high", "low", "close"])
    logger.info("brapi %s: %d pregões em %.2fs", code, len(df), time.time() - t0)
    return df


def fetch_quote(code: str) -> Optional[dict]:
    """Cotação + fundamentalistas de uma ação B3.

    Returns:
        Dict com chaves: regularMarketPrice, marketCap, priceEarnings,
        earningsPerShare, dividendYield, fiftyTwoWeekHigh/Low, sector,
        longName, etc. — ou None se falhar.
    """
    code = code.upper()
    if not _is_b3(code):
        raise ValueError(f"Código B3 inválido: {code!r}")

    url = f"{_BASE_URL}/quote/{urllib.parse.quote(code)}"
    try:
        data = _http_get(url)
    except Exception as exc:
        logger.error("brapi %s quote: HTTP falhou (%s)", code, exc)
        return None

    results = data.get("results") or []
    if not results:
        return None

    q = results[0]
    return {
        "symbol": q.get("symbol"),
        "longName": q.get("longName"),
        "sector": q.get("sector"),
        "industry": q.get("industry"),
        "currency": q.get("currency"),
        "regularMarketPrice": q.get("regularMarketPrice"),
        "regularMarketChange": q.get("regularMarketChange"),
        "regularMarketChangePercent": q.get("regularMarketChangePercent"),
        "regularMarketVolume": q.get("regularMarketVolume"),
        "marketCap": q.get("marketCap"),
        "priceEarnings": q.get("priceEarnings"),
        "earningsPerShare": q.get("earningsPerShare"),
        "dividendYield": q.get("dividendYield"),  # null se não houver
        "fiftyTwoWeekHigh": q.get("fiftyTwoWeekHigh"),
        "fiftyTwoWeekLow": q.get("fiftyTwoWeekLow"),
        "logourl": q.get("logourl"),
    }


# --- Smoke test ---
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print("\n=== Brapi.dev loader — smoke test ===\n")
    for code in ["VALE3", "PETR4", "ITUB4"]:
        q = fetch_quote(code)
        if q:
            price = q.get('regularMarketPrice') or 0
            pe = q.get('priceEarnings')
            dy = q.get('dividendYield')
            mc = q.get('marketCap') or 0
            pe_s = f"{pe:>6.2f}" if pe is not None else "   N/A"
            dy_s = f"{dy:>5.2f}%" if dy is not None else "  N/A "
            print(f"{code:6s}  R$ {price:>7.2f}  "
                  f"P/L {pe_s}  "
                  f"DivYld {dy_s}  "
                  f"Mcap R${mc/1e9:>5.1f}bi")
    print()
    df = fetch_ohlcv("VALE3", range_="1y")
    if df is not None:
        print(f"VALE3 1y: {len(df)} pregões  "
              f"{df.index[0].date()} → {df.index[-1].date()}")
        print(df.tail(3))
