"""
Data fetcher para predição — busca dados OHLCV via MT5 (Wine).

Módulo 100% isolado do projeto principal.
Usa o mesmo mt5_fetch.py via subprocess Wine.
"""

import csv
import io
import os
import subprocess
from datetime import datetime
from typing import Optional

import pandas as pd

# Paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WINE_PYTHON = os.path.expanduser("~/.wine/drive_c/Python311/python.exe")
FETCH_SCRIPT = os.path.join(BASE_DIR, "mt5_fetch.py")

# Timeframe map (MT5 → label)
TF_MAP = {
    "M1": "M1", "M5": "M5", "M15": "M15", "M30": "M30",
    "H1": "H1", "H4": "H4", "D1": "D1",
}


def _wine_run(args: list[str], timeout: int = 30) -> Optional[str]:
    """Executa mt5_fetch.py via Wine Python."""
    cmd = ["wine", WINE_PYTHON, FETCH_SCRIPT] + args
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "WINEDEBUG": "-all"},
        )
        if result.returncode != 0:
            print(f"[ERRO] mt5_fetch rc={result.returncode}: {result.stderr[:200]}")
            return None
        stdout = result.stdout.strip()
        if stdout.startswith("ERROR:"):
            print(f"[ERRO] mt5_fetch: {stdout}")
            return None
        return stdout
    except Exception as e:
        print(f"[ERRO] Wine run falhou: {e}")
        return None


def fetch_ohlcv(symbol: str = "WDO$", timeframe: str = "M5", n_bars: int = 1000) -> Optional[pd.DataFrame]:
    """
    Busca dados OHLCV do MT5.
    
    Args:
        symbol: WIN$ ou WDO$
        timeframe: M1, M5, M15, M30, H1, H4, D1
        n_bars: número de barras históricas
    
    Returns:
        DataFrame com colunas: datetime, open, high, low, close, volume
    """
    tf = TF_MAP.get(timeframe.upper(), "M5")
    raw = _wine_run(["rates", symbol, tf, str(n_bars)], timeout=30)
    if not raw:
        return None
    
    # Parse CSV output
    reader = csv.DictReader(io.StringIO(raw))
    rows = list(reader)
    if not rows:
        print(f"[AVISO] Nenhum dado retornado para {symbol} {tf}")
        return None
    
    df = pd.DataFrame(rows)
    
    # Converter colunas numéricas
    for col in ["open", "high", "low", "close", "volume", "time",
                "tick_volume", "spread", "real_volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    
    # Converter datetime
    if "time" in df.columns:
        # MT5 retorna unix timestamp (int), mas CSV lê como string
        df["datetime"] = pd.to_datetime(df["time"].astype("int64"), unit="s", utc=True)
        df["datetime"] = df["datetime"].dt.tz_localize(None)  # remove tz
    elif "datetime" not in df.columns:
        df["datetime"] = pd.date_range(end=datetime.now(), periods=len(df), freq=tf)
    
    # Manter apenas colunas necessárias
    # MT5 retorna tick_volume e real_volume — mapear para volume
    if "volume" not in df.columns:
        if "tick_volume" in df.columns:
            df["volume"] = df["tick_volume"]
        elif "real_volume" in df.columns:
            df["volume"] = df["real_volume"]
        else:
            df["volume"] = 0
    
    cols_keep = ["datetime", "open", "high", "low", "close", "volume"]
    df = df[[c for c in cols_keep if c in df.columns]]
    
    df = df.sort_values("datetime").reset_index(drop=True)
    print(f"[OK] {symbol} {tf}: {len(df)} barras ({df['datetime'].iloc[0]} → {df['datetime'].iloc[-1]})")
    return df


def fetch_all_symbols(timeframe: str = "M5", n_bars: int = 1000) -> dict[str, pd.DataFrame]:
    """Busca dados de todos os símbolos (WDO + WIN)."""
    result = {}
    for symbol in ["WDO$", "WIN$"]:
        df = fetch_ohlcv(symbol, timeframe, n_bars)
        if df is not None:
            result[symbol] = df
    return result


if __name__ == "__main__":
    # Teste rápido
    for sym in ["WDO$", "WIN$"]:
        df = fetch_ohlcv(sym, "M5", 500)
        if df is not None:
            print(f"\n{sym} — Últimas 3 barras:")
            print(df.tail(3).to_string(index=False))
