"""
backfill_intel.py — o AGI calibra FILTROS DE SESSÃO (time_blocks) por replay
histórico contrafactual (Wave AGI-backfill, Bruno 2026-08-16).

Fecha o ciclo autônomo da "história melhor": em vez de esperar o walker live
acumular amostra (1 pregão/dia), o AGI replay-a a janela rolante com a
semântica exata do daemon (forward_walker --backfill) e decide sozinho:

  1. BASELINE — replay do config ATUAL → forward_backfill_trades (run_id
     próprio; tabela isolada que o stage6 shadow NÃO lê).
  2. ANÁLISE — corte por (root, hora) na MESMA escala do gate do daemon
     (hora do ts da barra — aggregate_blackout usa dt.hour do mesmo ts).
  3. HIPÓTESE — janelas contíguas de horas com perda líquida e n mínimo.
     Só nasce candidato de hora NEGATIVA: horas positivas não são tocadas
     (anti-overfit — não otimizamos o que já funciona).
  4. CONTRAFACTUAL — re-replay da mesma janela com o time_blocks da hipótese
     aplicado IN-MEMORY (config live em disco intocado).
  5. DECISÃO — aplica só se: ΔPnL(replay) >= MIN_GAIN_R, dias de evidência
     >= MIN_DAYS_EVIDENCE, n da janela >= MIN_TRADES_HOUR, e não sobrepõe
     bloco manual existente (manual sempre vence).
  Aplicação via save_full_config (writer autorizado, lineage
  "agi_v4_backfill_intel"), espelhando o risk_calibrator. Churn controlado:
  blocks próprios são marcados com OWN_TAG e só substituídos por overlap.

Guardas: só roda pós-close (>= POST_CLOSE_HOUR ou fim de semana — o walker
recusa dia útil 08-17h e o AGI do meio-dia não deve disputar o Wine);
AUTO-APPLY OFF POR PADRÃO (Bruno 16/08, opção "análise-only": walk-forward
out-of-sample ficou INCONCLUSIVO — prometia +R$87 na descoberta e entregou
+R$7 na validação cega de agosto; autonomia não se paga nesta amostra).
Reativar o auto-apply: export VT_BACKFILL_INTEL=1 (ex.: no wrapper do cron).
Janela de análise: VT_BACKFILL_INTEL_DAYS. O modo manual (__main__) força
análise em dry-run — é a "ferramenta de análise" da opção 2, nunca aplica.
Fail-safe: nunca derruba o pipeline.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

log = logging.getLogger("agi_v4.backfill_intel")

LOOKBACK_DAYS = 30        # janela de replay (env: VT_BACKFILL_INTEL_DAYS)
MIN_TRADES_HOUR = 12      # n mínimo na janela de horas p/ ser candidato
MIN_DAYS_EVIDENCE = 10    # dias com trades no baseline p/ aplicar qualquer coisa
MIN_GAIN_R = 20.0         # ΔPnL mínimo (R$, escala walker) p/ aplicar um bloco
MAX_WINDOWS = 2           # nº máx de janelas candidatas testadas por run
POST_CLOSE_HOUR = 17      # só roda >= 17h em dia útil (walker guarda 08-17h)
OWN_TAG = "agi_backfill"  # marca de posse dos blocks que criamos
BARS_FETCH = 6000         # barras por par no replay (igual ao default do walker)


def _db_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "vt_trades.db"


def _enabled() -> bool:
    """Auto-apply é OPT-IN (default OFF — probação Bruno 16/08).

    Racional: o walk-forward out-of-sample (descoberta maio-jul vs validação
    cega de agosto) deu INCONCLUSIVO — o Δ prometido não se materializa fora
    da amostra. Até haver evidência melhor, o módulo só roda como ferramenta
    de análise manual (``python3 optimization/agi_v4/backfill_intel.py``,
    sempre dry-run) ou com opt-in explícito via VT_BACKFILL_INTEL=1.
    """
    return os.environ.get("VT_BACKFILL_INTEL", "0").lower() in ("1", "true", "yes")


def _post_close(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    return now.weekday() >= 5 or now.hour >= POST_CLOSE_HOUR


def _run_replay(run_id: str, date_from: str, date_to: str,
                symbols: list, tfs: list, override: dict | None) -> dict:
    """Roda um backfill do walker programaticamente e devolve métricas do run.

    O import é tardio (pesado: carrega vt_autotrader/MT5). SystemExit do
    guard de horário do walker é capturado — nunca derruba o pipeline.
    """
    try:
        from optimization import forward_walker as fw
        ns = SimpleNamespace(
            backfill=True, from_date=date_from, to_date=date_to,
            run_id=run_id, backfill_bars=BARS_FETCH, bars_count=100,
            symbols=list(symbols), tfs=list(tfs), min_trades=5,
            ignore_time_blocks=False, force_backfill_hours=False,
            config_override=override,
        )
        state = fw.WalkerState()
        fw.run_backfill(ns, state)
    except SystemExit as e:
        return {"error": f"walker guardou o horário (exit {e.code})"}
    except Exception as e:
        log.warning(f"backfill_intel: replay {run_id} falhou: {e}")
        return {"error": str(e)[:200]}
    return _metrics_for_run(run_id)


def _metrics_for_run(run_id: str) -> dict:
    """Lê o run da tabela isolada e agrega: geral + por (root, hora)."""
    db = _db_path()
    if not db.exists():
        return {"error": "DB não encontrado"}
    con = sqlite3.connect(str(db), timeout=30.0)
    try:
        rows = con.execute(
            """SELECT substr(symbol,1,3), CAST(strftime('%H', entry_time) AS INT),
                      net_pnl_brl, date(entry_time)
               FROM forward_backfill_trades
               WHERE run_id = ? AND exit_time IS NOT NULL""",
            (run_id,),
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return {"n": 0, "pnl": 0.0, "wr": 0.0, "days": 0, "by_root_hour": {}}
    pnls = [r[2] or 0.0 for r in rows]
    by_root_hour: dict[tuple, dict] = {}
    for root, hour, pnl, day in rows:
        d = by_root_hour.setdefault((root, hour), {"n": 0, "pnl": 0.0})
        d["n"] += 1
        d["pnl"] += pnl or 0.0
    return {
        "n": len(rows),
        "pnl": sum(pnls),
        "wr": sum(1 for p in pnls if p > 0) / len(pnls),
        "days": len({r[3] for r in rows}),
        "by_root_hour": {f"{k[0]}_{k[1]:02d}h": v for k, v in by_root_hour.items()},
    }


def _candidate_windows(baseline: dict, config: dict) -> list[dict]:
    """Janelas contíguas de horas negativas por root, ordenadas por perda.

    Anti-overfit: só horas com PnL < 0 e n >= MIN_TRADES_HOUR. Janelas que
    sobrepõem bloco existente no config (manual ou próprio) são descartadas —
    manual sempre vence, e bloco próprio já está honrado no baseline.
    """
    existing: dict[str, list] = config.get("time_blocks", {}) or {}
    cands: list[dict] = []
    roots = sorted({k.rsplit("_", 1)[0] for k in baseline.get("by_root_hour", {})})
    for root in roots:
        hours = {}
        for k, v in baseline["by_root_hour"].items():
            r, h = k.rsplit("_", 1)
            if r == root:
                hours[int(h[:-1])] = v
        bad = sorted(h for h, v in hours.items()
                     if v["pnl"] < 0 and v["n"] >= MIN_TRADES_HOUR)
        if not bad:
            continue
        # agrupa horas contíguas em janelas [start, end)
        windows = []
        for h in bad:
            if windows and h == windows[-1][1]:
                windows[-1][1] = h + 1
            else:
                windows.append([h, h + 1])
        for s, e in windows:
            n = sum(hours[h]["n"] for h in range(s, e))
            pnl = sum(hours[h]["pnl"] for h in range(s, e))
            # descarta se sobrepõe qualquer bloco já configurado p/ o root
            overlap = any(
                s < (b.get("end", 24)) and (b.get("start", 0)) < e
                for b in (existing.get(root) or [])
                if isinstance(b, dict)
            )
            if overlap:
                continue
            cands.append({"root": root, "start": s, "end": e, "n": n, "pnl": pnl})
    cands.sort(key=lambda c: c["pnl"])
    return cands[:MAX_WINDOWS]


def _merge_time_blocks(cfg: dict, applied: list[dict], days: int) -> int:
    """Insere os blocks aprovados preservando entradas manuais.

    Entries marcadas com OWN_TAG que sobrepõem a nova janela são substituídas;
    manuais nunca são tocadas. Retorna nº de roots alterados.
    """
    tb = dict(cfg.get("time_blocks", {}) or {})
    changed = 0
    for cand in applied:
        root = cand["root"]
        lst = [e for e in (tb.get(root) or []) if isinstance(e, dict)]
        kept = [
            e for e in lst
            if OWN_TAG not in (e.get("reason") or "")
            or not (cand["start"] < (e.get("end", 24)) and (e.get("start", 0)) < cand["end"])
        ]
        kept.append({
            "start": cand["start"], "end": cand["end"],
            "reason": (f"{OWN_TAG}: replay {days}d evitaria R$ {-cand['pnl']:.0f} "
                       f"em {cand['n']}t ({cand['delta']:+.0f} validado)"),
        })
        tb[root] = kept
        changed += 1
    if changed:
        cfg["time_blocks"] = tb
    return changed


def run(ctx: dict) -> dict:
    """Stage de inteligência de sessão. Fail-safe: nunca derruba o pipeline."""
    config = ctx.get("config", {}) or {}
    dry_run = ctx.get("dry_run", True)

    if not _enabled():
        ctx["backfill_intel"] = {"status": "desligado (análise-only; reativar com VT_BACKFILL_INTEL=1)"}
        return {"summary": "desligado por env"}
    if not _post_close():
        # O walker recusa dia útil 08-17h; o AGI do meio-dia pula esta fase
        # (o cron das 17h10 é o slot desta análise — Wine livre pós-close).
        ctx["backfill_intel"] = {"status": "pulado (dentro do pregão; roda no cron 17h)"}
        return {"summary": "pulado (pré-close)"}

    days = int(os.environ.get("VT_BACKFILL_INTEL_DAYS", str(LOOKBACK_DAYS)))
    date_to = datetime.now().strftime("%Y-%m-%d")
    date_from = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    stamp = datetime.now().strftime("%Y%m%d")
    symbols = config.get("symbols", []) or []
    tfs_by = config.get("timeframes_by_symbol", {}) or {}
    tfs = sorted({tf for s in symbols
                  for tf in tfs_by.get(s, config.get("timeframes", []) or [])})
    if not symbols or not tfs:
        ctx["backfill_intel"] = {"status": "sem pares no config"}
        return {"summary": "sem pares"}

    # ── 1. Baseline: config atual, honrando time_blocks vigentes ──
    base_id = f"agi_base_{stamp}"
    log.info(f"backfill_intel: replay baseline {date_from}→{date_to} "
             f"({base_id}; symbols={symbols}; tfs={tfs})")
    baseline = _run_replay(base_id, date_from, date_to, symbols, tfs, None)
    if baseline.get("error") or not baseline.get("n"):
        ctx["backfill_intel"] = {"status": "sem dados", "baseline": baseline,
                                 "window_days": days}
        return {"summary": f"baseline sem dados ({baseline.get('error', '0 trades')})"}

    # ── 2/3. Análise por (root, hora) + hipóteses de bloqueio ──
    cands = _candidate_windows(baseline, config)
    result = {
        "status": "ok",
        "window_days": days,
        "baseline": {k: baseline[k] for k in ("n", "pnl", "wr", "days")},
        "by_root_hour": baseline["by_root_hour"],
        "candidates": [],
        "applied": [],
    }
    _cand_strs = [f"{c['root']} {c['start']}-{c['end']}h {round(c['pnl'])}" for c in cands]
    log.info(f"backfill_intel: baseline n={baseline['n']} "
             f"R${baseline['pnl']:+.0f} WR {baseline['wr']*100:.0f}% "
             f"({baseline['days']}d) | candidatos: {_cand_strs}")

    # ── 4. Contrafactual: re-replay com o bloco da hipótese (in-memory) ──
    evidence_ok = baseline["days"] >= MIN_DAYS_EVIDENCE
    for cand in cands:
        scn_id = f"agi_scn_{stamp}_{cand['root']}{cand['start']:02d}{cand['end']:02d}"
        override = {"time_blocks": {
            cand["root"]: [{"start": cand["start"], "end": cand["end"],
                            "reason": f"{OWN_TAG}: counterfactual"}]
        }}
        scn = _run_replay(scn_id, date_from, date_to, symbols, tfs, override)
        if scn.get("error") or not scn.get("n"):
            cand["delta"] = None
            cand["apply"] = False
            cand["note"] = scn.get("error", "cenário vazio")
            result["candidates"].append(cand)
            continue
        cand["delta"] = scn["pnl"] - baseline["pnl"]
        cand["apply"] = bool(
            evidence_ok
            and cand["delta"] >= MIN_GAIN_R
            and (baseline["n"] - scn["n"]) > 0  # bloqueio precisa ter cortado trades
        )
        log.info(f"backfill_intel: {cand['root']} {cand['start']}-{cand['end']}h: "
                 f"{cand['n']}t R${cand['pnl']:+.0f} | replay c/ bloco: "
                 f"R${scn['pnl']:+.0f} (Δ {cand['delta']:+.0f}, "
                 f"{baseline['n'] - scn['n']}t cortados) "
                 f"{'APLICA' if cand['apply'] else 'mantém'}")
        result["candidates"].append(cand)

    # ── 5. Aplicação (só produção, só com evidência) ──
    applied = [c for c in result["candidates"] if c.get("apply")]
    if applied and not dry_run:
        try:
            from core.vt_config_loader import load_config, save_full_config
            cfg = load_config(force=True)
            n_roots = _merge_time_blocks(cfg, applied, baseline["days"])
            if n_roots:
                save_full_config(cfg, updated_by="agi_v4_backfill_intel")
                try:
                    config.clear()
                    config.update(load_config(force=True))
                except Exception:
                    pass
                result["applied"] = [
                    f"{c['root']} {c['start']}-{c['end']}h (Δ R${c['delta']:+.0f})"
                    for c in applied
                ]
                log.info(f"backfill_intel: APLICADO — {', '.join(result['applied'])}")
        except Exception as e:
            log.error(f"backfill_intel: aplicação falhou: {e}")
            result["apply_error"] = str(e)[:200]
    elif applied:
        log.info(f"backfill_intel: {len(applied)} block(s) aprovado(s) mas "
                 f"dry-run — nada escrito")

    ctx["backfill_intel"] = result
    summary = (f"replay {days}d: n={baseline['n']} R${baseline['pnl']:+.0f} "
               f"({baseline['days']}d); {len(cands)} candidato(s), "
               f"{len(applied)} aprovado(s)"
               + (f", aplicou: {'; '.join(result['applied'])}" if result.get("applied")
                  else ""))
    return {"summary": summary}


if __name__ == "__main__":
    # Execução manual (fim de semana/madrugada): análise + contrafactual,
    # NUNCA aplica (dry-run) — é a ferramenta de análise da opção 2. Força o
    # env local (o default OFF é probação do AUTO-APPLY do cron, não da
    # ferramenta manual).
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    os.environ["VT_BACKFILL_INTEL"] = "1"
    from core.vt_config_loader import load_config
    _ctx = {"config": load_config(force=True), "dry_run": True}
    out = run(_ctx)
    print(json.dumps(out, ensure_ascii=False, default=str))
