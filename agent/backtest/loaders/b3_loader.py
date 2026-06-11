"""Wrapper com fallback automático: brapi.dev → yfinance.

Tenta primeiro brapi.dev (rápido, B3-nativo, com fundamental data).
Se falhar (rate limit 401, timeout, HTTP error), cai automaticamente
pra yfinance com sufixo .SA (mais lento mas sem rate limit agressivo).

Para o usuário é transparente — ele só vê "loader" e o melhor dado.

Uso:
    from backtest.loaders.b3_loader import fetch_ohlcv, fetch_quote
    df = fetch_ohlcv("VALE3", range_="1y")  # brapi → yfinance fallback
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import pandas as pd

from backtest.loaders.brapi_loader import fetch_ohlcv as _brapi_ohlcv
from backtest.loaders.brapi_loader import fetch_quote as _brapi_quote
from backtest.loaders.brapi_loader import _is_b3

logger = logging.getLogger(__name__)

# --- Yfinance helpers ---

def _yf_to_ohlcv(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """Normaliza output do yfinance pro schema OHLCV padrão."""
    if df is None or df.empty:
        return None
    # yfinance >= 0.2.31 retorna MultiIndex columns
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
    if len(keep) < 4:
        return None
    out = df[keep].copy()
    out.columns = [c.lower() for c in out.columns]
    out.index = pd.to_datetime(out.index).tz_localize(None).normalize()
    out.index.name = "trade_date"
    out = out.dropna(subset=["open", "high", "low", "close"])
    return out.sort_index() if not out.empty else None


def _yf_ohlcv(code: str, range_: str = "1y") -> Optional[pd.DataFrame]:
    """Baixa histórico via yfinance(.SA)."""
    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance não instalado")
        return None

    # Mapear range_ brapi → period yfinance
    period_map = {
        "1mo": "1mo", "3mo": "3mo", "6mo": "6mo",
        "1y": "1y", "2y": "2y", "5y": "5y",
    }
    period = period_map.get(range_, "1y")

    ticker = f"{code}.SA"
    t0 = time.time()
    try:
        df = yf.download(ticker, period=period, progress=False, auto_adjust=True)
    except Exception as exc:
        logger.error("yfinance %s falhou: %s", ticker, exc)
        return None

    out = _yf_to_ohlcv(df)
    if out is not None:
        logger.info("yfinance %s: %d pregões em %.2fs", ticker, len(out), time.time() - t0)
    return out


def _yf_quote(code: str) -> Optional[dict]:
    """Cotação + fundamentalistas via yfinance."""
    try:
        import yfinance as yf
    except ImportError:
        return None

    ticker = f"{code}.SA"
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
    except Exception as exc:
        logger.error("yfinance Ticker(%s) falhou: %s", ticker, exc)
        return None

    if not info:
        return None

    return {
        "symbol": code,
        "longName": info.get("longName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "currency": info.get("currency"),
        "regularMarketPrice": info.get("currentPrice") or info.get("regularMarketPrice"),
        "regularMarketChange": info.get("regularMarketChange"),
        "regularMarketChangePercent": info.get("regularMarketChangePercent"),
        "regularMarketVolume": info.get("regularMarketVolume"),
        "marketCap": info.get("marketCap"),
        "priceEarnings": info.get("trailingPE") or info.get("forwardPE"),
        "earningsPerShare": info.get("trailingEps"),
        "dividendYield": info.get("dividendYield"),  # fração (0.0669 = 6.69%)
        "fiftyTwoWeekHigh": info.get("fiftyTwoWeekHigh"),
        "fiftyTwoWeekLow": info.get("fiftyTwoWeekLow"),
        "_source": "yfinance",
    }


# --- API pública com fallback ---

def fetch_ohlcv(code: str, *, range_: str = "1y") -> Optional[pd.DataFrame]:
    """OHLCV B3 com fallback automático: brapi.dev → yfinance(.SA).

    Args:
        code: Ticker B3 (ex: 'VALE3', 'PETR4').
        range_: '1mo' | '3mo' | '6mo' | '1y' | '2y' | '5y'.

    Returns:
        DataFrame [open, high, low, close, volume] indexado por trade_date,
        ou None se ambas as fontes falharem.
    """
    code = code.upper()
    if not _is_b3(code):
        raise ValueError(f"Código B3 inválido: {code!r}")

    # 1) Tenta brapi (rápido)
    try:
        df = _brapi_ohlcv(code, range_=range_)
        if df is not None and not df.empty:
            df.attrs["source"] = "brapi"
            return df
    except Exception as exc:
        logger.warning("brapi %s falhou: %s — tentando yfinance", code, exc)

    # 2) Fallback yfinance (sem rate limit, mais lento)
    df = _yf_ohlcv(code, range_=range_)
    if df is not None and not df.empty:
        df.attrs["source"] = "yfinance"
        return df

    logger.error("Ambas as fontes falharam pra %s", code)
    return None


def fetch_quote(code: str) -> Optional[dict]:
    """Cotação + fundamentalistas B3 com fallback automático."""
    code = code.upper()
    if not _is_b3(code):
        raise ValueError(f"Código B3 inválido: {code!r}")

    # 1) Tenta brapi
    try:
        q = _brapi_quote(code)
        if q is not None:
            q["_source"] = "brapi"
            return q
    except Exception as exc:
        logger.warning("brapi %s quote falhou: %s", code, exc)

    # 2) Fallback yfinance
    return _yf_quote(code)


# --- Smoke test ---
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print("\n=== B3 unified loader — fallback test ===\n")

    for code in ["VALE3", "PETR4", "ITUB4", "BBDC4", "ABEV3", "WEGE3"]:
        print(f"--- {code} ---")
        df = fetch_ohlcv(code, range_="1y")
        if df is not None:
            q = fetch_quote(code) or {}
            price = q.get("regularMarketPrice") or 0
            pe = q.get("priceEarnings")
            pe_s = f"{pe:.1f}" if pe else "N/A"
            src = df.attrs.get("source", "?")
            print(f"  {src:9s}  {len(df)} pregões  "
                  f"preço R$ {price:>6.2f}  P/L {pe_s}")
        else:
            print("  ❌ sem dados")
        print()
