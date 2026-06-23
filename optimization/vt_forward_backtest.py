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
from typing import Optional


# ─── Module-level constants ────────────────────────────────────────────────

WINE_PYTHON = os.path.expanduser("~/.wine/drive_c/Python311/python.exe")
FETCH_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "mt5", "mt5_fetch.py"
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

# Forward simulation constants
SIM_WARMUP_BARS = 20         # bars skipped for indicator warmup
SIM_MIN_BARS = 30            # min bars required to run simulation
SIM_ATR_PERIOD = 14          # ATR period for trade management
SIM_COOLDOWN_BARS = 5        # DEFAULT bars to wait after each exit (overridden by config)
SIM_TRAIL_ACTIVATE_ATR = 1.0 # trail activates after this × ATR profit
SIM_TRAIL_DISTANCE_ATR = 0.2 # trail distance = this × ATR
SIM_COMMISSION = 2.5         # commission per trade (R$)
SIM_EOD_HOUR = 16            # EOD close hour (BRT)
SIM_EOD_MINUTE = 45          # EOD close minute (BRT)
SIM_BRT_OFFSET_HOURS = 3     # UTC → BRT offset
SIM_WARMUP_MINUTES = 5       # minutes after market open to skip (matches bot warmup_minutes)
SIM_WINDDOWN_MINUTES = 15    # minutes before close to skip (matches bot winddown_minutes)
SIM_MARKET_OPEN_HOUR = 9     # market open hour (BRT)
SIM_MARKET_OPEN_MINUTE = 5   # market open minute (BRT) — matches bot start_hour/start_minute

# Bar count per timeframe (7-day forward backtest coverage)
BAR_COUNT_PER_TF = {
    "M5":  500,   # ~7 days of 5-min bars
    "M15": 300,   # ~7 days of 15-min bars
    "M30": 200,   # ~7 days of 30-min bars
    "H1":  100,   # ~4-7 days of hourly bars
}
DEFAULT_BAR_COUNT = 300      # fallback for unknown TFs


# ─── Stubs (Task 1) — to be replaced in Tasks 2-5 ────────────────────────


def discover_pairs(config: dict) -> list:
    """Discover all active (sym, tf, strategy_name, params_dict) pairs from config.

    Reads dynamically from vt_config.json. No hardcode.
    Adding a new symbol to config["symbols"] auto-discovers it.

    Strategy resolution priority:
      1. config["strategy_by_tf"][SYM_TF]  (per-timeframe, most specific)
      2. config["strategy"][SYM]            (per-symbol fallback)

    Returns: list of (sym_root, tf, strategy_name, params_dict) tuples.
    """
    pairs = []
    symbols = config.get("symbols", [])
    default_tfs = config.get("timeframes", [])
    per_sym_tfs = config.get("per_symbol_timeframes", {})
    strategy_map = config.get("strategy", {})
    strategy_by_tf = config.get("strategy_by_tf", {})
    disabled = set(config.get("disabled_timeframes", []) or [])

    for sym in symbols:
        if sym not in strategy_map:
            continue
        default_strategy = strategy_map[sym]
        # Resolve TFs: per-symbol override OR global default
        tfs = per_sym_tfs.get(sym, default_tfs)
        # Resolve params: merge base + per-TF override ({}, base) == (base, {})
        sym_params_base = config.get(sym.lower(), {})
        for tf in tfs:
            pair_key = f"{sym}_{tf}"
            # Skip disabled pairs
            if pair_key in disabled:
                continue
            # Per-TF strategy override takes priority
            strategy = strategy_by_tf.get(pair_key, default_strategy)
            tf_override = sym_params_base.get(tf, {})
            merged = {**sym_params_base, **tf_override}
            pairs.append((sym, tf, strategy, merged))

    return pairs


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


def _resolve_sim_max_daily(params: dict, symbol: str, config: Optional[dict] = None) -> int:
    """Resolve per-symbol daily trade limit for simulation.

    Mirrors the autotrader's _resolve_max_daily_trades hierarchy:
    1. params (merged base+TF override) → max_daily_trades
    2. config[symbol_root].max_daily_trades
    3. config["max_daily_trades"]
    4. 15 (safety default)
    """
    # From merged params (most specific)
    val = params.get("max_daily_trades")
    if val is not None:
        return int(val)
    if config:
        # From symbol-level config
        sym_root = symbol.upper()
        for key in ("WIN", "WDO", "BIT", "DOL", "IND", "WSP"):
            if key in sym_root:
                sym_root = key
                break
        sym_cfg = config.get(sym_root.lower(), {})
        val = sym_cfg.get("max_daily_trades")
        if val is not None:
            return int(val)
        # From root config
        val = config.get("max_daily_trades")
        if val is not None:
            return int(val)
    return 15  # safety default


