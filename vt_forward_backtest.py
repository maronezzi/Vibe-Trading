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


# ─── Stubs (Task 1) — to be replaced in Tasks 2-5 ────────────────────────


def discover_pairs(config: dict) -> list:
    """Discover all active (sym, tf, strategy_name, params_dict) pairs from config.

    Reads dynamically from vt_config.json. No hardcode.
    Adding a new symbol to config["symbols"] auto-discovers it.

    Returns: list of (sym_root, tf, strategy_name, params_dict) tuples.
    """
    raise NotImplementedError("discover_pairs will be implemented in Task 2")


def run_all_pairs_parallel(
    config: dict, days: int = 7, max_workers: int = 4, pair_timeout: int = 60
) -> dict:
    """Run forward backtest for all pairs in parallel using multiprocessing.Pool.

    Returns: dict keyed by "SYM_TF" with {pnl, n_trades, wr, max_dd, decision}.
    """
    raise NotImplementedError("run_all_pairs_parallel will be implemented in Task 5")


def _get_safe_max_workers(configured_max: int, cpu_count: int, load_avg: float) -> int:
    """Auto-adjust worker count to avoid CPU saturation.

    Returns: safe number of workers (always >= 1).
    """
    raise NotImplementedError("_get_safe_max_workers will be implemented in Task 3")


# ─── Placeholder for future functions (Tasks 2-5) ──────────────────────────


def fetch_bars_for_backtest(symbol: str, tf: str, count: int = 500) -> list:
    """Fetch bars via Wine + MT5. Task 3."""
    raise NotImplementedError("fetch_bars_for_backtest will be implemented in Task 3")


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
