"""
vt_forward_backtest.py — Forward backtest module for AGI 17h convergence gate.

This module provides:
  - discover_pairs(config): discover all active (sym, tf) pairs from vt_config.json
  - run_mini_backtest_pair(sym, tf, config, days): backtest a single pair
  - simulate_forward(...): bar-by-bar forward simulation
  - fetch_bars_for_backtest(...): Wine/MT5 bar fetch
  - run_all_pairs_parallel(...): run all pairs via multiprocessing.Pool
  - _get_safe_max_workers(...): auto-adjust worker count based on CPU + load

Task #1 (TDD): This file initially contains stubs. Tasks 2-5 add real implementations.

Architecture:
  - Each worker (multiprocessing.Process) loads its own copy of vt_autotrader
  - Workers don't share state (config passed as arg)
  - Per-pair timeout (default 60s) prevents hung workers from blocking the pool
  - CPU load detection auto-reduces workers on busy systems (8 CPUs, 2.21 load)
"""
import os
import csv
import io
import subprocess


# ─── Module-level constants ────────────────────────────────────────────────

WINE_PYTHON = os.path.expanduser("~/.wine/drive_c/Python311/python.exe")
FETCH_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "mt5_fetch.py"
)

# Per-symbol contract specs (multiplier, slippage)
# Used by the forward simulation engine (Task 4)
_CONTRACT_SPECS = {
    "WIN$":  {"mult": 0.20, "margin": 5000, "slip": 1.0},
    "WDO$":  {"mult": 10.0, "margin": 3000, "slip": 5.0},
    "BIT$":  {"mult": 0.01, "margin": 5000, "slip": 1.0},
    "DOL$":  {"mult": 10.0, "margin": 3000, "slip": 5.0},
    "IND$":  {"mult": 0.20, "margin": 5000, "slip": 1.0},
    "WSP$":  {"mult": 0.50, "margin": 3000, "slip": 0.5},
}

# Bar fetch / Wine timeouts
FETCH_TIMEOUT_SEC = 60   # max time to wait for Wine/MT5 to return bars
PAIR_TIMEOUT_SEC = 60    # max time to wait for a single pair in the pool
LOAD_THRESHOLD_HIGH = 4.0  # load avg > this = saturated (1 worker)
LOAD_THRESHOLD_BUSY = 2.0  # load avg > this = busy (25% workers)


# ─── Stubs (Task 1) — to be replaced in Tasks 2-5 ────────────────────────


def discover_pairs(config: dict) -> list:
    """Discover all active (sym, tf, strategy_name, params_dict) pairs from config.

    Reads dynamically from vt_config.json. No hardcode.
    Adding a new symbol to config["symbols"] auto-discovers it.

    Returns: list of (sym_root, tf, strategy_name, params_dict) tuples.
    """
    pairs = []
    symbols = config.get("symbols", [])
    default_tfs = config.get("timeframes", [])
    per_sym_tfs = config.get("per_symbol_timeframes", {})
    strategy_map = config.get("strategy", {})

    for sym in symbols:
        # Skip symbols without strategy assignment
        if sym not in strategy_map:
            continue
        strategy = strategy_map[sym]
        # Resolve TFs: per-symbol override OR global default
        tfs = per_sym_tfs.get(sym, default_tfs)
        # Resolve params: merge base + per-TF override ({}, base) == (base, {})
        sym_params_base = config.get(sym.lower(), {})
        for tf in tfs:
            tf_override = sym_params_base.get(tf, {})
            merged = {**sym_params_base, **tf_override}
            pairs.append((sym, tf, strategy, merged))

    return pairs


def run_all_pairs_parallel(
    config: dict, days: int = 7, max_workers: int = 4, pair_timeout: int = 60
) -> dict:
    """Run forward backtest for all pairs in parallel using multiprocessing.Pool.

    Returns: dict keyed by "SYM_TF" with {pnl, n_trades, wr, max_dd, decision}.
    """
    raise NotImplementedError("run_all_pairs_parallel will be implemented in Task 5")


def _get_safe_max_workers(configured_max: int, cpu_count: int, load_avg: float) -> int:
    """Auto-adjust worker count to avoid CPU saturation.

    Strategy:
      1. Start with min(configured_max, cpu_count)
      2. If load > 4.0: return 1 (saturated)
      3. If load > 2.0: use 25% of CPUs (busy)
      4. Otherwise: use 50% of CPUs (capped by configured_max)
      5. Floor: 1 worker (so we always make progress)

    Args:
        configured_max: max workers requested by caller
        cpu_count: total CPU count (typically from os.cpu_count())
        load_avg: 1-minute system load average (from os.getloadavg())

    Returns:
        Safe number of workers (always >= 1)
    """
    base = max(1, min(configured_max, cpu_count))

    if load_avg > LOAD_THRESHOLD_HIGH:
        # Saturated — single worker to avoid making it worse
        return 1
    elif load_avg > LOAD_THRESHOLD_BUSY:
        # Busy — 25% of CPUs
        return max(1, cpu_count // 4)
    else:
        # Headroom available — 50% of CPUs (capped)
        return max(1, min(base, cpu_count // 2))


# ─── Placeholder for future functions (Tasks 2-5) ──────────────────────────


def fetch_bars_for_backtest(symbol: str, tf: str, count: int = 500) -> list:
    """Fetch bars via Wine + MT5. Returns list of bar dicts newest-first.

    Offline-resilient: returns [] if Wine is unavailable, MT5 is down,
    symbol is invalid, or timeout occurs. Never raises.

    Each bar dict has: time (int), open, high, low, close, tick_volume (all float).

    Output order: NEWEST-FIRST (matches autotrader's fetch_bars() format).
    """
    if not symbol or not tf:
        return []

    try:
        cmd = ["wine", WINE_PYTHON, FETCH_SCRIPT, "rates", symbol, tf, str(count)]
        env = {**os.environ, "WINEDEBUG": "-all"}
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=FETCH_TIMEOUT_SEC, env=env
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []

        reader = csv.reader(io.StringIO(result.stdout.strip()))
        try:
            next(reader)  # skip header row
        except StopIteration:
            return []
        rows = [row for row in reader if row]
        if not rows:
            return []

        # mt5_fetch outputs bars oldest-first; convert to newest-first
        bars = []
        for row in rows:
            try:
                bars.append({
                    "time": int(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "tick_volume": float(row[5]) if len(row) > 5 else 0.0,
                })
            except (ValueError, IndexError):
                continue  # skip malformed rows
        bars.reverse()  # newest-first
        return bars
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []


def simulate_forward(
    symbol: str, tf: str, bars: list, strategy_name: str, params: dict
) -> dict:
    """Bar-by-bar forward simulation. Task 4."""
    raise NotImplementedError("simulate_forward will be implemented in Task 4")


def run_mini_backtest_pair(sym: str, tf: str, config: dict, days: int = 7) -> dict:
    """Forward backtest for one SYM_TF. Task 5."""
    raise NotImplementedError("run_mini_backtest_pair will be implemented in Task 5")


def _run_single_pair(args: tuple) -> dict:
    """Worker function for multiprocessing pool. Task 5."""
    raise NotImplementedError("_run_single_pair will be implemented in Task 5")


def _detect_load_avg() -> float:
    """Get current 1-min load average."""
    try:
        return os.getloadavg()[0]
    except (OSError, AttributeError):
        return 0.0


def _detect_cpu_count() -> int:
    """Get CPU count."""
    return os.cpu_count() or 1