def simulate_forward(
    symbol: str, tf: str, bars: list, strategy_name: str, params: dict,
    config: Optional[dict] = None,
) -> dict:
    """Bar-by-bar forward simulation. Returns metrics dict.

    Reuses the autotrader's strategy plugins (dynamic import) and indicator
    functions. Bars are NEWEST-FIRST; we convert to chronological internally.

    Trade lifecycle:
      - Entry: strategy.check_entry() returns {direction, sl_pts, ...}
      - Exit: SL hit intra-bar, trailing after 1.0x ATR, or EOD close at 16:45
      - Cooldown: from config (cooldown_seconds) converted to bars, or default 5 bars
      - Per-symbol daily trade limits: max_daily_trades from config
      - Warmup/winddown: skip entries in first/last N min of session
      - Execution delay (simulate_execution_delay): when True, signal on bar N
        enters at bar N+1's open price (realistic 1-bar delay)

    Returns:
      dict with keys: pnl, n_trades, wr, max_dd, decision
      decision ∈ {ok, negative, no_data, no_trades, strategy_load_failed, utils_load_failed, error}
    """
    empty = {
        "pnl": 0.0, "n_trades": 0, "wr": 0.0, "max_dd": 0.0,
        "decision": "no_data",
    }
    if not bars or len(bars) < SIM_MIN_BARS:
        return empty

    # Lazy import autotrader utils (avoids running its main loop)
    utils = _load_strategy_utils()
    if utils is None:
        empty["decision"] = "utils_load_failed"
        return empty

    # Lazy import strategy plugin
    strategy = _load_strategy_module(strategy_name)
    if strategy is None:
        empty["decision"] = "strategy_load_failed"
        return empty

    # Resolve contract spec by symbol+'$'
    spec = _CONTRACT_SPECS.get(symbol + "$", _CONTRACT_SPECS["WIN$"])
    mult = spec["mult"]
    slip = spec["slip"]
    commission = SIM_COMMISSION

    # ── Resolve realistic limits from config ──
    # Cooldown: convert seconds → bars (approximate based on TF)
    tf_minutes = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60}.get(tf, 5)
    cooldown_seconds = params.get("cooldown_seconds", SIM_COOLDOWN_BARS * tf_minutes * 60)
    cooldown_bars = max(1, int(cooldown_seconds / (tf_minutes * 60)))

    # Per-symbol daily trade limit
    max_daily = _resolve_sim_max_daily(params, symbol, config)

    # Warmup/winddown from config (or defaults)
    if config:
        warmup_min = config.get("warmup_minutes", SIM_WARMUP_MINUTES)
        winddown_min = config.get("winddown_minutes", SIM_WINDDOWN_MINUTES)
    else:
        warmup_min = SIM_WARMUP_MINUTES
        winddown_min = SIM_WINDDOWN_MINUTES

    # bars are newest-first; reverse to chronological for simulation
    chronological = list(reversed(bars))

    # Execution delay: read from config (default True for realism)
    exec_delay = True
    if config:
        exec_delay = config.get("simulate_execution_delay", True)

    pos = 0
    ep = 0.0
    e_atr = 0.0
    sl_price = 0.0
    trail_on = False
    cooldown_until = 0
    trades = []
    pending_signal = None  # stores signal from bar N, executed on bar N+1

    # Per-symbol daily trade tracking (across simulated days)
    daily_counts = {}  # {day_str: count}

    import datetime  # used for EOD close check

    for i in range(SIM_WARMUP_BARS, len(chronological)):  # warmup for indicators
        bar = chronological[i]
        prev_bars = list(reversed(chronological[:i]))  # newest-first slice
        price = float(bar["close"])
        high = float(bar["high"])
        low = float(bar["low"])
        bar_open = float(bar["open"])
        bar_ts = int(bar["time"])

        atr = utils["calculate_atr"](prev_bars, SIM_ATR_PERIOD)
        if atr <= 0:
            continue

        # ── Execute pending signal from previous bar (1-bar execution delay) ──
        if pending_signal is not None and pos == 0:
            psig = pending_signal
            pending_signal = None
            psig_dir = psig["direction"]
            psig_sl = psig["sl_pts"]
            psig_atr = psig["atr"]
            if psig_dir in ("BUY", "SELL"):
                # Entry at this bar's open (delayed execution)
                pos = 1 if psig_dir == "BUY" else -1
                ep = bar_open
                e_atr = psig_atr
                sl_price = ep - psig_sl if pos == 1 else ep + psig_sl
                trail_on = False
                # Fall through to exit logic for this bar
            # If direction invalid, pending_signal is discarded

        if pos == 0:
            # Check cooldown
            if i < cooldown_until:
                continue

            # ── Warmup/Winddown window filter ──
            t = datetime.datetime.utcfromtimestamp(bar_ts) - datetime.timedelta(
                hours=SIM_BRT_OFFSET_HOURS
            )
            bar_minute_of_day = t.hour * 60 + t.minute
            session_open = SIM_MARKET_OPEN_HOUR * 60 + SIM_MARKET_OPEN_MINUTE
            session_close = SIM_EOD_HOUR * 60 + SIM_EOD_MINUTE
            # Skip warmup period
            if session_open <= bar_minute_of_day <= session_open + warmup_min:
                continue
            # Skip winddown period
            if session_close - winddown_min <= bar_minute_of_day <= session_close:
                continue

            # ── Per-symbol daily trade limit ──
            day_str = t.strftime("%Y-%m-%d")
            day_count = daily_counts.get(day_str, 0)
            if day_count >= max_daily:
                continue

            # Try entry
            try:
                sig = strategy.check_entry(
                    symbol=symbol, tf=tf, price=price, atr=atr,
                    bar_ts=bar_ts, bars=prev_bars, params=params, utils=utils
                )
            except Exception:
                continue
            if not sig:
                continue
            sl_pts = sig.get("sl_pts", int(atr * 1.5))
            direction = sig.get("direction")
            if direction not in ("BUY", "SELL"):
                continue

            if exec_delay:
                # Store signal for next-bar execution (1-bar delay)
                pending_signal = {
                    "direction": direction,
                    "sl_pts": sl_pts,
                    "atr": atr,
                }
            else:
                # Immediate execution (original behavior)
                pos = 1 if direction == "BUY" else -1
                ep = price
                e_atr = atr
                sl_price = ep - sl_pts if pos == 1 else ep + sl_pts
                trail_on = False
        else:
            # Check exit: SL hit intra-bar
            if pos == 1 and low <= sl_price:
                pnl = (sl_price - ep) * mult - slip - commission
                trades.append(pnl)
                pos = 0
                cooldown_until = i + cooldown_bars
                # Track daily count
                t = datetime.datetime.utcfromtimestamp(bar_ts) - datetime.timedelta(
                    hours=SIM_BRT_OFFSET_HOURS
                )
                day_str = t.strftime("%Y-%m-%d")
                daily_counts[day_str] = daily_counts.get(day_str, 0) + 1
                continue
            if pos == -1 and high >= sl_price:
                pnl = (ep - sl_price) * mult - slip - commission
                trades.append(pnl)
                pos = 0
                cooldown_until = i + cooldown_bars
                t = datetime.datetime.utcfromtimestamp(bar_ts) - datetime.timedelta(
                    hours=SIM_BRT_OFFSET_HOURS
                )
                day_str = t.strftime("%Y-%m-%d")
                daily_counts[day_str] = daily_counts.get(day_str, 0) + 1
                continue
            # Trailing stop: activate after SIM_TRAIL_ACTIVATE_ATR × ATR profit
            profit_pts = (high - ep) if pos == 1 else (ep - low)
            if not trail_on and profit_pts >= SIM_TRAIL_ACTIVATE_ATR * e_atr:
                trail_on = True
            if trail_on:
                td = SIM_TRAIL_DISTANCE_ATR * e_atr
                if pos == 1:
                    nsl = high - td
                    if nsl > sl_price:
                        sl_price = nsl
                else:
                    nsl = low + td
                    if nsl < sl_price:
                        sl_price = nsl
            # EOD close at SIM_EOD_HOUR:SIM_EOD_MINUTE BRT (UTC-SIM_BRT_OFFSET_HOURS)
            t = datetime.datetime.utcfromtimestamp(bar_ts) - datetime.timedelta(
                hours=SIM_BRT_OFFSET_HOURS
            )
            if t.hour > SIM_EOD_HOUR or (t.hour == SIM_EOD_HOUR and t.minute >= SIM_EOD_MINUTE):
                pnl = ((price - ep) if pos == 1 else (ep - price)) * mult - slip - commission
                trades.append(pnl)
                pos = 0
                cooldown_until = i + cooldown_bars
                day_str = t.strftime("%Y-%m-%d")
                daily_counts[day_str] = daily_counts.get(day_str, 0) + 1

    if not trades:
        empty["decision"] = "no_trades"
        return empty

    n = len(trades)
    wins = sum(1 for t in trades if t > 0)
    pnl = sum(trades)
    wr = wins / n * 100
    # Max drawdown (equity-based)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        equity += t
        peak = max(peak, equity)
        dd = peak - equity
        max_dd = max(max_dd, dd)

    return {
        "pnl": round(pnl, 2),
        "n_trades": n,
        "wr": round(wr, 1),
        "max_dd": round(max_dd, 2),
        "decision": "ok" if pnl > 0 else "negative",
    }


