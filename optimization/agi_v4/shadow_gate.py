# -*- coding: utf-8 -*-
"""
shadow_gate — gate de promoção shadow-first + blindagem de incumbência live
(Wave 894B, Bruno 15/09/2026)

Motivação (dados live/sombra, 90d até 14/09):
- A sim 30d do AGI prometia ~+R$30k/30d; o LIVE total é -R$764 (628 trades) e
  a família AGI4_* soma -R$915. A sim não transfere para o real.
- HTF_BIAS_LTF_ENTRY (+R$665/83t live no WIN_M15) foi removida pelo AGI em
  11/08 e substituída por AGI4_WIN_121815 (-R$503 desde então) — troca de um
  vencedor comprovado por um candidato que só tinha sim.
- DIVERGENCE_RSI na sombra: -R$5.739 em 80 trades no BIT_M15 — estratégia sem
  edge que o gate por par demorou a ver.

Este gate mora entre o "candidato aprovado na sim" e o write do stage5:

1. **shadow_negative** — candidato com sombra NEGATIVA (n ≥ min, PnL ≤ 0 no
   `forward_sim_trades` do MESMO par) é rejeitado. A sombra é simulação em
   mercado real (house rule), a melhor régua pré-live disponível.
2. **evidência positiva** — sombra com n ≥ min, PnL > 0 E PF ≥ min → passa.
3. **incumbent_protected** — sem evidência de sombra suficiente, o incumbente
   live-positivo (n ≥ min_live no `trades`, 30d) NÃO pode ser trocado por um
   candidato que só tem sim (o caso HTF_BIAS→AGI4_WIN).
4. Sem evidência E incumbente não-protegido → passa (soberania do AGI
   preservada em par sem dono comprovado; kill-switch/holdout vigiam).

Só vale para TROCA de estratégia (candidato != incumbente). Mudança só de
params no incumbente passa direto (a sombra valida estratégia, não params).

Módulo PURO (sqlite + env). Fail-open: qualquer erro interno libera a troca
(o gate é defesa contra incidentes conhecidos, não caminho crítico — o mesmo
contrato do live_kill_switch).

Env:
- VT_AGI_SHADOW_GATE=0 desativa o gate inteiro;
- VT_AGI_SHADOW_GATE_DAYS (30), VT_AGI_SHADOW_GATE_MIN_TRADES (20),
  VT_AGI_SHADOW_GATE_MIN_PF (1.1);
- VT_AGI_LIVE_PROTECT_MIN_TRADES (10), VT_AGI_LIVE_PROTECT_DAYS (30).
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
    return os.environ.get("VT_AGI_SHADOW_GATE", "1") == "1"


def _resolve_db(config: dict) -> Path | None:
    try:
        from optimization.agi_v4.stage1_collect import _resolve_db_path
        p = _resolve_db_path(config)
        if p:
            return Path(p)
    except Exception:
        pass
    p = Path("/home/bruno/Projects/Vibe-Trading/vt_trades.db")
    return p if p.exists() else None


def _shadow_agg(db_path: Path | None, pair: str, strategy: str, days: int) -> dict | None:
    """Agregado de sombra (forward_sim_trades) do par+estratégia na janela.

    Returns: {"n", "pnl", "pf"} ou None se sem dados/tabela.
    """
    if not db_path or not Path(db_path).exists():
        return None
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                """SELECT timeframe, symbol, net_pnl_brl
                   FROM forward_sim_trades
                   WHERE strategy = ? AND exit_time IS NOT NULL
                     AND entry_time >= ?""",
                (strategy, cutoff),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return None
    n, pnl, gross_win, gross_loss = 0, 0.0, 0.0, 0.0
    for tf, sym, p in rows:
        if not sym or not tf:
            continue
        if f"{sym[:3]}_{tf}" != pair:
            continue
        n += 1
        v = float(p or 0)
        pnl += v
        if v > 0:
            gross_win += v
        elif v < 0:
            gross_loss += v
    if n <= 0:
        return None
    pf = (gross_win / abs(gross_loss)) if gross_loss < 0 else float("inf")
    return {"n": n, "pnl": round(pnl, 2), "pf": pf}


def _live_incumbent_agg(db_path: Path | None, pair: str, incumbent: str,
                        days: int) -> dict | None:
    """PnL LIVE do incumbente no par (tabela trades, sem GHOST) — o lado
    positivo do `_incumbent_live_bleeding` do stage5."""
    if not db_path or not Path(db_path).exists():
        return None
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    root = pair.split("_", 1)[0] if "_" in pair else pair
    tf = pair.split("_", 1)[1] if "_" in pair else ""
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                """SELECT COUNT(*), COALESCE(SUM(net_pnl), 0) FROM trades
                   WHERE symbol LIKE ? AND timeframe = ? AND strategy = ?
                     AND entry_time >= ? AND exit_time IS NOT NULL
                     AND exit_reason != 'GHOST'""",
                (f"{root}%", tf, incumbent, cutoff),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    n, pnl = int(row[0] or 0), float(row[1] or 0)
    return {"n": n, "pnl": pnl} if n > 0 else None


def gate_strategy_swap(config: dict, db_path: Path | None, pair: str,
                       candidate_strategy: str) -> tuple[bool, str, str]:
    """Decide se a TROCA de estratégia no par pode ser aplicada.

    Args:
        config: config do AGI (lê strategy_by_tf para achar o incumbente).
        db_path: path do vt_trades.db (None → resolve do config/repo).
        pair: "ROOT_TF" (ex: "WIN_M15").
        candidate_strategy: estratégia do candidato.

    Returns:
        (ok, gate, reason) — gate/reason vazios quando liberado. Gates:
        "shadow_negative" e "incumbent_protected".
    """
    if not enabled():
        return True, "", "shadow gate desativado por env"
    try:
        incumbent = ((config or {}).get("strategy_by_tf", {}) or {}).get(pair) or ""
        if not incumbent or incumbent == candidate_strategy:
            return True, "", "sem troca de estratégia (params-only ou sem incumbente)"

        days = max(_env_int("VT_AGI_SHADOW_GATE_DAYS", 30), 1)
        min_n = max(_env_int("VT_AGI_SHADOW_GATE_MIN_TRADES", 20), 1)
        min_pf = _env_float("VT_AGI_SHADOW_GATE_MIN_PF", 1.1)

        path = db_path if db_path is not None else _resolve_db(config)
        ev = _shadow_agg(path, pair, candidate_strategy, days)

        # 1) Sombra NEGATIVA com amostra → rejeita em qualquer circunstância
        if ev and ev["n"] >= min_n and ev["pnl"] <= 0:
            return False, "shadow_negative", (
                f"sombra {pair}/{candidate_strategy}: R$ {ev['pnl']:.2f} em "
                f"{ev['n']} trades/{days}d — simulação em mercado real "
                f"contradiz o candidato (ex: DIVERGENCE_RSI -R$5.739/BIT_M15)")

        # 2) Evidência positiva suficiente → passa
        if ev and ev["n"] >= min_n and ev["pnl"] > 0 and ev["pf"] >= min_pf:
            return True, "", (
                f"sombra positiva {pair}/{candidate_strategy}: "
                f"R$ {ev['pnl']:.2f} em {ev['n']}t (PF {ev['pf']:.2f})")

        # 3) Sem evidência: incumbente live-positivo é blindado
        prot_days = max(_env_int("VT_AGI_LIVE_PROTECT_DAYS", 30), 1)
        prot_min = max(_env_int("VT_AGI_LIVE_PROTECT_MIN_TRADES", 10), 1)
        live = _live_incumbent_agg(path, pair, incumbent, prot_days)
        if live and live["n"] >= prot_min and live["pnl"] > 0:
            return False, "incumbent_protected", (
                f"incumbente {incumbent} live +R$ {live['pnl']:.2f}/{live['n']}t "
                f"em {prot_days}d e candidato {candidate_strategy} sem sombra "
                f"positiva (n≥{min_n}) — não se tira vencedor comprovado por "
                f"candidato que só tem sim")

        # 4) Soberania: par sem dono comprovado, candidato sem contradição
        _ev_txt = (f"sombra insuficiente ({ev['n']}t)" if ev else "sem sombra")
        return True, "", f"{_ev_txt}; incumbente não protegido — segue"
    except Exception as e:
        # Fail-open: o gate nunca segura uma troca por erro interno
        return True, "", f"fail-open ({type(e).__name__}: {e})"
