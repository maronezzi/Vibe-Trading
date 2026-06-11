"""BOVA11 / IVVB11 proxy: índice Brasil e S&P500 em BRL via yfinance.

BOVA11 = ETF iShares que replica Ibovespa (correlação 99%+)
IVVB11 = ETF iShares S&P500 em BRL (hedge cambial + exposição EUA)

Vantagens:
  - yfinance: zero rate limit, dados intraday até 1m (60 dias) e daily (anos)
  - Sem Wine, sem MT5, sem credenciais
  - Histórico longo (BOVA11: 2008, IVVB11: 2014)
  - Liquidíssimo: ~R$ 1bi/dia cada um
  - Perfeito pra backtest diário e intraday

Limitação:
  - Tracking error ~0.2% vs índice subjacente
  - Sofre microstructure (taxas, spread) — não dá pra arbitragem
  - Daily: history 20y+
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


# Lista canônica de tickers-índice via ETF B3
INDEX_ETFS = {
    "BOVA11": {
        "name": "iShares Ibovespa",
        "tracks": "Ibovespa (IBOV)",
        "correlation": 0.998,
        "expense_ratio": 0.30,  # % a.a.
        "use": "proxy índice Brasil",
    },
    "IVVB11": {
        "name": "iShares S&P 500 (BRL)",
        "tracks": "S&P 500 em reais (hedge cambial inverso)",
        "correlation": 0.995,  # vs S&P em BRL
        "expense_ratio": 0.25,
        "use": "exposição EUA + dolarização",
    },
    "SMAL11": {
        "name": "iShares Bovespa Small Cap",
        "tracks": "Small caps B3",
        "correlation": 0.97,
        "expense_ratio": 0.59,
        "use": "small caps",
    },
    "NASD11": {
        "name": "iShares NASDAQ-100 (BRL)",
        "tracks": "Nasdaq 100 em reais",
        "correlation": 0.995,
        "expense_ratio": 0.30,
        "use": "exposição tech EUA",
    },
    "DIVO11": {
        "name": "iShares Dividendos",
        "tracks": "high dividend B3",
        "correlation": 0.92,
        "expense_ratio": 0.50,
        "use": "dividend yield",
    },
    "HASH11": {
        "name": "Hashdex NASDAQ Crypto Index",
        "tracks": "cripto em BRL (BTC+ETH)",
        "correlation": 0.85,
        "expense_ratio": 1.30,
        "use": "exposição cripto",
    },
}


def fetch_ohlcv(code: str, *, period: str = "2y", interval: str = "1d") -> Optional[pd.DataFrame]:
    """Baixa OHLCV de ETF-índice B3 via yfinance.

    Args:
        code: BOVA11, IVVB11, SMAL11, NASD11, DIVO11, HASH11.
        period: 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, max.
        interval: 1m, 5m, 15m, 1h, 1d, 1wk, 1mo.

    Returns:
        DataFrame [open, high, low, close, volume] indexado por trade_date, ou None.
    """
    code = code.upper()
    if code not in INDEX_ETFS:
        raise ValueError(f"ETF desconhecido: {code}. Use {list(INDEX_ETFS)}")

    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance não instalado")
        return None

    # yfinance quer ticker com .SA pra B3
    ticker = f"{code}.SA"
    try:
        df = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=True)
    except Exception as exc:
        logger.error("yfinance %s falhou: %s", ticker, exc)
        return None

    if df is None or df.empty:
        logger.warning("yfinance %s: sem dados", ticker)
        return None

    # Normaliza MultiIndex
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
    out = df[keep].copy()
    out.columns = [c.lower() for c in out.columns]
    out.index = pd.to_datetime(out.index).tz_localize(None).normalize()
    out.index.name = "trade_date"
    out = out.dropna(subset=["open", "high", "low", "close"])
    out.attrs["source"] = "yfinance"
    logger.info("yfinance %s %s/%s: %d barras de %s → %s",
                ticker, period, interval, len(out), out.index[0].date(), out.index[-1].date())
    return out


# --- Smoke test ---
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print("\n=== Index ETF loader — smoke test ===\n")
    for code in ["BOVA11", "IVVB11", "NASD11"]:
        info = INDEX_ETFS[code]
        df = fetch_ohlcv(code, period="1y")
        if df is not None:
            first = float(df["close"].iloc[0])
            last = float(df["close"].iloc[-1])
            ret = (last / first - 1) * 100
            print(f"  {code:6s} ({info['name']:30s})  {len(df):>4} pregões  "
                  f"ret 1y {ret:+6.2f}%  R$ {first:>6.2f} → R$ {last:>6.2f}")
        else:
            print(f"  {code:6s} ❌ sem dados")
