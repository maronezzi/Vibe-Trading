#!/usr/bin/env python3
"""
w880_nightly_super_agi_apply_20260818.py — Apply noturna AUTÔNOMA dos
candidatos do super_agi_v5 (Bruno 2026-08-18: "pode executar sozinho").

Contexto: a busca exaustiva super_agi_v5 (16 pares × ~300 estratégias × 60
combos, walk-forward 4 janelas) termina tarde (~00:30). Este script roda
sozinho de madrugada (cron one-shot), espera o report ficar completo,
compara cada candidato contra a estratégia ATUAL do config NAS MESMAS
BARRAS (Regra 1 honesta — mesmo padrão do verify_super_agi_v5.py) e aplica
SOMENTE trocas "ótimas" (gates abaixo). Resumo completo vai pro Telegram.

GATES DE APPLY (todos obrigatórios — muito mais duros que os do verify):
  1. Daemon do autotrader NÃO pode estar rodando (apply fora do pregão).
  2. Horário < 08:30 (não mexer no config perto do pre-flight 08:55).
  3. Candidato: PnL > 0, PF > 1.05, n_trades >= 15 (mesmas barras).
  4. Candidato PnL > PnL atual (mesmas barras) E >= 1.3x o atual
     ("ótimo", não melhoria marginal — regra Bruno Wave 877: <30% não troca).
  5. Walk-forward do report: consistência >= 0.75 E >= 3 de 4 janelas
     positivas (anti-overfit — o docstring do super_agi pede 75%, os gates
     internos só exigem 50%).
  6. Proteção live-winner: pares que estão LUCRANDO live (análise 16d de
     2026-08-18: WIN_M15 +R$174) exigem fator >= 2.0x e WF == 100%.
  7. SÓ troca estratégia/params de pares JÁ ATIVOS. NUNCA reativa par
     desabilitado (disabled_timeframes/day_trade_intent intocáveis).
  8. Máximo de 4 trocas por noite (blast radius limitado; ranked por delta).
  9. Idempotente: se o config já foi escrito por este script, aborta.

Se NENHUM candidato passar: não escreve nada e avisa no Telegram.
Backup: vt_config.json.snapshot_pre_w880_<ts> antes de qualquer escrita.

Usage:
    /usr/bin/python3 scripts/w880_nightly_super_agi_apply_20260818.py \
        [--report-dir data/super_agi_v5_20260818_run2] [--dry-run] \
        [--deadline-hhmm 05:00]

Exit codes: 0 = ok (aplicado ou skip limpo) · 1 = abort de segurança ·
2 = report incompleto após deadline · 3 = erro inesperado.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

RUN_TAG = "w880_nightly_20260818"
WRITER_NAME = "w880_nightly_super_agi_apply_20260818"
MAX_SWAPS = 4
APPLY_CUTOFF_HHMM = "08:30"
# Pares lucrativos live nos últimos 16 pregões (análise DB 2026-08-18) —
# só saem da posição com upgrade expressivo, não por margem de simulação.
LIVE_WINNERS = {"WIN_M15": 2.0}

BAR_COUNTS = {"M5": 2500, "M15": 900, "M30": 500, "H1": 260}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def notify(title: str, body: str) -> None:
    """Telegram via Hermes (best-effort: falha de envio não aborta o apply)."""
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from core.vt_hermes_helper import hermes_send

        # Mesmo grupo do relatório diário/copilot (monitoring/vt_copilot.py).
        target = "telegram:-1004284773048"
        ok = hermes_send(target, f"{title}\n\n{body}", timeout=30)
        log(f"Telegram: {'enviado' if ok else 'FALHOU (não bloqueia)'}")
    except Exception as e:  # pragma: no cover — envio é best-effort
        log(f"Telegram: exceção best-effort: {e}")


def daemon_running() -> bool:
    r = subprocess.run(["pgrep", "-f", "vt_autotrader"], capture_output=True)
    return r.returncode == 0


def super_agi_running() -> bool:
    r = subprocess.run(["pgrep", "-f", "super_agi_v5.py"], capture_output=True)
    return r.returncode == 0


def report_is_complete(report: dict) -> bool:
    per = report.get("per_pair", {})
    if len(per) < 12:
        return False
    return all(not v.get("error") for v in per.values())


def wait_for_report(report_path: Path, out_dir: Path, deadline_dt: datetime,
                    max_relaunches: int = 3) -> dict | None:
    """Espera o report ficar completo; se a busca morreu, retoma com --resume."""
    relaunches = 0
    while datetime.now() < deadline_dt:
        if report_path.exists():
            try:
                report = json.loads(report_path.read_text())
            except Exception:
                report = None
            if report and report_is_complete(report):
                log(f"report completo: {len(report['per_pair'])} pares OK")
                return report
            log("report ainda incompleto (ou com erro) — aguardando busca...")
        elif super_agi_running():
            log("busca rodando, report ainda não escrito — aguardando...")
        else:
            if relaunches >= max_relaunches:
                log("busca morta e limite de relaunches atingido — desistindo")
                return None
            relaunches += 1
            log(f"busca morta e sem report — relaunch {relaunches}/{max_relaunches} com --resume")
            cmd = (f"cd {PROJECT_ROOT} && setsid nohup /usr/bin/python3 "
                   f"optimization/super_agi_v5.py --out {out_dir} --top-k 3 "
                   f"--resume >> {out_dir}/console_resume.log 2>&1 &")
            subprocess.run(["bash", "-c", cmd])
            time.sleep(60)
            continue
        time.sleep(120)
    return None


def evaluate(df, sym_root: str, tf: str, strategy: str, params: dict) -> dict:
    """Métricas R$ nas barras dadas (mesma régua do verify_super_agi_v5)."""
    from backtest import backtest_v944 as bt

    trades = bt.backtest_combo(df, sym_root, tf, strategy, params)
    pnls = [float(t.get("pnl", 0)) for t in trades]
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gw, gl = sum(wins), abs(sum(losses))
    pf = gw / gl if gl > 0 else (float("inf") if gw > 0 else 0.0)
    return {
        "pnl": round(sum(pnls), 2),
        "n": n,
        "wr": round(len(wins) / n * 100, 1) if n else 0.0,
        "pf": round(min(pf, 99.99), 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", default="data/super_agi_v5_20260818_run2")
    parser.add_argument("--dry-run", action="store_true",
                        help="avalia e notifica, mas NÃO escreve no config")
    parser.add_argument("--deadline-hhmm", default="05:00",
                        help="hora-limite para esperar o report (default 05:00)")
    args = parser.parse_args()

    out_dir = (PROJECT_ROOT / args.report_dir).resolve()
    report_path = out_dir / "report.json"

    log(f"═══ {WRITER_NAME} — start (dry_run={args.dry_run}) ═══")

    # Gate 9: idempotência — se já aplicamos nesta janela, não aplica de novo.
    from core.vt_config_loader import load_config

    cfg = load_config(force=True)
    if cfg.get("_updated_by") == WRITER_NAME:
        msg = f"config já escrito por {WRITER_NAME} (v{cfg.get('_version')}) — nada a fazer"
        log(msg)
        notify("🌙 W880 apply noturna — skip", msg)
        return 0

    # Gate 1: daemon vivo → aborta (apply é fora do pregão, com autotrader parado).
    if daemon_running():
        msg = "ABORT: autotrader rodando — apply só com daemon parado"
        log(msg)
        notify("🛑 W880 apply noturna — ABORT", msg)
        return 1

    # Espera o report da busca (retoma se a busca morreu — ex: sessão fechada).
    h, m = map(int, args.deadline_hhmm.split(":"))
    deadline = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
    report = wait_for_report(report_path, out_dir, deadline)
    if report is None:
        msg = (f"report incompleto/ausente após {args.deadline_hhmm} — nada aplicado "
               f"(a busca continua; rode este script de novo com --resume se preciso)")
        log(msg)
        notify("🌙 W880 apply noturna — sem report", msg)
        return 2

    # Gate 2: janela de segurança — nunca perto do pre-flight/abertura.
    now_hhmm = datetime.now().strftime("%H:%M")
    if now_hhmm >= APPLY_CUTOFF_HHMM:
        msg = f"ABORT: {now_hhmm} >= {APPLY_CUTOFF_HHMM} — janela de apply encerrada"
        log(msg)
        notify("🛑 W880 apply noturna — ABORT", msg)
        return 1

    disabled = set(cfg.get("disabled_timeframes", []))
    strategy_by_tf = cfg.get("strategy_by_tf", {})
    params_by_tf = cfg.get("params_by_tf", {})
    active_pairs = [p for p in strategy_by_tf if p not in disabled]

    # Recarrega estratégias no motor canônico (mesma régua do AGI).
    from backtest import backtest_v944 as bt

    bt.load_strategies()

    approved, skipped = [], []
    for pair in sorted(active_pairs):
        info = report["per_pair"].get(pair) or {}
        top = (info.get("top_k") or [None])[0]
        if not top:
            skipped.append((pair, "sem candidato aprovado nos gates do super_agi"))
            continue
        sym_root, tf = pair.split("_", 1)
        cand = {
            "strategy": top["strategy"],
            "params": top.get("params", {}),
            "wf_consistency": top.get("wf_consistency", 0.0),
            "wf_positive": top.get("wf_positive", 0),
        }
        if cand["strategy"] == strategy_by_tf.get(pair) and not cand["params"]:
            skipped.append((pair, "candidato == atual"))
            continue

        # Regra 1 honesta: atual E candidato nas MESMAS barras (perpétua SYM$).
        try:
            path = bt.fetch(f"{sym_root}$", tf, BAR_COUNTS.get(tf, 2500))
            df = bt.load_csv(path)
        except Exception as e:
            skipped.append((pair, f"fetch falhou: {e}"))
            continue
        if df is None or len(df) < 100:
            skipped.append((pair, "barras insuficientes"))
            continue

        cur = evaluate(df, sym_root, tf, strategy_by_tf[pair],
                       params_by_tf.get(pair, {}))
        c = evaluate(df, sym_root, tf, cand["strategy"], cand["params"])

        factor_req = LIVE_WINNERS.get(pair, 1.3)
        wf_req = 1.0 if pair in LIVE_WINNERS else 0.75
        base = max(cur["pnl"], 1.0)
        reasons = []
        if c["pnl"] <= 0:
            reasons.append("cand PnL<=0")
        if c["pf"] <= 1.05:
            reasons.append(f"PF {c['pf']}<=1.05")
        if c["n"] < 15:
            reasons.append(f"n {c['n']}<15")
        if c["pnl"] <= cur["pnl"]:
            reasons.append(f"cand {c['pnl']:.0f} <= atual {cur['pnl']:.0f}")
        elif c["pnl"] < factor_req * base:
            reasons.append(
                f"melhoria {c['pnl'] / base:.2f}x < {factor_req}x exigido"
                + (" (proteção live-winner)" if pair in LIVE_WINNERS else ""))
        if cand["wf_consistency"] < wf_req or cand["wf_positive"] < 3:
            reasons.append(
                f"WF {cand['wf_consistency']:.2f}/{cand['wf_positive']}de4 "
                f"< {wf_req}/3")

        line = (f"{pair}: {strategy_by_tf[pair]} ({cur['pnl']:+.0f}, PF {cur['pf']}) → "
                f"{cand['strategy']} ({c['pnl']:+.0f}, PF {c['pf']}, n {c['n']}, "
                f"WF {cand['wf_consistency']:.0%})")
        if reasons:
            skipped.append((pair, "; ".join(reasons)))
            log(f"  SKIP {line} — {reasons[0]}")
        else:
            approved.append({
                "pair": pair, "line": line,
                "current": cur, "cand": c,
                "strategy": cand["strategy"], "params": cand["params"],
                "delta": round(c["pnl"] - cur["pnl"], 2),
            })
            log(f"  ✅ {line}")

    # Gate 8: máx. MAX_SWAPS trocas, ranked por delta (blast radius limitado).
    approved.sort(key=lambda a: a["delta"], reverse=True)
    if len(approved) > MAX_SWAPS:
        for a in approved[MAX_SWAPS:]:
            skipped.append((a["pair"], f"fora do top-{MAX_SWAPS} por delta"))
        approved = approved[:MAX_SWAPS]

    # ── Resumo / apply ──
    mode = "DRY-RUN" if args.dry_run else "APPLY"
    lines = [f"Config base: v{cfg.get('_version')} ({cfg.get('_updated_by')})",
             f"Candidatos avaliados: {len([p for p in active_pairs])} pares ativos",
             "", f"APROVADAS ({len(approved)}):"] + \
            [f"• {a['line']} — Δ R$ {a['delta']:+,.0f}/30d" for a in approved] + \
            ["", f"SKIP ({len(skipped)}):"] + \
            [f"• {p}: {r}" for p, r in skipped]
    body = "\n".join(lines)
    log(body)

    summary_path = out_dir / f"nightly_apply_summary_{RUN_TAG}.json"
    summary_path.write_text(json.dumps({
        "mode": mode, "approved": approved, "skipped": skipped,
        "config_version_before": cfg.get("_version"),
        "generated_at": datetime.now().isoformat(),
    }, ensure_ascii=False, indent=2, default=str))
    log(f"summary: {summary_path}")

    if args.dry_run:
        notify(f"🌙 W880 apply noturna — {mode} (nenhuma escrita)", body)
        return 0

    if not approved:
        notify(f"🌙 W880 apply noturna — nenhum candidato atingiu os gates", body)
        log("nenhum aprovado — config intacto")
        return 0

    # Backup canônico (mesma convenção dos snapshots do cron) + apply.
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = PROJECT_ROOT / f"vt_config.json.snapshot_pre_w880_{ts}"
    shutil.copy2(PROJECT_ROOT / "vt_config.json", backup)
    log(f"backup: {backup.name}")

    from core.vt_config_loader import save_full_config

    cfg = load_config(force=True)  # read-modify-write do estado MAIS RECENTE
    for a in approved:
        cfg["strategy_by_tf"][a["pair"]] = a["strategy"]
        cfg["params_by_tf"][a["pair"]] = a["params"]  # substitui (não mescla)
    save_full_config(cfg, updated_by=WRITER_NAME)

    check = load_config(force=True)
    applied_ok = all(check["strategy_by_tf"].get(a["pair"]) == a["strategy"]
                     for a in approved)
    log(f"config novo: v{check.get('_version')} by {check.get('_updated_by')} "
        f"({'conf conferido' if applied_ok else 'DIVERGENCIA NO CONFERE'})")
    notify(
        f"🌙 W880 apply noturna — {len(approved)} troca(s) aplicada(s) "
        f"(v{check.get('_version')})",
        body + f"\n\nBackup: {backup.name}\nConferência: "
               + ("OK" if applied_ok else "DIVERGÊNCIA — ver log"),
    )
    return 0 if applied_ok else 3


if __name__ == "__main__":
    sys.exit(main())
