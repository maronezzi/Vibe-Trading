# -*- coding: utf-8 -*-
"""
holdout_gate — revalidação OUT-OF-SAMPLE + penalidade de overlap netting
(Wave 894B, Bruno 15/09/2026)

Problema: a seleção do AGI (stage3) escolhe o melhor combo SIM no MESMO 30d
que o walk-forward divide — as janelas são in-sample. Estratégia escolhida
por grid de ~600 combos quase sempre "descobre" ruído do período. 90d de
evidência live: a sim +R$30k escolheu estratégias que somam -R$764 no real.

Este gate roda UMA vez por candidato prestes a ser APLICADO (não por combo
do stage3 — custo O(1) simulação):

1. **Holdout verdadeiro** — re-simula o candidato nos últimos
   VT_AGI_HOLDOUT_DAYS (default 10) pregões. A seleção usou o 30d que
   TERMINA no início do holdout, então esse trecho é genuinamente
   fora-da-amostra. PnL < 0 no holdout → candidato rejeitado.
2. **Overlap netting** — nos trades do holdout, mede a fração cuja entrada
   cai DENTRO do intervalo [entrada, saída] de um trade de sombra de OUTRO
   TF do MESMO root em direção oposta (forward_sim_trades). Sob netting
   (com o gate NETTING_COHERENCE da Wave 894 no daemon), essas entradas
   seriam BLOQUEADAS ao vivo — a sim que as conta superestima o candidato.
   Fração > VT_AGI_NETTING_OVERLAP_MAX (default 0.2) → rejeitado.

Fail-open: falha de fetch/MT5 NÃO rejeita (não trava o AGI por instabilidade
de infra); só evidência válida rejeita. VT_AGI_HOLDOUT_DAYS=0 desativa.

Módulo com decisão PURA (avaliável sem MT5) + wrapper fino que busca barras
via backtest_evaluator.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("agi_v4.holdout_gate")


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


def holdout_days() -> int:
    return max(_env_int("VT_AGI_HOLDOUT_DAYS", 10), 0)


def evaluate_holdout_decision(holdout_pnl: float, n_trades: int,
                              overlap_fraction: float) -> tuple[bool, str]:
    """Decisão PURA sobre o resultado do holdout (testável sem MT5).

    Returns:
        (ok, reason) — reason vazio quando aprovado.
    """
    days = holdout_days()
    if days <= 0:
        return True, "holdout desativado (VT_AGI_HOLDOUT_DAYS=0)"
    if n_trades <= 0:
        # Sem trades no holdout: nada contradiz o candidato (segue; os
        # demais gates — sim 30d, walk-forward, shadow — continuam valendo)
        return True, "holdout sem trades — neutro"
    if holdout_pnl < 0:
        return False, (f"holdout {days}d NEGATIVO: R$ {holdout_pnl:.2f} em "
                       f"{n_trades} trades fora-da-amostra — overfit do grid")
    max_overlap = _env_float("VT_AGI_NETTING_OVERLAP_MAX", 0.2)
    if 0 <= max_overlap < 1 and overlap_fraction > max_overlap:
        return False, (f"{overlap_fraction:.0%} dos trades do holdout "
                       f"conflitam com direção oposta de outro TF do mesmo "
                       f"root (limite {max_overlap:.0%}) — sob netting o "
                       f"daemon bloquearia essas entradas")
    return True, ""


def _parse_dt(v) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v)[:19].replace("T", " "))
    except (TypeError, ValueError):
        return None


def netting_overlap_fraction(db_path: Path | None, root: str, own_tf: str,
                             candidate_trades: list) -> tuple[float, int]:
    """Fração dos trades do candidato cuja ENTRADA cai dentro de um trade de
    sombra oposto de outro TF do mesmo root (mesmo dia, direção contrária).

    Proxy do que o gate NETTING_COHERENCE bloquearia ao vivo. Retorna
    (fração, n_checado); sem DB/sem direção nos trades → (0.0, n) — neutro.
    """
    n = len(candidate_trades or [])
    if n == 0 or not db_path or not Path(db_path).exists():
        return 0.0, n
    window_start = datetime.now() - timedelta(
        days=max(holdout_days(), 1) + 2)
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                """SELECT timeframe, direction, entry_time, exit_time
                   FROM forward_sim_trades
                   WHERE substr(symbol,1,3) = ? AND exit_time IS NOT NULL
                     AND entry_time >= ?""",
                (root, window_start.strftime("%Y-%m-%d")),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return 0.0, n

    # Intervalos de sombra por direção, EXCETO o TF do candidato
    buys: list[tuple[datetime, datetime]] = []
    sells: list[tuple[datetime, datetime]] = []
    for tf, direction, et, xt in rows:
        if not tf or tf == own_tf:
            continue
        e, x = _parse_dt(et), _parse_dt(xt)
        if not e or not x or x < e:
            continue
        (buys if str(direction).upper() == "BUY" else sells).append((e, x))
    if not buys and not sells:
        return 0.0, n

    conflicts = 0
    for t in candidate_trades:
        e = _parse_dt(t.get("entry_dt"))
        if not e:
            continue
        d = str(t.get("direction", t.get("side", "")) or "").upper()
        against = sells if d == "BUY" else buys
        if any(se <= e <= sx for se, sx in against):
            conflicts += 1
    return (conflicts / n), n


def validate(config: dict, pair: str, strategy: str, params: dict,
             db_path: Path | None = None) -> tuple[bool, str]:
    """Wrapper do gate para o stage5: simula o holdout e decide.

    Fail-open: erro de fetch/simulação NÃO rejeita o candidato (o gate é
    defesa contra overfit, não dependência crítica de infra).
    """
    days = holdout_days()
    if days <= 0:
        return True, "holdout desativado"
    if "_" not in pair:
        return True, ""
    root, tf = pair.split("_", 1)
    try:
        from optimization.agi_v4.backtest_evaluator import evaluate_holdout
        res = evaluate_holdout(root, tf, strategy, params, config, days=days)
        trades = res.get("trades") or []
        pnl = float(res.get("total_pnl", 0) or 0)
        n = int(res.get("n_trades", 0) or 0)
        if res.get("error"):
            return True, f"holdout falhou ({res['error']}) — fail-open"
        if db_path is None:
            db_path = Path("/home/bruno/Projects/Vibe-Trading/vt_trades.db")
        frac, _ = netting_overlap_fraction(db_path, root, tf, trades)
        ok, reason = evaluate_holdout_decision(pnl, n, frac)
        if ok and reason:
            log.info(f"[HOLDOUT] {pair}/{strategy}: {reason}")
        elif not ok:
            log.info(f"[HOLDOUT] {pair}/{strategy} REJEITADO: {reason}")
        return ok, reason
    except Exception as e:
        return True, f"holdout exception ({type(e).__name__}: {e}) — fail-open"
