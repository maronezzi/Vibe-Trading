"""
non_regression.py — gates de não-regressão do AGI v4 (Wave 880.I, Bruno
2026-08-19: "o AGI não pode piorar; ele precisa ter todas as super
informações e garantir um sistema ideal").

Porta para DENTRO do pipeline AGI v4 a régua validada no apply noturno
W880 (18/08, scripts/w880_nightly_super_agi_apply_20260818.py), que em seu
primeiro dia validou forward (+R$395 shadow PF 2.42) e foi verde live
(+R$90.60). Incidentes que motivaram (todos 18–19/08):

  1. WIN_M30 trocado ao meio-dia pelo AGI logo após fazer +R$57 LIVE de
     manhã — o AGI não olhava histórico live multiday do par;
  2. BIT_H1 ligado/desligado/ligado em 3 sessões seguidas — churn de
     soberania sem evidência nova;
  3. Trocas aplicadas com melhoria marginal de simulação — sem fator
     mínimo, ruído vira mudança de estratégia.

Gates (todos fail-closed por candidato: se este módulo falhar, o candidato
é REJEITADO, nunca aplicado sem exame):

  A. walk_forward      — consistência >= 0.75 E >= 3 janelas positivas
                         (régua do super_agi_v5; a do evaluator é 0.65);
  B. fator             — baseline positivo exige cand_score >= 1.3x o
                         baseline (regra Wave 877: <30% não troca);
  C. live_winner       — par lucrando >= R$100 live (10 pregões) exige
                         fator 2.0x E walk-forward 100%;
  D. churn             — par trocado em sessão anterior há < 2 dias exige
                         evidência >= 2x a alegada na troca anterior;
  E. flip (soberania)  — enable/disable em U-turn há < 5 dias é bloqueado.

Journal: ``state/pair_change_journal.json`` (ao lado deste módulo) registra
toda troca/enable/disable com ts, from→to e PnL alegado. Na primeira
execução é AUTO-SEMEADO dos snapshots ``vt_config.json.snapshot_pre_cron_*``
da raiz do projeto — o BIT_H1 já nasce com histórico para o gate E agir.

Env vars (defaults conservadores):
  VT_AGI_FACTOR=1.3                VT_AGI_LIVE_WINNER_FACTOR=2.0
  VT_AGI_WF_MIN_CONSISTENCY=0.75   VT_AGI_LIVE_WINNER_MIN=100
  VT_AGI_CHURN_DAYS=2.0            VT_AGI_FLIP_DAYS=5.0
  VT_AGI_LIVE_LOOKBACK_DAYS=10
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("agi_v4.non_regression")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
JOURNAL_PATH = Path(__file__).resolve().parent / "state" / "pair_change_journal.json"

# Janelas do walk-forward com menos trades que isto não são julgadas
# (mesma régua do super_agi_v5: mínimo 3 trades/janela p/ contar).
_WF_MIN_TRADES_WINDOW = 3


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_i(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


# ═══════════════════════════════════════════════════════════════════
# Journal de mudanças por par
# ═══════════════════════════════════════════════════════════════════

def load_journal() -> list[dict]:
    """Carrega o journal; semeia dos snapshots se ainda não existir."""
    if not JOURNAL_PATH.exists():
        seeded = _seed_from_snapshots()
        if seeded:
            _save_journal(seeded)
            log.info(f"non_regression: journal semeado dos snapshots "
                     f"({len(seeded)} eventos)")
            return seeded
        return []
    try:
        data = json.loads(JOURNAL_PATH.read_text())
        return data if isinstance(data, list) else []
    except Exception as e:
        log.warning(f"non_regression: journal ilegível ({e}) — recomeça vazio")
        return []


def append_journal(entry: dict) -> None:
    """Adiciona um evento ao journal (escrita atômica tmp+rename)."""
    entries = load_journal()
    entries.append({"ts": datetime.now().isoformat(timespec="seconds"),
                    **entry})
    _save_journal(entries)


def _save_journal(entries: list[dict]) -> None:
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = JOURNAL_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=1))
    tmp.replace(JOURNAL_PATH)


_SNAP_RE = re.compile(r"snapshot_pre_(?:cron|w880)_(\d{8})_(\d{6})$")


def _seed_from_snapshots() -> list[dict]:
    """Reconstrói histórico de mudanças diffs dos snapshots de config.

    Cada snapshot_pre_cron_<TS> é o estado ANTES do run que começou em TS;
    mudanças entre snapshots consecutivos são atribuídas a TS (granularidade
    de sessão — suficiente para os gates de churn/flip, que são diários).
    """
    snaps = []
    for p in _PROJECT_ROOT.glob("vt_config.json.snapshot_pre_*"):
        m = _SNAP_RE.search(p.name)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            continue
        snaps.append((ts, p))
    snaps.sort()
    if len(snaps) < 1:
        return []
    states = []
    for ts, p in snaps:
        try:
            states.append((ts, json.loads(p.read_text())))
        except Exception:
            continue
    try:
        states.append((datetime.now(), json.loads(
            (_PROJECT_ROOT / "vt_config.json").read_text())))
    except Exception:
        pass

    events = []
    for (ts_a, a), (ts_b, b) in zip(states, states[1:]):
        # diferença de estrategia por par → swap
        sa = a.get("strategy_by_tf", {}) or {}
        sb = b.get("strategy_by_tf", {}) or {}
        for pair in sorted(set(sa) | set(sb)):
            if sa.get(pair) != sb.get(pair):
                events.append({
                    "ts": ts_b.isoformat(timespec="seconds"),
                    "kind": "swap", "pair": pair,
                    "from": sa.get(pair), "to": sb.get(pair),
                    "pnl_claimed": None, "session": f"seed_{ts_b:%Y%m%d%H}",
                    "seeded": True,
                })
        # enable/disable: membership em disabled_timeframes OU day_trade_intent
        da = set(a.get("disabled_timeframes", []) or [])
        db_ = set(b.get("disabled_timeframes", []) or [])
        ia = a.get("day_trade_intent", {}) or {}
        ib = b.get("day_trade_intent", {}) or {}
        for pair in sorted(da | db_ | set(ia) | set(ib)):
            was_on = pair not in da and bool(ia.get(pair, False))
            is_on = pair not in db_ and bool(ib.get(pair, False))
            if was_on and not is_on:
                events.append({"ts": ts_b.isoformat(timespec="seconds"),
                               "kind": "disable", "pair": pair,
                               "session": f"seed_{ts_b:%Y%m%d%H}",
                               "seeded": True})
            elif not was_on and is_on:
                events.append({"ts": ts_b.isoformat(timespec="seconds"),
                               "kind": "enable", "pair": pair,
                               "session": f"seed_{ts_b:%Y%m%d%H}",
                               "seeded": True})
    return events


# ═══════════════════════════════════════════════════════════════════
# Informações "super": live multiday por par (DB) + walk-forward do cand
# ═══════════════════════════════════════════════════════════════════

def live_pair_pnl(config: dict | None = None,
                  days: int | None = None) -> dict[str, dict]:
    """P&L live por par (SYM_TF) nos últimos N dias — tabela `trades`.

    Retorna {pair: {"pnl", "n", "wins"}}. Falha de DB → dict vazio (os
    gates tratam vazio como "sem evidência live", não como winner).
    """
    days = days or _env_i("VT_AGI_LIVE_LOOKBACK_DAYS", 10)
    db = None
    try:
        try:
            from optimization.agi_v4 import stage1_collect
        except ImportError:
            from . import stage1_collect  # noqa: F401 — alias de módulo
        db = stage1_collect._resolve_db_path(config or {})
    except Exception:
        pass
    if not db or not Path(db).exists():
        db = _PROJECT_ROOT / "vt_trades.db"
        if not db.exists():
            return {}
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    out: dict[str, dict] = {}
    try:
        conn = sqlite3.connect(str(db))
        rows = conn.execute(
            """SELECT symbol, timeframe, net_pnl FROM trades
               WHERE entry_time >= ? AND exit_time IS NOT NULL
                 AND exit_reason != 'GHOST'""",
            (cutoff,),
        ).fetchall()
        conn.close()
    except Exception as e:
        log.warning(f"non_regression: live_pair_pnl falhou ({e})")
        return {}
    for sym, tf, pnl in rows:
        pair = f"{str(sym)[:3]}_{tf}"
        d = out.setdefault(pair, {"pnl": 0.0, "n": 0, "wins": 0})
        d["pnl"] += float(pnl or 0)
        d["n"] += 1
        if float(pnl or 0) > 0:
            d["wins"] += 1
    return out


def wf_from_candidate(cand: dict) -> dict:
    """Consistência walk-forward do candidato (lista de janelas do stage3).

    Janelas com < 3 trades não são julgadas (régua super_agi_v5).
    Sem dados → {"ok": False} — gate A rejeita.
    """
    windows = cand.get("walk_forward") or cand.get("full", {}).get("walk_forward") or []
    judged = [w for w in windows
              if isinstance(w, dict)
              and int(w.get("n_trades", 0) or 0) >= _WF_MIN_TRADES_WINDOW]
    if not judged:
        return {"ok": False, "consistency": 0.0, "n_positive": 0,
                "n_judged": 0}
    n_pos = sum(1 for w in judged if float(w.get("total_pnl", 0)) > 0)
    return {"ok": True, "consistency": n_pos / len(judged),
            "n_positive": n_pos, "n_judged": len(judged)}


# ═══════════════════════════════════════════════════════════════════
# Gates
# ═══════════════════════════════════════════════════════════════════

def gate_swap(pair: str, cand: dict, baseline_pnl: float,
              cand_score: float, base_score: float, session: str,
              live_pnl_by_pair: dict[str, dict] | None = None,
              ) -> tuple[bool, str, str]:
    """Gates A–D para trocar estratégia/params de um par ATIVO.

    Returns:
        (ok, gate, reason) — reason vazia quando ok.
    """
    # A. walk-forward (régua super_agi: >=75% das janelas, >=3 positivas)
    wf = wf_from_candidate(cand)
    wf_min = _env_f("VT_AGI_WF_MIN_CONSISTENCY", 0.75)
    if not wf["ok"] or wf["n_judged"] < 3 or wf["consistency"] < wf_min \
            or wf["n_positive"] < 3:
        return False, "wf_below_bar", (
            f"walk-forward {wf['consistency']:.0%} "
            f"({wf['n_positive']}/{wf['n_judged']} janelas) < exigência "
            f"{wf_min:.0%} com >=3 positivas")

    # B. fator mínimo sobre o baseline positivo (regra Wave 877: <30% não troca)
    factor = _env_f("VT_AGI_FACTOR", 1.3)
    live = (live_pnl_by_pair if live_pnl_by_pair is not None
            else live_pair_pnl())
    lwin_min = _env_f("VT_AGI_LIVE_WINNER_MIN", 100.0)
    is_live_winner = float(live.get(pair, {}).get("pnl", 0)) >= lwin_min
    if is_live_winner:
        factor = _env_f("VT_AGI_LIVE_WINNER_FACTOR", 2.0)

    if baseline_pnl > 0 and cand_score < factor * max(base_score, 1.0):
        return False, "marginal_improvement", (
            f"score cand R${cand_score:.0f} < {factor}x baseline "
            f"R${base_score:.0f} ({'live-winner: ' if is_live_winner else ''}"
            f"live 10d R${live.get(pair, {}).get('pnl', 0):+.0f})")

    # C. live-winner exige walk-forward perfeito (além do fator 2x acima)
    if is_live_winner and wf["consistency"] < 1.0:
        return False, "live_winner_wf", (
            f"par live-winner (R${live[pair]['pnl']:+.0f}/10d) exige WF "
            f"100% — candidato tem {wf['consistency']:.0%}")

    # D. churn: troca em sessão anterior recente exige evidência escalada
    churn_days = _env_f("VT_AGI_CHURN_DAYS", 2.0)
    cutoff = datetime.now() - timedelta(days=churn_days)
    for ev in reversed(load_journal()):
        if ev.get("pair") != pair or ev.get("kind") != "swap":
            continue
        if ev.get("session") == session:
            continue  # mesma execução do AGI: iteração interna é permitida
        try:
            ev_ts = datetime.fromisoformat(ev.get("ts", ""))
        except ValueError:
            continue
        if ev_ts < cutoff:
            continue
        claimed = ev.get("pnl_claimed")
        base_ref = float(claimed) if claimed else max(base_score, baseline_pnl, 1.0)
        if cand_score < 2.0 * max(base_ref, 1.0):
            return False, "churn_cooldown", (
                f"par trocado há <{churn_days:.0f}d "
                f"({ev_ts:%d/%m %Hh}, alegado R${base_ref:.0f}) — exige "
                f">=2x essa evidência (cand R${cand_score:.0f})")
        break  # só a troca mais recente conta
    return True, "", ""


def allow_flip(pair: str, kind: str, session: str,
               flip_days: float | None = None) -> tuple[bool, str]:
    """Gate E: bloqueia U-turn de soberania (enable↔disable) recente.

    Incidente BIT_H1 (18–19/08): ligado 12h → desligado 17h → ligado 17h.
    Um flip em U-turn exige evidência nova — aqui, tempo (FLIP_DAYS).
    """
    flip_days = flip_days if flip_days is not None else _env_f("VT_AGI_FLIP_DAYS", 5.0)
    opposite = "disable" if kind == "enable" else "enable"
    cutoff = datetime.now() - timedelta(days=flip_days)
    for ev in reversed(load_journal()):
        if ev.get("pair") != pair or ev.get("kind") != opposite:
            continue
        if ev.get("session") == session:
            continue
        try:
            ev_ts = datetime.fromisoformat(ev.get("ts", ""))
        except ValueError:
            continue
        if ev_ts >= cutoff:
            return False, (
                f"U-turn de soberania: {opposite} há "
                f"{(datetime.now() - ev_ts).days}d (<{flip_days:.0f}d) em "
                f"{ev_ts:%d/%m %Hh} — mantém estado atual até virar a janela")
        break
    return True, ""
