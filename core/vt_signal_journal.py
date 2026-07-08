"""
vt_signal_journal.py — Wave N+1 (2026-07-08)

Log contrafactual: captura sinais que estratégia DESCART (retornou None), mas
para os quais havia setup latente (mesma estratégia no mesmo (symbol, tf)
gerou sinal nos últimos 30 minutos).

Fundamentação: ver docs/PLAN_REFATOR_PROXIMAS_WAVES_2026-07-08.md §4.

Por que existe:
1. Mede seletividade por estratégia: entries / (entries + blocked) — antes
   desta tabela, "filtros barraram setups" era invisível.
2. Alimenta Wave N+3B (edge estimator): expectancy vs hipotética (se filtros
   não tivessem barrado).
3. Alimenta Wave N+5B (loser replay): hipóteses de novos filtros.

Garantias:
- Batch insert (50 rows ou 30s) — não bloqueia hot-loop do autotrader.
- Idempotente por chave única (ts, symbol, tf, direction) — resolve() seguro.
- Sem raise: append/flush NUNCA interrompe trading. Falha de DB volta rows
  ao buffer e loga warning.
- Resolve() só toca rows com `resolved=0` e `ts < now - window_minutes`.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("vt_signal_journal")

DB_PATH = Path("/home/bruno/Projects/Vibe-Trading/vt_trades.db")

# Heurística "setup latente vs sem setup": estratégia no mesmo (sym, tf) deve
# ter gerado sinal nos últimos N minutos para qualificar como filter-reject.
LATENT_LOOKBACK_MINUTES = 30

# Auto-flush periódico (evita INSERT em hot-loop, reduz contenção SQLite WAL).
_BATCH_SIZE = 50
_BATCH_INTERVAL_S = 30.0

# Buffer em memória — lists de rows; tuples alinhadas com schema.
_blocked_buffer: list[tuple] = []
# Inicializado em import-time: garante que o primeiro log_blocked_signal
# NÃO dispara auto-flush por "delta > 30s" (que aconteceria se fosse 0.0).
_last_flush_ts: float = time.time()


def _now_iso() -> str:
    """ISO local com timezone (consistente com outras tabelas VT)."""
    return datetime.now().astimezone().isoformat()


def _connect() -> sqlite3.Connection:
    """Conexão SQLite — espelha padrão de core/vt_trade_log.py."""
    conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ═══════════════════════════════════════════════════════════════════
# Schema + migration (chamado por autotrader no boot OU por init_db externo)
# ═══════════════════════════════════════════════════════════════════

_BLOCKED_SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_blocked_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    tf TEXT NOT NULL,
    strategy TEXT NOT NULL,
    direction TEXT,
    block_reason TEXT NOT NULL,
    hypothetical_sl_pts INTEGER,
    hypothetical_atr_pts REAL,
    regime TEXT,
    resolved INTEGER DEFAULT 0,
    outcome_win INTEGER,
    outcome_pnl_pts REAL,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(ts, symbol, tf, direction, strategy)
);
"""

_BLOCKED_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_blocked_sym_tf_strat_ts "
    "ON signal_blocked_log(symbol, tf, strategy, ts)",
    "CREATE INDEX IF NOT EXISTS idx_blocked_resolved_ts "
    "ON signal_blocked_log(resolved, ts)",
]


def ensure_schema(conn: sqlite3.Connection | None = None) -> None:
    """Garante que a tabela + indexes existem. Idempotente.

    Args:
        conn: conexão SQLite opcional. Se None, abre e fecha uma nova.
            Permite batch com outras migrations no mesmo conn.
    """
    owns_conn = conn is None
    if owns_conn:
        conn = _connect()
    try:
        conn.executescript(_BLOCKED_SCHEMA)
        for idx in _BLOCKED_INDEXES:
            conn.execute(idx)
        conn.commit()
    finally:
        if owns_conn:
            conn.close()


# ═══════════════════════════════════════════════════════════════════
# API pública
# ═══════════════════════════════════════════════════════════════════


def log_blocked_signal(
    symbol: str,
    tf: str,
    strategy: str,
    *,
    direction: str | None,
    block_reason: str,
    sl_pts: int | None = None,
    atr_pts: float | None = None,
    regime: str | None = None,
    ts: str | None = None,
) -> None:
    """Enfileira um setup latente barrado. Auto-flush via _blocked_buffer.

    Args:
        symbol: ativo (ex.: 'WINQ26', 'WDO$M5' → root 'WDO').
        tf: timeframe (ex.: 'M5', 'M15', 'H1').
        strategy: nome da estratégia (ex.: 'ADX_TREND').
        direction: 'BUY'/'SELL' ou None se filtro barrou antes.
        block_reason: tag do filtro (ex.: 'MTF_LOW_SCORE', 'VOL_FILTER',
            'ADX_THRESHOLD', 'LOSS_COOLDOWN', 'DAY_TRADE_BLOCK').
        sl_pts: SL hipotético em pts (do check_entry se chegou a computar).
        atr_pts: ATR no momento do sinal — usado em Wave N+3B para vol-normalized.
        regime: regime atual ('TREND'/'RANGE'/'VOL_EXPANSION'/None).
        ts: ISO timestamp (default now); permite injetar p/ testes.
    """
    global _last_flush_ts
    row = (
        ts or _now_iso(),
        symbol,
        tf,
        strategy,
        direction,
        block_reason,
        sl_pts,
        atr_pts,
        regime,
        0,   # resolved
        None,  # outcome_win
        None,  # outcome_pnl_pts
    )
    _blocked_buffer.append(row)
    if (
        len(_blocked_buffer) >= _BATCH_SIZE
        or (time.time() - _last_flush_ts) > _BATCH_INTERVAL_S
    ):
        flush()


