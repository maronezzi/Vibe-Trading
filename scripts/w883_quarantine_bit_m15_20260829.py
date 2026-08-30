#!/usr/bin/env python3
"""
w883_quarantine_bit_m15_20260829.py — quarentena manual do BIT_M15
(DIVERGENCE_RSI), runbook AGI_V4_NORMA.md §13.

Bruno 29/08/2026 aprovou a quarentena do par ("5 - ok") após auditoria
multi-agente: DIVERGENCE_RSI no BIT_M15 foi o pior sangrador do sistema —
-17,3R em 15 dias, win rate 22%, perda média -1,92R (o DOBRO do risco
planejado por trade — execução além do SL), 31 aberturas consumindo o stop
diário do BIT. É também o par mais ativo, ou seja: risco máximo, retorno
negativo.

O que faz (idempotente):
  1. Adiciona "BIT_M15" a disabled_timeframes e seta
     day_trade_intent["BIT_M15"]=false — mesmo mecanismo do
     stage5_apply._deactivate_failing_pairs (única diferença: decisão
     humana documentada, não sim).
  2. Registra no journal (kind="live_kill", rule="manual_quarantine_883")
     para a quarentena de 10 dias SEGURAR a reativação por simulação — sem
     isso o AGI religaria o par na segunda-feira (sim 30d dele é positiva;
     é exatamente o caso live_kill: sim bonita, bolso negativo).
  3. Avisa no Telegram.

Escrita via save_full_config (lock/whitelist/_updated_by). RODAR FORA DO
PREGÃO ou com o daemon em estado estável — mesma régua dos scripts AGI.
Reversão: remover BIT_M15 de disabled_timeframes e religar
day_trade_intent (manualmente ou deixando o AGI reavaliar após a
quarentena).

Usage:
    /usr/bin/python3 scripts/w883_quarantine_bit_m15_20260829.py [--dry-run]

Exit codes: 0 = ok (aplicado ou já estava) · 1 = erro.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

PAIR = "BIT_M15"
SESSION = "w883_manual_20260829"


def main(dry_run: bool = False) -> int:
    from core.vt_config_loader import load_config, save_full_config

    cfg = load_config(force=True)
    disabled = list(cfg.get("disabled_timeframes", []) or [])
    dti = dict(cfg.get("day_trade_intent", {}) or {})

    already = PAIR in disabled and not dti.get(PAIR, True)
    if already:
        print(f"[OK] {PAIR} já está em quarentena — nada a fazer.")
        return 0

    print(f"[QUARENTENA] {PAIR} (DIVERGENCE_RSI) — evidência: -17,3R/15d, "
          f"WR 22%, perda média -1,92R (auditoria 29/08; autorização Bruno 29/08)")
    print(f"  disabled_timeframes: {disabled} → {disabled + [PAIR] if PAIR not in disabled else disabled}")

    if dry_run:
        print("[DRY-RUN] nenhuma escrita.")
        return 0

    if PAIR not in disabled:
        disabled = disabled + [PAIR]
    dti[PAIR] = False
    cfg["disabled_timeframes"] = disabled
    cfg["day_trade_intent"] = dti
    save_full_config(cfg, updated_by="w883_quarantine_bit_m15")

    # Journal: kind=live_kill ativa a quarentena de 10d contra reativação
    # por sim no stage5_apply (is_quarantined lê ts/pair/kind).
    try:
        from optimization.agi_v4 import non_regression
        non_regression.append_journal({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "kind": "live_kill",
            "pair": PAIR,
            "rule": "manual_quarantine_883",
            "pnl": -17.3,
            "pnl_unit": "R",
            "n_trades": 31,
            "session": SESSION,
            "note": "Quarentena manual Bruno 29/08 — DIVERGENCE_RSI BIT_M15 "
                    "-17,3R/15d, WR 22%, perda média -1,92R (auditoria). "
                    "Reativar só após reavaliação (runbook §13).",
        })
    except Exception as e:
        print(f"[AVISO] journal não gravado ({e}) — quarentena de 10d pode "
              f"não segurar a reativação por sim; registre manualmente.")

    try:
        from core.vt_hermes_helper import hermes_send
        hermes_send(
            "telegram:-1004284773048:1",
            "🔒 [W883] Quarentena manual BIT_M15 (DIVERGENCE_RSI) aplicada.\n"
            "Evidência: -17,3R em 15d, WR 22%, perda média -1,92R.\n"
            "Quarentena 10d contra reativação por sim (journal live_kill).\n"
            "Autorização: Bruno 29/08. Reversão: runbook §13 da NORMA."
        )
    except Exception as e:
        print(f"[AVISO] telegram falhou: {e}")

    print(f"[APLICADO] {PAIR} em quarentena (config + journal).")
    return 0


if __name__ == "__main__":
    _dry = "--dry-run" in sys.argv
    sys.exit(main(dry_run=_dry))
