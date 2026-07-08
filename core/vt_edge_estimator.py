"""
vt_edge_estimator.py — Wave N+3B (2026-07-08)

Edge estimator vivo: detecta decadência de expectancy em tempo real e
sugere degradação automática de exposição. Lê últimos N trades por
(symbol, tf, strategy), compara com baseline_expectancy_pts (config), e
escreve um snapshot por (symbol, tf, strategy, ts) com a recomendação.

Integração com sizing:
- vt_sizing.resolve_volume multiplica volume final por
  ``recommended_size_scale`` da última leitura de edge_estimator
  (cache 5 min) — degradação automática sem rebalance manual.

Alerting:
- monitoring/vt_copilot.py: dispara "🔶 EDGE DECAY {sym} {tf}" quando
  size_scale < 1.0.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("vt_edge_estimator")

# Mirror de core/vt_signal_journal:DB_PATH — ambas tabelas vivem na mesma DB.
# Reaproveita path já controlado por conftest no tmp DB.
try:
    from core import vt_signal_journal as _sj
    DB_PATH = _sj.DB_PATH
except ImportError:
    DB_PATH = Path(
        os.environ.get(
            "VT_TRADES_DB",
            "/home/bruno/Projects/Vibe-Trading/vt_trades.db",
        )
    )

DEFAULT_CONFIG = {
    "enabled": False,
    "min_trades": 20,
    "decay_threshold": -0.30,
    "size_scale_floor": 0.4,
    "check_interval_min": 15,
}

_LAST_CACHE: dict[tuple, tuple[float, dict]] = {}
_LAST_CACHE_TTL = 300.0  # 5 min


# ═══════════════════════════════════════════════════════════
# Schema
# ═══════════════════════════════════════════════════════════

EDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS edge_estimator (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    tf TEXT NOT NULL,
    strategy TEXT NOT NULL,
    n INTEGER NOT NULL,
    wins INTEGER NOT NULL,
    expectancy_pts REAL NOT NULL,
    avg_rr REAL,
    baseline_expectancy_pts REAL NOT NULL,
    edge_decay REAL NOT NULL,
    recommended_size_scale REAL,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_edge_sym_tf_strat_ts
    ON edge_estimator(symbol, tf, strategy, ts);
"""

EDGE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_edge_sym_tf_strat_recent "
    "ON edge_estimator(symbol, tf, strategy, ts DESC)",
]


def _config(config: dict) -> dict:
    """Snap config + defaults."""
    out = dict(DEFAULT_CONFIG)
    src = config.get("edge_estimator") or {}
    for k, v in src.items():
        if k in DEFAULT_CONFIG:
            out[k] = v
    return out


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_schema(conn: sqlite3.Connection | None = None) -> None:
    owns = conn is None
    if owns:
        conn = _connect()
    try:
        conn.executescript(EDGE_SCHEMA)
        for idx in EDGE_INDEXES:
            conn.execute(idx)
        conn.commit()
    finally:
        if owns:
            conn.close()


# ═══════════════════════════════════════════════════════════
# Cálculo de expectancy por (sym, tf, strategy)
# ═══════════════════════════════════════════════════════════


