# -*- coding: utf-8 -*-
"""
swap_scorecard — scorecard de entregas das mudanças do AGI (Wave 883.B2)

Problema (auditoria 29/08/2026): o journal acumula swaps com `pnl_claimed`
que NUNCA foram cobrados. Evidência empírica: WDO_M15 swap alegando sim
positiva entregou -R$405 no live (live_kill 26/08); shadow agregado
-R$6.083 em 14d enquanto todas as sims 30d eram positivas. O score
in-sample não prediz entrega — e ninguém conferia depois.

Este módulo é o "conferidor de recibos": para cada troca aplicada
(kind="swap", com pnl_claimed) com idade >= MIN_PREGÕES pregões, compara
o PnL ENTREGUE (live `trades` + shadow `forward_sim_trades` na janela da
troca) contra o alegado no momento da decisão.

MODO OBSERVAÇÃO (decisão Bruno 29/08): nesta wave o scorecard só REPORTA
(Telegram no Stage 6 + audit JSON). Nenhuma mudança de gate, nenhuma
escrita de config, nenhuma quarentena. O escalonamento (Gate B 1,3→2,0
para pares com ratio <0,5 e quarentena automática para <0,25) só entra
depois de ~2 semanas de dados, em wave própria com aprovação.

House rule "nunca treinar com trades passados": leitura de trades para
ACCOUNTABILITY de decisão já tomada (mesma natureza do live_kill_switch
e do risk_calibrator — gestão de risco), não treino/otimização. Nenhuma
estratégia/params nasce daqui.

Módulo PURO (journal + sqlite, somente leitura). Fail-safe: erro aqui
nunca derruba o pipeline.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

JOURNAL_PATH = Path(__file__).parent / "state" / "pair_change_journal.json"
_DB_FALLBACK = Path("/home/bruno/Projects/Vibe-Trading/vt_trades.db")


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def enabled() -> bool:
    return os.environ.get("VT_AGI_SCORECARD", "1") == "1"


def _db_path(config: dict) -> Path | None:
    try:
        from .stage1_collect import _resolve_db_path
        p = _resolve_db_path(config)
        if p:
            return Path(p)
    except Exception:
        pass
    return _DB_FALLBACK if _DB_FALLBACK.exists() else None


def _pair_pnl_window(db_path: Path | None, pair: str, start: str, end: str) -> dict:
    """PnL live+shadow do par na janela [start, end) (datas YYYY-MM-DD).

    Live: tabela `trades` (fechados, sem GHOST — espelho da query do
    live_kill_switch). Shadow: `forward_sim_trades` (walker, magic 555599).
    """
    root = pair.split("_", 1)[0]
    tf = pair.split("_", 1)[1] if "_" in pair else ""
    out = {"live_pnl": 0.0, "live_n": 0, "shadow_pnl": 0.0, "shadow_n": 0}
    if not db_path or not Path(db_path).exists():
        return out
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            """SELECT net_pnl FROM trades
               WHERE symbol LIKE ? AND timeframe = ?
                 AND date(entry_time) >= ? AND date(entry_time) < ?
                 AND exit_time IS NOT NULL AND exit_reason != 'GHOST'""",
            (f"{root}%", tf, start, end),
        ).fetchall()
        for (pnl,) in rows:
            out["live_pnl"] += float(pnl or 0)
            out["live_n"] += 1
        rows = conn.execute(
            """SELECT net_pnl_brl FROM forward_sim_trades
               WHERE symbol LIKE ? AND timeframe = ?
                 AND date(entry_time) >= ? AND date(entry_time) < ?""",
            (f"{root}%", tf, start, end),
        ).fetchall()
        for (pnl,) in rows:
            out["shadow_pnl"] += float(pnl or 0)
            out["shadow_n"] += 1
        conn.close()
    except Exception:
        pass
    return out


def _count_pregoes(db_path: Path | None, start: str) -> int:
    """Pregões (datas distintas com trade live) desde `start` — aproximação
    de idade útil para não contar fim de semana."""
    if not db_path or not Path(db_path).exists():
        return 0
    try:
        conn = sqlite3.connect(str(db_path))
        (n,) = conn.execute(
            "SELECT count(DISTINCT date(entry_time)) FROM trades WHERE date(entry_time) >= ?",
            (start,),
        ).fetchone()
        conn.close()
        return int(n or 0)
    except Exception:
        return 0


def run(ctx: dict) -> dict:
    """Scorecard de observação. Retorna dict para o Stage 6/audit.

    Returns:
        {
          "n_scored": int,
          "aggregate": {"claimed": f, "delivered": f, "ratio": f},
          "swaps": [{pair, date, claimed, live, shadow, delivered, ratio, pregoes}],
          "worst": [mesmo formato, os 3 piores por ratio],
          "mode": "observacao",
        }
    """
    if not enabled():
        return {"n_scored": 0, "mode": "desligado por env"}
    min_pregoes = _env_int("VT_AGI_SCORECARD_MIN_PREGOES", 5)

    if not JOURNAL_PATH.exists():
        return {"n_scored": 0, "mode": "observacao", "note": "journal inexistente"}

    try:
        events = json.loads(JOURNAL_PATH.read_text())
    except Exception as e:
        return {"n_scored": 0, "mode": "observacao", "error": str(e)[:200]}
    if not isinstance(events, list):
        return {"n_scored": 0, "mode": "observacao", "error": "journal malformado"}

    db = _db_path(ctx.get("config") or {})
    today = datetime.now().strftime("%Y-%m-%d")

    # Janela de cada swap = [data do swap, próxima decisão no mesmo par)
    by_pair: dict = {}
    swaps = [e for e in events
             if isinstance(e, dict) and e.get("kind") == "swap"
             and float(e.get("pnl_claimed") or 0) > 0]
    for e in events:
        if isinstance(e, dict) and e.get("pair"):
            by_pair.setdefault(e["pair"], []).append(e.get("ts", ""))

    scored = []
    for e in swaps:
        start = (e.get("ts") or "")[:10]
        if not start:
            continue
        pregoes = _count_pregoes(db, start)
        if pregoes < min_pregoes:
            continue  # novo demais — sem idade mínima não há recibo a conferir
        nexts = sorted(t for t in by_pair.get(e["pair"], []) if t[:10] > start)
        end = nexts[0][:10] if nexts else today
        if end <= start:
            end = today
        pnl = _pair_pnl_window(db, e["pair"], start, end)
        claimed = float(e.get("pnl_claimed") or 0)
        delivered = pnl["live_pnl"] + pnl["shadow_pnl"]
        ratio = round(delivered / claimed, 2) if claimed > 0 else None
        scored.append({
            "pair": e["pair"],
            "date": start,
            "claimed": round(claimed, 2),
            "live": round(pnl["live_pnl"], 2),
            "shadow": round(pnl["shadow_pnl"], 2),
            "delivered": round(delivered, 2),
            "ratio": ratio,
            "pregoes": pregoes,
        })

    scored.sort(key=lambda s: s["ratio"] if s["ratio"] is not None else 9.9)
    agg_claimed = sum(s["claimed"] for s in scored)
    agg_delivered = sum(s["delivered"] for s in scored)
    return {
        "mode": "observacao",
        "n_scored": len(scored),
        "min_pregoes": min_pregoes,
        "aggregate": {
            "claimed": round(agg_claimed, 2),
            "delivered": round(agg_delivered, 2),
            "ratio": round(agg_delivered / agg_claimed, 2) if agg_claimed > 0 else None,
        },
        "swaps": scored,
        "worst": scored[:3],
    }


def telegram_line(result: dict) -> str | None:
    """Linha compacta para o relatório do Stage 6. None se nada a mostrar."""
    if not result or not result.get("n_scored"):
        return None
    agg = result.get("aggregate", {})
    claimed = agg.get("claimed", 0)
    delivered = agg.get("delivered", 0)
    parts = [f"alegado R${claimed:+.0f} → entregue R${delivered:+.0f} "
             f"({result['n_scored']} swaps ≥{result.get('min_pregoes', 5)} pregões)"]
    for w in result.get("worst", [])[:3]:
        if w.get("ratio") is not None:
            parts.append(f"{w['pair']} r={w['ratio']:.2f} ({w['date']})")
    return "📋 Scorecard trocas: " + " | ".join(parts)
