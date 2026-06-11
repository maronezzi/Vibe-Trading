"""MT5 loader: B3 futures (WIN, WDO) via Wine + subprocess.

Runs mt5_fetch.py inside Wine Python (with official MetaTrader5 package),
reads CSV output via subprocess. No RPyC dependency needed.

Prerequisites:
  - Wine + MT5 installed and running (see install_mt5.sh)
  - Wine Python with MetaTrader5 + mt5_fetch.py (auto-setup)

Symbols:
  WIN$  = Mini Índice B3 (continuous contract)
  WDO$  = Mini Dólar B3 (continuous contract)
  WINV26, WINQ26... = specific month contracts
  WDOF26, WDOG26... = specific month contracts

Usage:
    from backtest.loaders.mt5_loader import fetch_ohlcv, mt5_info
    info = mt5_info()
    df = fetch_ohlcv("WIN$", timeframe="D1", n_bars=500)
"""

from __future__ import annotations

import csv
import io
import logging
import os
import subprocess
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Wine Python + fetch script paths
WINE_PYTHON = os.path.expanduser("~/.wine/drive_c/Python311/python.exe")
FETCH_SCRIPT = os.path.join(os.path.dirname(__file__), "../../../mt5_fetch.py")

# Resolve absolute path
FETCH_SCRIPT = os.path.abspath(FETCH_SCRIPT)


def _wine_run(*args: str, timeout: int = 30) -> Optional[str]:
    """Run mt5_fetch.py inside Wine Python, return stdout or None."""
    if not os.path.isfile(WINE_PYTHON):
        logger.error("Wine Python não encontrado: %s", WINE_PYTHON)
        return None
    if not os.path.isfile(FETCH_SCRIPT):
        logger.error("mt5_fetch.py não encontrado: %s", FETCH_SCRIPT)
        return None

    cmd = ["wine", WINE_PYTHON, FETCH_SCRIPT] + list(args)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "WINEDEBUG": "-all"},  # suppress wine noise
        )
        if result.returncode != 0:
            logger.error("mt5_fetch.py error (rc=%d): %s", result.returncode, result.stderr[:200])
            return None
        stdout = result.stdout.strip()
        if stdout.startswith("ERROR:"):
            logger.error("mt5_fetch.py: %s", stdout)
            return None
        return stdout
    except subprocess.TimeoutExpired:
        logger.error("mt5_fetch.py timeout (%ds)", timeout)
        return None
    except FileNotFoundError:
        logger.error("wine command not found")
        return None


def mt5_info() -> Optional[dict]:
    """Get MT5 connection info (account, server, version)."""
    raw = _wine_run("info", timeout=15)
    if not raw:
        return None

    info = {}
    for line in raw.splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("==="):
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if key == "terminal":
                parts = val.split(" build=")
                info["terminal"] = parts[0]
                if len(parts) > 1:
                    info["build"] = parts[1]
            elif key == "server":
                info["server"] = val
            elif key == "login":
                info["login"] = val
            elif key == "balance":
                info["balance"] = val
            elif key == "futures":
                info["futures"] = val.split(",")
    return info


def list_symbols(pattern: str = None) -> list[str]:
    """List available symbols. Pattern filter for WIN/WDO etc."""
    # For now, use info to get futures list
    info = mt5_info()
    if info and "futures" in info:
        syms = info["futures"]
        if pattern:
            syms = [s for s in syms if pattern.upper() in s.upper()]
        return syms
    return []


def fetch_ohlcv(
    symbol: str,
    *,
    timeframe: str = "D1",
    n_bars: int = 500,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Optional[pd.DataFrame]:
    """Download OHLCV data from MT5 via Wine subprocess.

    Args:
        symbol: WIN$, WDO$, WINV26, WDOF26, etc.
        timeframe: M1, M5, M15, M30, H1, H4, D1, W1, MN.
        n_bars: Number of bars (last N).
        start_date/end_date: 'YYYY-MM-DD' range (overrides n_bars).

    Returns:
        DataFrame [open, high, low, close, tickvol, spread, volume]
        indexed by datetime, or None if failed.
    """
    valid_tf = {"M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN"}
    if timeframe not in valid_tf:
        raise ValueError(f"timeframe inválido: {timeframe!r}. Use {sorted(valid_tf)}")

    args = ["rates", symbol, timeframe, str(n_bars)]
    raw = _wine_run(*args, timeout=30)
    if not raw:
        return None

    try:
        reader = csv.reader(io.StringIO(raw))
        headers = next(reader)
        rows = [r for r in reader if r]
    except Exception as e:
        logger.error("CSV parse error: %s", e)
        return None

    if not rows:
        return None

    df = pd.DataFrame(rows, columns=headers)
    # Convert types
    for col in ["open", "high", "low", "close", "tick_volume", "spread", "real_volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Timestamp index
    df["time"] = pd.to_datetime(df["time"].astype(int), unit="s")
    df = df.set_index("time").rename_axis("trade_date")

    # Rename columns to standard format
    col_map = {"tick_volume": "tickvol", "real_volume": "volume", "spread": "spread"}
    df = df.rename(columns=col_map)

    keep = ["open", "high", "low", "close", "tickvol", "spread"]
    if "volume" in df.columns:
        keep.append("volume")
    df = df[keep].dropna(subset=["close"])

    logger.info("MT5 %s %s: %d barras %s → %s",
                symbol, timeframe, len(df), df.index[0], df.index[-1])
    df.attrs["source"] = "mt5"
    return df


def fetch_ticks(symbol: str, n_ticks: int = 10) -> Optional[pd.DataFrame]:
    """Fetch recent ticks from MT5."""
    raw = _wine_run("ticks", symbol, str(n_ticks), timeout=15)
    if not raw:
        return None
    try:
        reader = csv.reader(io.StringIO(raw))
        headers = next(reader)
        rows = [r for r in reader if r]
    except Exception:
        return None
    if not rows:
        return None

    df = pd.DataFrame(rows, columns=headers)
    for col in ["bid", "ask", "last", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["time"] = pd.to_datetime(df["time"].astype(int), unit="s")
    df = df.set_index("time")
    return df


# --- Smoke test ---
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print("\n=== MT5 Loader — smoke test (Wine subprocess) ===\n")

    info = mt5_info()
    if info:
        print(f"Terminal: {info.get('terminal', '?')} build {info.get('build', '?')}")
        print(f"Server: {info.get('server', '?')} login {info.get('login', '?')}")
        print(f"Balance: {info.get('balance', '?')}")
        print(f"Futures: {len(info.get('futures', []))} symbols\n")
    else:
        print("❌ MT5 não conectado. Verifique se o terminal está rodando:")
        print("   wine 'C:\\Program Files\\MetaTrader 5 Terminal\\terminal64.exe'")
        sys.exit(1)

    for sym in ["WIN$", "WDO$"]:
        try:
            df = fetch_ohlcv(sym, timeframe="D1", n_bars=30)
            if df is not None:
                last = df.iloc[-1]
                print(f"{sym:6s} D1: close={last['close']:>10.2f}  ({len(df)} barras)")
                print(f"         range: {df.index[0]} → {df.index[-1]}")
            else:
                print(f"{sym:6s} ❌ sem dados")
        except Exception as e:
            print(f"{sym:6s} 💥 {e}")