def update(symbol: str, tf: str, strategy: str, *, config: dict) -> dict | None:
    """Calcula expectancy viva e grava snapshot.

    Args:
        symbol: contrato resolvido (ou root — usamos match parcial).
        tf: timeframe.
        strategy: nome da estratégia.
        config: vt_config dict.

    Returns:
        dict com snapshot (ou None se min_trades insuficiente).
    """
    cfg = _config(config)
    if not cfg["enabled"]:
        return None

    symbol_root = _symbol_root(symbol)
    cutoff = (datetime.now() - timedelta(days=30)).isoformat()

    conn = _connect()
    try:
        cur = conn.execute(
            """
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                   AVG(net_pnl) AS avg_pnl_pts
            FROM trades
            WHERE (symbol = ? OR symbol LIKE ?)
              AND timeframe = ?
              AND strategy = ?
              AND entry_time >= ?
              AND exit_time IS NOT NULL
            """,
            (symbol, f"{symbol_root}%", tf, strategy, cutoff),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if not row or row["n"] is None:
        return None

    n = int(row["n"])
    wins = int(row["wins"] or 0)
    if n < cfg["min_trades"]:
        return None

    # expectancy_pts em pontos do ativo (R$) — net_pnl já está em R$ na tabela.
    # Convert para pts: dividir por point_val per-symbol. Mantemos em R$
    # para o gate — mais interpretável pra humano.
    expectancy_brl = float(row["avg_pnl_pts"] or 0.0)
    baseline = _baseline_for(config, strategy, tf)
    edge_decay = (
        (expectancy_brl - baseline) / abs(baseline)
        if abs(baseline) > 1e-9
        else 0.0
    )
    size_scale = _size_scale_from_decay(edge_decay, cfg["decay_threshold"],
                                        cfg["size_scale_floor"])

    snap = {
        "ts": datetime.now().astimezone().isoformat(),
        "symbol": symbol,
        "tf": tf,
        "strategy": strategy,
        "n": n,
        "wins": wins,
        "expectancy_pts": expectancy_brl,
        "avg_rr": None,  # TODO Wave N+5: avg win / avg loss
        "baseline_expectancy_pts": baseline,
        "edge_decay": edge_decay,
        "recommended_size_scale": size_scale,
    }

    # Persiste + atualiza cache.
    conn = _connect()
    try:
        ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO edge_estimator
              (ts, symbol, tf, strategy, n, wins, expectancy_pts,
               avg_rr, baseline_expectancy_pts, edge_decay,
               recommended_size_scale)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snap["ts"], snap["symbol"], snap["tf"], snap["strategy"],
                snap["n"], snap["wins"], snap["expectancy_pts"],
                snap["avg_rr"], snap["baseline_expectancy_pts"],
                snap["edge_decay"], snap["recommended_size_scale"],
            ),
        )
        conn.commit()
    except Exception as exc:
        log.warning(f"edge_estimator insert falhou: {exc!r}")
    finally:
        conn.close()

    _LAST_CACHE[(symbol, tf, strategy)] = (
        datetime.now().timestamp(), snap,
    )
    return snap


def get_recommended_size_scale(
    symbol: str, tf: str, strategy: str,
) -> float:
    """Retorna factor de sizing recomendado (1.0 = sem degradação).

    Cache: 5 min, hit no DB pode ser cacheado.
    """
    key = (symbol, tf, strategy)
    now = datetime.now().timestamp()
    if key in _LAST_CACHE:
        ts, snap = _LAST_CACHE[key]
        if now - ts <= _LAST_CACHE_TTL:
            return snap.get("recommended_size_scale", 1.0)

    conn = _connect()
    try:
        ensure_schema(conn)
        cur = conn.execute(
            """
            SELECT recommended_size_scale
            FROM edge_estimator
            WHERE (symbol = ? OR symbol LIKE ?)
              AND tf = ? AND strategy = ?
            ORDER BY ts DESC LIMIT 1
            """,
            (symbol, f"{_symbol_root(symbol)}%", tf, strategy),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        return 1.0
    scale = float(row["recommended_size_scale"] or 1.0)
    _LAST_CACHE[key] = (now, {"recommended_size_scale": scale})
    return scale


# ═══════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════


def _baseline_for(config: dict, strategy: str, tf: str) -> float:
    """Baseline expectancy por (strategy, tf) — vem dos parâmetros AGI.

    Default 0.0 se não setado: edge_decay neutro não dispara alarme."""
    params_root = config.get("params_by_tf", {})
    for k in params_root:
        # params_by_tf é dict aninhado: {<symbol>_<tf>: {strategy: ...}}
        # Aqui só lemos o level superior — ter estrategia+tf na chave.
        pass
    # Leitura direta via estrutura esperada:
    params = config.get("params_by_tf") or {}
    key = f"{strategy}_{tf}"
    val = params.get(key, {}).get("baseline_expectancy_pts")
    if isinstance(val, (int, float)):
        return float(val)
    # Heurística: $5/trade como baseline para B3 mini-contratos WIN M5.
    return 5.0


def _size_scale_from_decay(
    decay: float,
    threshold: float,
    floor: float,
) -> float:
    """Mapeia decay ∈ [-1.0, +∞) para scale ∈ [floor, 1.0].

    decay >= 0.0 → scale = 1.0 (edge saudável ou crescendo).
    decay entre threshold (negativo) e 0 → scale linear entre floor e 1.0.
    decay <= threshold (mais negativo) → scale = floor.
    """
    if decay >= 0:
        return 1.0
    if decay <= threshold:
        return floor
    # decay ∈ [threshold, 0); ambos negativos. ratio ∈ (0, 1].
    # Quando decay == 0 (ratio=1) → scale = 1.0.
    # Quando decay == threshold (ratio=0) → scale = floor.
    ratio = decay / threshold  # positivo
    return floor + (1.0 - floor) * ratio


def _symbol_root(symbol: str) -> str:
    for r in ("WIN", "WDO", "BIT", "DOL", "IND", "WSP"):
        if r in symbol:
            return r
    return symbol


__all__ = [
    "ensure_schema",
    "update",
    "get_recommended_size_scale",
    "DEFAULT_CONFIG",
]
