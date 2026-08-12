#!/usr/bin/env python3
"""monitor_agi_run.py — Monitora a run mais recente do AGI v4.

Verifica se as capacidades das waves AGI-param-tuning / param-tuning-2 estão
funcionando na run mais recente (cron 12:00 ou 17:10):

  - START/FATAL lifecycle (Telegram enviado?)
  - bootstrap AGI4 sanctioned (registro de params próprios no processo principal)
  - Stage 4b tuning (estratégias novas otimizadas? defaults mantidos?)
  - Stage 3 grids AGI4 (bootstrap populou STRATEGY_PARAM_GRIDS?)
  - Params próprios ACEITOS pelo guardrail (fix de ordem funcionando?)
  - Zombie drop (params residuais removidos?)
  - Relatório final enriquecido enviado?

Somente LEITURA: lê /tmp/vt_agi_v4_latest.log + /tmp/vt_agi_v4_audit.json.
Nunca toca no sistema live. Seguro para rodar a qualquer momento.

Uso:
    python3 scripts/monitor_agi_run.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

LOG = Path("/tmp/vt_agi_v4_latest.log")
AUDIT = Path("/tmp/vt_agi_v4_audit.json")


def _read_log() -> str:
    if not LOG.exists():
        return ""
    try:
        return LOG.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"__ERRO_LEITURA__: {e}"


def _load_audit() -> dict | None:
    if not AUDIT.exists():
        return None
    try:
        return json.loads(AUDIT.read_text(encoding="utf-8"))
    except Exception:
        return None


def _count(pattern: str, text: str) -> int:
    return len(re.findall(pattern, text))


def _grep_lines(pattern: str, text: str, limit: int = 5) -> list[str]:
    matches = re.findall(pattern, text)
    return matches[:limit]


def _status_icon(ok: bool | None) -> str:
    if ok is True:
        return "✅"
    if ok is None:
        return "⏳"
    return "❌"


def main() -> int:
    log = _read_log()
    audit = _load_audit()

    if not log or log.startswith("__ERRO_LEITURA__"):
        print(f"⚠️ Sem log do AGI em {LOG} (run ainda não começou?).")
        return 1

    started = "═══ AGI v4 START ═══" in log
    fatal = "═══ AGI v4 FATAL ═══" in log
    done = "═══ AGI v4 DONE ═══" in log

    print("═" * 64)
    print("  MONITOR AGI v4 — run mais recente")
    print("═" * 64)

    # ── Estado da run ──
    if fatal:
        print(f"{_status_icon(False)} RUN FALHOU (FATAL) — ver log para detalhe")
    elif started and not done:
        print(f"{_status_icon(None)} RUN EM ANDAMENTO (START sem DONE)")
    elif done:
        print(f"{_status_icon(True)} RUN FINALIZADA (DONE)")
    else:
        print(f"{_status_icon(None)} RUN: estado indeterminado (sem START/DONE no log)")

    if not started:
        print(f"\n⚠️ Nenhum START encontrado — a run das 17:10 pode não ter disparado.")
        print(f"   Log: {LOG}")
        return 1

    print()

    # ── Bootstrap AGI4 sanctioned (#2) ──
    boot_matches = _grep_lines(r"bootstrap AGI4 sanctioned: (\d+) estratégia", log)
    n_sanctioned = int(boot_matches[0]) if boot_matches else 0
    print(f"{_status_icon(n_sanctioned > 0)} Bootstrap AGI4 sanctioned "
          f"({n_sanctioned} estratégia(s) registrada(s) no processo principal)")

    # ── Stage 4b tuning ──
    n_tuned = _count(r"tune AGI4.*: OTIMIZADO", log)
    n_defaults = _count(r"tune AGI4.*: defaults", log)
    n_tune_tried = _count(r"tune AGI4.*: testando \d+ combo", log)
    if n_tune_tried > 0:
        print(f"{_status_icon(True)} Stage 4b tuning: {n_tuned} otimizada(s), "
              f"{n_defaults} default(s) mantido(s) ({n_tune_tried} tentativa(s))")
    else:
        print(f"{_status_icon(None)} Stage 4b tuning: nenhuma estratégia nova "
              f"gerada/testada nesta run (pode ser normal se sem failing)")

    # ── Params próprios ACEITOS vs rejeitados (fix #1) ──
    # "APLICADO {pair}" no stage5 indica que passou pelo guardrail.
    n_aplicado = _count(r"APLICADO [A-Z]+_[A-Z0-9]+:", log)
    n_guardrail_reject = _count(r"AGI GUARDRAIL rejeitou", log)
    # Rejeições de guardrail_reject (gate) entram no rejected — distinga params
    # próprios de outros motivos. Se há APLICADO e poucas rejeições, está ok.
    if n_aplicado > 0:
        print(f"{_status_icon(True)} Params aplicados pelo guardrail: "
              f"{n_aplicado} mudança(s) APLICADA(s)")
    print(f"   guardrail rejeitou: {n_guardrail_reject} vez(es) "
          f"(esperado ~0 para params sancionados)")

    # ── Zombie drop (#3) ──
    zombie_matches = _grep_lines(r"zombie_drop (\S+): removendo (\d+) param", log)
    n_zombie = len(zombie_matches)
    if n_zombie > 0:
        detail = ", ".join(f"{m.split(':')[0].split()[-1]}" for m in
                           _grep_lines(r"zombie_drop (\S+):", log))
        print(f"{_status_icon(True)} Zombie drop: {n_zombie} par(es) com params "
              f"residuais removidos ({detail})")
    else:
        print(f"{_status_icon(None)} Zombie drop: nenhum nesta run "
              f"(normal se não houve troca de estratégia)")

    # ── Audit JSON (se disponível) ──
    print()
    if audit:
        gen = audit.get("generated_strategies", []) or []
        tuned_in_audit = [g for g in gen
                          if g.get("status") == "approved_pending"
                          and g.get("tuned_params")]
        applied = audit.get("applied_changes", []) or []
        profit_opt = audit.get("profit_optimizations", []) or []
        print(f"📋 Audit ({AUDIT.name}):")
        print(f"   • estratégias geradas: {len(gen)} ({len(tuned_in_audit)} com tuning)")
        print(f"   • mudanças aplicadas: {len(applied)}")
        print(f"   • otimizações de lucrativos: {len(profit_opt)}")
        conv = audit.get("converged")
        stag = audit.get("stag_") or "stagnated" in str(audit)
        print(f"   • convergiu: {conv}")
    else:
        print(f"⏳ Audit JSON ainda não escrito (run em andamento)")

    # ── Resumo ──
    print()
    print("═" * 64)
    if fatal:
        print("🔴 RUN FALHOU — investigar /tmp/vt_agi_v4_latest.log")
        return 2
    if done:
        print("🟢 Run finalizada — confira o Telegram para o relatório completo.")
    else:
        print("🟡 Run em andamento — rode novamente mais tarde para ver o final.")
    print(f"   Log:  {LOG}")
    print(f"   Audit: {AUDIT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
