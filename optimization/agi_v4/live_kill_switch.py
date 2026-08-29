# -*- coding: utf-8 -*-
"""
live_kill_switch — kill-switch LIVE por par (Wave 880.II, 26/08/2026)

Problema: a soberania do AGI v4 decide entra/sai 100% pela SIMULAÇÃO. Um par
pode ter sim 30d positiva (ex: WDO_M15/ADX_TREND +R$450) e ser cronicamente
negativo no LIVE (-R$337 em 14d) — nenhum mecanismo desativava. O gate
`live_reality` congela MUDANÇAS no dia do sangramento, mas o par segue
operando (proteção zero de capital).

Este módulo é o lado "sangramento real" da soberania:
- Regra BLEED: par com n ≥ VT_AGI_LIVE_KILL_MIN_TRADES trades e
  PnL live ≤ VT_AGI_LIVE_KILL_PNL na janela de VT_AGI_LIVE_KILL_DAYS
  pregões → DESATIVA (disabled_timeframes + day_trade_intent=false).
- Regra CHURN: n ≥ VT_AGI_LIVE_CHURN_MIN_TRADES e PnL ≤
  VT_AGI_LIVE_CHURN_PNL (morte por comissão — ex: BIT_M15/DIVERGENCE_RSI
  com 39 trades e -R$40 em 14d) → DESATIVA.

NOTA — house rule "nunca treinar com trades passados": kill-switch NÃO é
treino/otimização — é gestão de risco (mesma natureza do risk_calibrator,
que já lê a tabela `trades` para calibrar stops). Nenhuma decisão de
estratégia/params nasce daqui; ela só TIRA par do ar.

Quarentena: par live-killed só pode ser reativado depois de
VT_AGI_LIVE_QUARANTINE_DAYS dias (o gate vive em stage5_apply; o journal
`kind="live_kill"` é a fonte). Evita o ciclo desativa → sim bonita
reativa → sangra de novo.

Módulo PURO (sqlite + env). O WRITE fica em stage5_apply (único writer
autorizado do AGI v4). Fail-open em tudo: erro aqui NUNCA derruba o
pipeline e NUNCA desativa sem evidência mínima.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def enabled() -> bool:
    return os.environ.get("VT_AGI_LIVE_KILL", "1") == "1"


def _db_path(config: dict) -> Path | None:
    try:
        from .stage1_collect import _resolve_db_path
        p = _resolve_db_path(config)
        if p:
            return Path(p)
    except Exception:
        pass
    p = Path("/home/bruno/Projects/Vibe-Trading/vt_trades.db")
    return p if p.exists() else None


def _load_pair_pnl(db_path: Path | None, days: int) -> dict:
    """PnL live por par (root_tf) na janela — espelho da query do
    risk_calibrator (sem GHOST, só fechados)."""
    if not db_path or not Path(db_path).exists():
        return {}
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            """SELECT symbol, timeframe, net_pnl
               FROM trades
               WHERE entry_time >= ? AND exit_time IS NOT NULL
                 AND exit_reason != 'GHOST'
               ORDER BY entry_time""",
            (cutoff,),
        ).fetchall()
        conn.close()
    except Exception:
        return {}
    agg: dict = {}
    for sym, tf, pnl in rows:
        if not sym or not tf:
            continue
        pair = f"{sym[:3]}_{tf}"
        a = agg.setdefault(pair, {"n": 0, "pnl": 0.0})
        a["n"] += 1
        a["pnl"] += float(pnl or 0)
    return agg


def evaluate(config: dict, db_path: Path | None = None) -> list[dict]:
    """Avalia todos os pares ATIVOS contra as regras live.

    Returns:
        Lista de decisões {pair, rule, pnl, n_trades, days} — o caller
        (stage5_apply.live_kill_switch_pass) aplica via save_full_config.
    """
    if not enabled():
        return []
    cfg = config or {}
    strategy_by_tf = cfg.get("strategy_by_tf", {}) or {}
    disabled = set(cfg.get("disabled_timeframes", []) or [])
    if not strategy_by_tf:
        return []

    days = max(_env_int("VT_AGI_LIVE_KILL_DAYS", 10), 1)
    min_n = max(_env_int("VT_AGI_LIVE_KILL_MIN_TRADES", 10), 1)
    kill_pnl = _env_float("VT_AGI_LIVE_KILL_PNL", -200.0)
    churn_n = max(_env_int("VT_AGI_LIVE_CHURN_MIN_TRADES", 30), 1)
    churn_pnl = _env_float("VT_AGI_LIVE_CHURN_PNL", -20.0)

    path = db_path if db_path is not None else _db_path(cfg)
    agg = _load_pair_pnl(path, days)

    decisions = []
    for pair in strategy_by_tf:
        if pair in disabled:
            continue  # já desativado — nada a fazer
        a = agg.get(pair)
        if not a or a["n"] <= 0:
            continue
        if a["n"] >= min_n and a["pnl"] <= kill_pnl:
            decisions.append({
                "pair": pair, "rule": "live_bleed",
                "pnl": round(a["pnl"], 2), "n_trades": a["n"], "days": days,
                "quarantine_days": _env_int("VT_AGI_LIVE_QUARANTINE_DAYS", 10),
            })
        elif a["n"] >= churn_n and a["pnl"] <= churn_pnl:
            decisions.append({
                "pair": pair, "rule": "live_churn",
                "pnl": round(a["pnl"], 2), "n_trades": a["n"], "days": days,
                "quarantine_days": _env_int("VT_AGI_LIVE_QUARANTINE_DAYS", 10),
            })
    return decisions


def is_quarantined(pair: str, journal_entries: list,
                   now: datetime | None = None) -> tuple[bool, str]:
    """Par live-killed na janela de quarentena não pode ser reativivo pela
    simulação (a sim que o kill-switch contradiz não é evidência suficiente
    p/ religar antes da quarentena acabar).

    Returns:
        (True, motivo) se bloqueado; (False, "") se livre.
    """
    now = now or datetime.now()
    days = max(_env_int("VT_AGI_LIVE_QUARANTINE_DAYS", 10), 0)
    if days <= 0:
        return False, ""
    best: datetime | None = None
    for e in journal_entries or []:
        if not isinstance(e, dict) or e.get("kind") != "live_kill":
            continue
        if e.get("pair") != pair:
            continue
        try:
            ts = datetime.fromisoformat(str(e.get("ts", "")))
        except (TypeError, ValueError):
            continue
        if ts <= now and (best is None or ts > best):
            best = ts
    if best is None:
        return False, ""
    age = (now - best).days
    if age < days:
        return True, (f"live_kill há {age}d < quarentena {days}d "
                      f"(desde {best.date().isoformat()})")
    return False, ""
