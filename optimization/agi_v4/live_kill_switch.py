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
- Regra STRATEGY-BLEED (Wave 894, 15/09): ESTRATÉGIA com n ≥
  VT_AGI_LIVE_STRAT_MIN_TRADES e PnL ≤ VT_AGI_LIVE_STRAT_PNL na janela
  → desativa TODOS os pares ativos que a usam. Motivação set/2026:
  AGI4_BIT_121102 sangrou -R$301 no WDO_M5 e migrou para WIN_M15/BIT_M30
  com "ficha limpa" — o kill por par não acompanha a estratégia (a
  granularidade errada já apontada no incidente 08/09 do WIN_M15).

Wave 894 (15/09) — calibração mais rápida (set/2026: setembro fechou
-R$802 com 7/9 pregões negativos; WDO_M5 sangrou 6 pregões antes do kill
pegar): MIN_TRADES 10→4 e KILL_PNL -200→-120. Com n=4 e -R$120 o
WDO_M5 teria sido morto em 10/09 (n=4, -R$167), salvando ~-R$164.
Validado contra setembro: nenhum par positivo teria sido morto
(DIVERGENCE_RSI -R$110/23t e ADX_TREND -R$90/12t ficam abaixo do gate).

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
    return _load_grouped_pnl(db_path, days, group="pair")


def _load_strategy_pnl(db_path: Path | None, days: int) -> dict:
    """PnL live por ESTRATÉGIA na janela (Wave 894) — a estratégia sangra
    independentemente do par em que o AGI a reassignou."""
    return _load_grouped_pnl(db_path, days, group="strategy")


def _load_grouped_pnl(db_path: Path | None, days: int, group: str) -> dict:
    if not db_path or not Path(db_path).exists():
        return {}
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            """SELECT symbol, timeframe, strategy, net_pnl
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
    for sym, tf, strat, pnl in rows:
        if group == "pair":
            if not sym or not tf:
                continue
            key = f"{sym[:3]}_{tf}"
        else:
            if not strat:
                continue
            key = str(strat)
        a = agg.setdefault(key, {"n": 0, "pnl": 0.0})
        a["n"] += 1
        a["pnl"] += float(pnl or 0)
    return agg


def evaluate(config: dict, db_path: Path | None = None) -> list[dict]:
    """Avalia todos os pares ATIVOS (e as estratégias em uso) contra as
    regras live.

    Returns:
        Lista de decisões {pair, rule, pnl, n_trades, days} para pares e
        {strategy, rule="live_strategy_bleed", pairs, pnl, n_trades, days}
        para estratégias — o caller (stage5_apply.live_kill_switch_pass)
        aplica via save_full_config.
    """
    if not enabled():
        return []
    cfg = config or {}
    strategy_by_tf = cfg.get("strategy_by_tf", {}) or {}
    disabled = set(cfg.get("disabled_timeframes", []) or [])
    if not strategy_by_tf:
        return []

    days = max(_env_int("VT_AGI_LIVE_KILL_DAYS", 10), 1)
    min_n = max(_env_int("VT_AGI_LIVE_KILL_MIN_TRADES", 4), 1)
    kill_pnl = _env_float("VT_AGI_LIVE_KILL_PNL", -120.0)
    churn_n = max(_env_int("VT_AGI_LIVE_CHURN_MIN_TRADES", 30), 1)
    churn_pnl = _env_float("VT_AGI_LIVE_CHURN_PNL", -20.0)
    strat_min_n = max(_env_int("VT_AGI_LIVE_STRAT_MIN_TRADES", 5), 1)
    strat_pnl = _env_float("VT_AGI_LIVE_STRAT_PNL", -150.0)

    path = db_path if db_path is not None else _db_path(cfg)
    agg = _load_pair_pnl(path, days)
    quarantine = _env_int("VT_AGI_LIVE_QUARANTINE_DAYS", 10)

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
                "quarantine_days": quarantine,
            })
        elif a["n"] >= churn_n and a["pnl"] <= churn_pnl:
            decisions.append({
                "pair": pair, "rule": "live_churn",
                "pnl": round(a["pnl"], 2), "n_trades": a["n"], "days": days,
                "quarantine_days": quarantine,
            })

    # ── Wave 894 (15/09): kill por ESTRATÉGIA — o bleed não respeita fronteira
    # de par; a estratégia que sangrou num par não pode recomeçar "limpa" em
    # outro. Desativa TODOS os pares ATIVOS que a usam na janela. ──
    strat_agg = _load_strategy_pnl(path, days)
    killed_strategies = set()
    for pair in strategy_by_tf:
        if pair in disabled:
            continue
        strat = strategy_by_tf.get(pair) or ""
        if not strat or strat in killed_strategies:
            continue
        a = strat_agg.get(strat)
        if not a or a["n"] < strat_min_n or a["pnl"] > strat_pnl:
            continue
        killed_strategies.add(strat)
        decisions.append({
            "strategy": strat, "rule": "live_strategy_bleed",
            "pairs": [p for p, s in strategy_by_tf.items()
                      if s == strat and p not in disabled],
            "pnl": round(a["pnl"], 2), "n_trades": a["n"], "days": days,
            "quarantine_days": quarantine,
        })
    return decisions


def blocked_for_entry(config: dict, db_path: Path | None = None) -> dict:
    """Face do kill-switch para o DAEMON usar intradia (Wave 894).

    Mesmas regras do evaluate() (fonte única), SEM escrever config: retorna
    os pares e estratégias que o daemon deve recusar até o próximo AGI run
    formalizar o disable. Fail-open: erro/VAZIO → nada bloqueado.

    Returns:
        {"pairs": set[str], "strategies": set[str]}
    """
    out: dict = {"pairs": set(), "strategies": set()}
    try:
        for d in evaluate(config, db_path) or []:
            if d.get("pair"):
                out["pairs"].add(d["pair"])
            elif d.get("strategy"):
                out["strategies"].add(d["strategy"])
    except Exception:
        pass
    return out


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