def _load_strategy_utils() -> Optional[dict]:
    """Lazy import — load indicator functions from vt_autotrader.

    Returns dict of utils (or None if import fails).
    Avoids running vt_autotrader's main loop by importing only the functions.
    """
    try:
        import importlib.util
        import sys as _sys
        path = os.path.join(os.path.dirname(__file__), "..", "core", "vt_autotrader.py")
        spec = importlib.util.spec_from_file_location("vt_autotrader", path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        _sys.modules["vt_autotrader"] = mod
        spec.loader.exec_module(mod)
        if not hasattr(mod, "_init_strategy_utils"):
            return None
        mod._init_strategy_utils()
        return mod._strategy_utils
    except Exception:
        return None


def _load_strategy_module(strategy_name: str):
    """Lazy import — load a strategy plugin from strategies/<name>.py.

    Returns the module (with STRATEGY_NAME + check_entry) or None.
    """
    try:
        import importlib.util
        import sys as _sys
        path = os.path.join(
            os.path.dirname(__file__), "..", "strategies", f"{strategy_name.lower()}.py"
        )
        if not os.path.exists(path):
            return None
        spec = importlib.util.spec_from_file_location(f"strategy_{strategy_name}", path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        _sys.modules[f"strategy_{strategy_name}"] = mod
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def run_mini_backtest_pair(sym: str, tf: str, config: dict, days: int = 7) -> dict:
    """Forward backtest for one SYM_TF. Reuses autotrader plugins.

    Pipeline:
      1. Look up strategy + params for sym/tf
      2. Fetch bars via Wine/MT5 (offline-resilient)
      3. Run bar-by-bar simulation
      4. Return metrics dict

    Returns dict with: pnl, n_trades, wr, max_dd, decision.
    decision ∈ {ok, negative, no_data, no_trades, strategy_not_in_config,
                strategy_load_failed, utils_load_failed}
    """
    empty = {
        "pnl": 0.0, "n_trades": 0, "wr": 0.0, "max_dd": 0.0,
        "decision": "no_data",
    }
    if not sym or not tf or not isinstance(config, dict):
        return empty

    # Find strategy + params — per-TF override takes priority
    strategy_by_tf = config.get("strategy_by_tf", {})
    pair_key = f"{sym}_{tf}"
    strategy_name = strategy_by_tf.get(pair_key) or config.get("strategy", {}).get(sym)
    if not strategy_name:
        empty["decision"] = "strategy_not_in_config"
        return empty

    params = _resolve_pair_params(config, sym, tf)

    # Fetch bars (offline-resilient)
    full_symbol = f"{sym}$"
    bar_count = BAR_COUNT_PER_TF.get(tf, DEFAULT_BAR_COUNT)
    bars = fetch_bars_for_backtest(full_symbol, tf, count=bar_count)
    if not bars:
        return empty

    # Simulate
    return simulate_forward(sym, tf, bars, strategy_name, params, config=config)


def run_mini_backtest_pair_with_strategy(
    sym: str, tf: str, strategy_name: str, config: dict, days: int = 7,
) -> dict:
    """Forward backtest for one SYM_TF with an explicit strategy override.

    Like run_mini_backtest_pair but forces a specific strategy instead of
    looking it up from config. Used by AGI convergence loop to explore
    all 28 strategies for failing pairs.

    Returns dict with: pnl, n_trades, wr, max_dd, decision.
    """
    empty = {
        "pnl": 0.0, "n_trades": 0, "wr": 0.0, "max_dd": 0.0,
        "decision": "no_data",
    }
    if not sym or not tf or not isinstance(config, dict):
        return empty

    params = _resolve_pair_params(config, sym, tf)

    full_symbol = f"{sym}$"
    bar_count = BAR_COUNT_PER_TF.get(tf, DEFAULT_BAR_COUNT)
    bars = fetch_bars_for_backtest(full_symbol, tf, count=bar_count)
    if not bars:
        return empty

    return simulate_forward(sym, tf, bars, strategy_name, params, config=config)


def _resolve_pair_params(config: dict, sym: str, tf: str) -> dict:
    """Resolve params for a (sym, tf) pair from config.

    Reads config[symbol.lower()] as base, then merges config[symbol.lower()][tf]
    if present (per-TF override).
    """
    sym_params_base = config.get(sym.lower(), {})
    tf_override = sym_params_base.get(tf, {})
    return {**sym_params_base, **tf_override}


def _run_single_pair(args: tuple) -> dict:
    """Worker function for multiprocessing pool.

    args = (sym, tf, config, days, timeout)

    Must be module-level (forksafe). Returns dict with at minimum
    {sym, tf, decision, pnl, n_trades, wr, max_dd}.
    """
    sym, tf, config, days, timeout = args
    base = {
        "sym": sym, "tf": tf,
        "pnl": 0.0, "n_trades": 0, "wr": 0.0, "max_dd": 0.0,
        "decision": "no_data",
    }
    try:
        if not sym or not tf or not isinstance(config, dict):
            base["decision"] = "error_invalid_args"
            return base
        result = run_mini_backtest_pair(sym, tf, config, days=days)
        return {**base, **result, "sym": sym, "tf": tf}
    except Exception as e:
        base["decision"] = f"error:{type(e).__name__}"
        base["error"] = str(e)[:200]
        return base


def run_all_pairs_parallel(
    config: dict, days: int = 7, max_workers: int = 4, pair_timeout: int = PAIR_TIMEOUT_SEC
) -> dict:
    """Run forward backtest for all pairs in parallel using multiprocessing.Pool.

    Pipeline:
      1. discover_pairs(config) → list of (sym, tf, strategy, params)
      2. _get_safe_max_workers() — auto-adjust based on CPU + load
      3. ProcessPoolExecutor — submit 1 task per pair
      4. Collect with per-pair timeout (default 60s)
      5. Survive worker crashes (timeout/error → entry with decision)

    Returns: dict keyed by "SYM_TF" with {pnl, n_trades, wr, max_dd, decision}.
    """
    from concurrent.futures import ProcessPoolExecutor, as_completed, TimeoutError as FuturesTimeout

    pairs = discover_pairs(config)
    if not pairs:
        return {}

    cpu_count = _detect_cpu_count()
    load_avg = _detect_load_avg()
    safe_workers = _get_safe_max_workers(max_workers, cpu_count, load_avg)
    safe_workers = min(safe_workers, len(pairs))  # no point having more workers than pairs
    if safe_workers < 1:
        safe_workers = 1

    # Build args list (only sym, tf, config, days, timeout — strategy/params
    # are looked up by run_mini_backtest_pair from config)
    args_list = [(sym, tf, config, days, pair_timeout) for sym, tf, _strat, _params in pairs]

    results = {}
    with ProcessPoolExecutor(max_workers=safe_workers) as executor:
        future_to_pair = {
            executor.submit(_run_single_pair, args): (args[0], args[1])
            for args in args_list
        }
        for future in as_completed(future_to_pair, timeout=pair_timeout * len(pairs) + 30):
            sym, tf = future_to_pair[future]
            key = f"{sym}_{tf}"
            try:
                result = future.result(timeout=pair_timeout)
                results[key] = result
            except FuturesTimeout:
                results[key] = {
                    "sym": sym, "tf": tf,
                    "pnl": 0.0, "n_trades": 0, "wr": 0.0, "max_dd": 0.0,
                    "decision": "timeout",
                }
            except Exception as e:
                results[key] = {
                    "sym": sym, "tf": tf,
                    "pnl": 0.0, "n_trades": 0, "wr": 0.0, "max_dd": 0.0,
                    "decision": f"error:{type(e).__name__}",
                    "error": str(e)[:200],
                }

    return results


def _detect_load_avg() -> float:
    """Get current 1-min load average."""
    try:
        return os.getloadavg()[0]
    except (OSError, AttributeError):
        return 0.0


def _detect_cpu_count() -> int:
    """Get CPU count."""
    return os.cpu_count() or 1