def flush() -> int:
    """Persiste buffer no DB. Idempotente. Retorna linhas persistidas.

    Falha silenciosa: rows voltam ao buffer (mantém integridade, evita perda).
    Próximo tick ou próximo append poderá re-flushar.
    """
    global _last_flush_ts, _blocked_buffer
    if not _blocked_buffer:
        return 0
    rows = _blocked_buffer
    _blocked_buffer = []
    try:
        conn = _connect()
        try:
            conn.executemany(
                """
                INSERT OR IGNORE INTO signal_blocked_log
                (ts, symbol, tf, strategy, direction, block_reason,
                 hypothetical_sl_pts, hypothetical_atr_pts, regime,
                 resolved, outcome_win, outcome_pnl_pts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
        finally:
            conn.close()
        _last_flush_ts = time.time()
        log.debug(f"signal_journal flush OK ({len(rows)} rows)")
        return len(rows)
    except Exception as exc:
        log.warning(
            f"signal_journal flush falhou: {exc!r}; "
            f"devolvendo {len(rows)} rows ao buffer"
        )
        _blocked_buffer = rows + _blocked_buffer  # devolve ao buffer
        return 0


def resolve_blocked_outcomes(
    window_minutes: int = 120,
    fetcher: Any | None = None,
) -> int:
    """Resolve rows pendentes (resolved=0 e ts<cutoff) consultando preço futuro.

    Args:
        window_minutes: idade mínima para resolver (default 2h).
        fetcher: callable(symbol, tf) -> dict|None com {entry_price, exit_price}
            ou None se sem dados. Default usa mt5_orchestrator.bars. Override
            em testes via monkeypatch.

    Returns:
        Número de rows resolvidas (resolved=1) nesta execução.

    Regras de win/loss (heurística conservadora, refina com Wave N+5B):
        BUY  ganha se exit_price > entry_price
        SELL ganha se exit_price < entry_price
        Empate = win=None (resolvido sem classificação).
        pnl_pts = abs(exit - entry) - hypothetical_sl_pts (clamp ≤0 se SL
        teria sido atingido antes).
    """
    if fetcher is None:
        fetcher = _default_fetcher

    cutoff_ts = (
        datetime.now().astimezone().timestamp() - window_minutes * 60
    )
    cutoff_iso = datetime.fromtimestamp(cutoff_ts).astimezone().isoformat()

    conn = _connect()
    try:
        ensure_schema(conn)
        cur = conn.execute(
            "SELECT id, symbol, tf, strategy, direction, "
            "hypothetical_sl_pts, ts "
            "FROM signal_blocked_log "
            "WHERE resolved = 0 AND ts < ?",
            (cutoff_iso,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return 0

    n_resolved = 0
    updates: list[tuple] = []  # (outcome_win, outcome_pnl_pts, id)
    for r in rows:
        try:
            data = fetcher(r["symbol"], r["tf"])
        except Exception as exc:
            log.debug(
                f"resolve fetcher falhou {r['symbol']}_{r['tf']}: {exc!r}"
            )
            continue
        if not data:
            continue
        entry = data.get("entry_price")
        exit_ = data.get("exit_price")
        if entry is None or exit_ is None:
            continue

        direction = r["direction"]
        if direction == "BUY":
            win = 1 if exit_ > entry else (0 if exit_ < entry else None)
        elif direction == "SELL":
            win = 1 if exit_ < entry else (0 if exit_ > entry else None)
        else:
            win = None  # filtro barrou antes da direção decidir
        delta = exit_ - entry
        if direction == "SELL":
            delta = -delta
        sl_pts = r["hypothetical_sl_pts"] or 0
        pnl_pts = abs(delta) - sl_pts
        # clamp: SL teria atingido primeiro se |delta| > sl_pts
        if abs(delta) > sl_pts:
            pnl_pts = -sl_pts  # sempre negativo no pior caso (max loss)

        updates.append((win, pnl_pts, r["id"]))
        n_resolved += 1

    if updates:
        try:
            conn = _connect()
            try:
                conn.executemany(
                    "UPDATE signal_blocked_log "
                    "SET resolved = 1, outcome_win = ?, outcome_pnl_pts = ? "
                    "WHERE id = ?",
                    [(w, p, rid) for w, p, rid in updates],
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            log.warning(f"signal_journal resolve update falhou: {exc!r}")
            return 0
    return n_resolved


def _default_fetcher(symbol: str, tf: str) -> dict | None:
    """Fetcher padrão — usa mt5_orchestrator.bars para obter entry/exit prices.

    Para (symbol, tf) que teve filter-reject em `ts`, busca o bar imediatamente
    APÓS `ts` no orchestrator e mais um bar N bars à frente. Retorna:
        {entry_price: bar_posterior_ts.close, exit_price: bar_N_close}.

    Mantém-se best-effort e conservadoramente silente — se a bridge Wine falhar,
    a row fica `resolved=0` e será tentada de novo no próximo tick do daemon.
    """
    try:
        from mt5 import mt5_orchestrator  # noqa: F401  # importa p/ sys.path
    except ImportError:
        return None
    try:
        bars_resp = mt5_orchestrator.bars(symbol, tf, count=60)
    except Exception:
        return None
    if isinstance(bars_resp, dict) and "error" in bars_resp:
        return None
    bars = bars_resp if isinstance(bars_resp, list) else bars_resp.get("bars")
    if not bars or len(bars) < 2:
        return None
    return {
        "entry_price": bars[0].get("close"),
        "exit_price": bars[-1].get("close"),
    }


# ═══════════════════════════════════════════════════════════════════
# Métricas (Wave N+3B consome)
# ═══════════════════════════════════════════════════════════════════


def compute_selectivity(
    strategy: str | None = None,
    days: int = 7,
) -> dict[str, Any]:
    """Top-level selectivity por estratégia.

    Returns:
        dict no formato:
            {
              "strategies": {
                "<strategy_name>": {
                  "entries": int,        # trades reais no período
                  "blocked": int,        # setups barrados no período
                  "selectivity": float,  # entries / (entries + blocked)
                  "reject_reasons": {"<reason>": count, ...},
                },
                ...
              },
              "global": {
                "entries": int, "blocked": int, "selectivity": float
              }
            }

    Selectivity = entries / (entries + blocked). ∈ [0, 1].
    - 0 = tudo barrado, nenhum trade.
    - 1 = tudo aceito (sem filtro efetivo).
    - ~0.4-0.6 = filtro saudável.
    """
    cutoff_ts = (
        datetime.now().astimezone().timestamp() - days * 86400
    )
    cutoff_iso = datetime.fromtimestamp(cutoff_ts).astimezone().isoformat()

    conn = _connect()
    try:
        blocked_q = (
            "SELECT strategy, block_reason, COUNT(*) AS n "
            "FROM signal_blocked_log "
            "WHERE ts >= ? "
        )
        blocked_args: tuple = (cutoff_iso,)
        if strategy:
            blocked_q += "AND strategy = ? "
            blocked_args = blocked_args + (strategy,)
        blocked_q += "GROUP BY strategy, block_reason"
        blocked_rows = conn.execute(blocked_q, blocked_args).fetchall()

        entries_q = (
            "SELECT strategy, COUNT(*) AS n "
            "FROM trades "
            "WHERE entry_time >= ? "
        )
        entries_args: tuple = (cutoff_iso,)
        if strategy:
            entries_q += "AND strategy = ? "
            entries_args = entries_args + (strategy,)
        entries_q += "GROUP BY strategy"
        entries_rows = conn.execute(entries_q, entries_args).fetchall()
    finally:
        conn.close()

    blocked_by_strat: dict[str, dict[str, int]] = {}
    for b in blocked_rows:
        blocked_by_strat.setdefault(b["strategy"], {})[b["block_reason"]] = b["n"]
    blocked_total_by_strat = {
        s: sum(rs.values()) for s, rs in blocked_by_strat.items()
    }
    entries_by_strat = {e["strategy"]: e["n"] for e in entries_rows}

    strategies = set(blocked_total_by_strat) | set(entries_by_strat)
    per_strat: dict[str, dict[str, Any]] = {}
    g_e, g_b = 0, 0
    for s in strategies:
        e = entries_by_strat.get(s, 0)
        b = blocked_total_by_strat.get(s, 0)
        per_strat[s] = {
            "entries": e,
            "blocked": b,
            "selectivity": (e / (e + b)) if (e + b) else 0.0,
            "reject_reasons": blocked_by_strat.get(s, {}),
        }
        g_e += e
        g_b += b
    return {
        "strategies": per_strat,
        "global": {
            "entries": g_e,
            "blocked": g_b,
            "selectivity": (g_e / (g_e + g_b)) if (g_e + g_b) else 0.0,
        },
    }


# ═══════════════════════════════════════════════════════════════════
# Helpers para testes
# ═══════════════════════════════════════════════════════════════════


def reset_buffer_for_test() -> None:
    """Limpa buffer + estado global singleton. Apenas para uso em testes."""
    global _blocked_buffer, _last_flush_ts
    _blocked_buffer = []
    _last_flush_ts = time.time()


__all__ = [
    "DB_PATH",
    "LATENT_LOOKBACK_MINUTES",
    "ensure_schema",
    "log_blocked_signal",
    "flush",
    "resolve_blocked_outcomes",
    "compute_selectivity",
    "reset_buffer_for_test",
]
