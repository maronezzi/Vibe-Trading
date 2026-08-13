"""
vt_loser_replay.py — Wave N+5B (2026-07-08)

Replay automático de losing trades: pra cada perda do dia, busca em
signal_blocked_log (Wave N+1) setups idênticos nas últimas 24h e
computa o que teria acontecido (com base em outcome_pnl_pts já resolvido).

Output: relatório ``monitoring/reports/loser_replay_<date>.json`` com
hipóteses rankeadas por impacto:
- H1: "se filtro X não existisse" → blocked_gain_ratio.
- H2: "se filtro Y novo existisse" (futuro) → prevented_loss_ratio.

Ingest pelo AGI: optimization/agi_v4/stage2_intel.py lê o report como
input de hipóteses (wave futuro).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("vt_loser_replay")

# Default reports em monitoring/reports/ — usuário pode override via env.
DEFAULT_REPORTS_DIR = Path(
    os.environ.get(
        "VT_REPORTS_DIR",
        "/home/bruno/Projects/Vibe-Trading/monitoring/reports",
    )
)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def generate_report(
    *,
    db_path: Path,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    lookback_days: int = 1,
) -> Path:
    """Gera report ``loser_replay_<YYYY-MM-DD>.json`` aggregating hipóteses.

    Args:
        db_path: path do `vt_trades.db` (mesmo que signal_journal + edge_estimator).
        reports_dir: onde salvar (default monitoring/reports/).
        lookback_days: janela de análise (default 1 = só hoje).

    Returns:
        Path do arquivo gerado.
    """
    reports_dir.mkdir(parents=True, exist_ok=True)
    cutoff = (datetime.now() - timedelta(days=lookback_days)).isoformat()
    today = datetime.now().date().isoformat()

    conn = _connect(db_path)
    try:
        # 1. losing trades do dia.
        losing_rows = conn.execute(
            """
            SELECT id, symbol, timeframe AS tf, strategy, direction, entry_time,
                   exit_time, net_pnl
            FROM trades
            WHERE net_pnl < 0
              AND entry_time >= ?
            """,
            (cutoff,),
        ).fetchall()

        # 2. blocked setups nas últimas 24h que match losing trade.
        # Heurística: mesmo (symbol_root, strategy) — gera lista de matching.
        hypotheses_per_strategy: dict[str, list[dict]] = {}
        for tr in losing_rows:
            sym_root = _symbol_root(tr["symbol"])
            strategy = tr["strategy"] or "UNKNOWN"
            blocked = conn.execute(
                """
                SELECT COUNT(*) AS n_blocked,
                       AVG(outcome_pnl_pts) AS avg_pnl_pts,
                       SUM(CASE WHEN outcome_win = 1 THEN 1 ELSE 0 END) AS wins
                FROM signal_blocked_log
                WHERE strategy = ?
                  AND (symbol = ? OR symbol LIKE ?)
                  AND block_reason != 'MTF_LOW_SCORE'
                  AND ts >= ?
                  AND resolved = 1
                """,
                (strategy, tr["symbol"], f"{sym_root}%", cutoff),
            ).fetchone()
            if blocked and blocked["n_blocked"]:
                key = f"{sym_root}_{strategy}_{tr['tf']}"
                hypotheses_per_strategy.setdefault(key, []).append({
                    "losing_trade_id": tr["id"],
                    "losing_pnl_brl": float(tr["net_pnl"]),
                    "blocked_count": int(blocked["n_blocked"]),
                    "blocked_avg_pnl_brl": float(blocked["avg_pnl_pts"] or 0.0),
                    "blocked_wins": int(blocked["wins"] or 0),
                    "would_have_saved_brl": _would_have_saved(
                        int(blocked["n_blocked"]),
                        float(blocked["avg_pnl_pts"] or 0.0),
                    ),
                })
    finally:
        conn.close()

    # Ranking por impacto (R$ salvo hipotético).
    flat_hypotheses = []
    for key, hyps in hypotheses_per_strategy.items():
        total_saved = sum(h["would_have_saved_brl"] for h in hyps)
        flat_hypotheses.append({
            "key": key,
            "n_losers": len(hyps),
            "total_would_have_saved_brl": total_saved,
            "details": hyps,
        })
    flat_hypotheses.sort(key=lambda x: x["total_would_have_saved_brl"], reverse=True)

    report = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "lookback_days": lookback_days,
        "n_losing_trades": len(losing_rows),
        "hypotheses": flat_hypotheses[:20],  # top 20
    }

    out_path = reports_dir / f"loser_replay_{today}.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    log.info(f"loser_replay report: {out_path} ({len(flat_hypotheses)} hyps)")
    return out_path


def _would_have_saved(n_blocked: int, avg_blocked_pnl: float) -> float:
    """Heurística conservadora: média * count (sem clamp — pode ser negativo
    se filtros bloquearam winners com mais frequência que losers).

    Nota: aqui contamos blocked setups que RESULTARIAM em win (outcome_win=1).
    Nosso avg_blocked_pnl já é a média incluindo wins+losses. Se filtros
    bloqueiam setups que viraram winners, "would_have_saved" é negativo
    (= deixar passar teria sido lucro). Relatório não decide — humano
    valida na revisão semanal.
    """
    return n_blocked * avg_blocked_pnl


def _symbol_root(symbol: str) -> str:
    for r in ("WIN", "WDO", "BIT", "DOL", "IND", "WSP"):
        if r in symbol:
            return r
    return symbol


__all__ = ["generate_report", "DEFAULT_REPORTS_DIR"]
