#!/usr/bin/env python3
"""W876 Trader-IA 11h — Tighten WIN_M15 RSI thresholds for current regime.

Wave: hermes_wave_11h_regime_tighten (2026-07-09, qui, 11h00-11h10).

Contexto CRÍTICO (validado 11:00):
  WIN_M15 hoje (09/07) — 5 SINAIS: 4 GHOST com PnL=0 + 1 OPEN (M15 SELL @ 173250)
  Padrão: sinais RSI>75 disparam, position aberta, trail corre, depois RECONCILE
  marca GHOST porque bot tenta reabrir (DEFESA2-DRIFT bloqueia). 5 GHOST em 8 trades 30d.
  WIN_M15 30d últimos real (excl ghosts Wave 1C.2): n=8 WR=12.5% PnL -R$21.
  Backtest last 60 bars (regime atual):
    - OB=75/OS=30/p=10 (atual): n=9 WR=33% PnL -R$35
    - OB=78/OS=28/p=10 (prop.) : n=6 WR=50% PnL +R$565  ✓ +R$600

NÃO toca:
  - SL multipliers (regra: "NUNCA mude o SL multiplier sem justificativa")
  - Estratégias (regra: "NÃO mude estratégias")
  - ativos pausados (BIT_M5/M30, WDO_*, WSP_*)
  - BIT_M15 (30d +R$1134 é o melhor par, mexer é arriscado)

Uso: autotrader PAUSADO (data/autotrader.paused presente). Mudanças são
hot-reloaded em ≤30s pelo autotrader, sem restart.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.vt_config_loader import load_config, save_full_config

# Param tweaks (escopo: WIN_M15 apenas)
PARAMS_TWEAKS = {
    # WIN_M15: RSI 75/30 → 78/28. Backtest last 60 bars justificou (n=6 WR=50% vs n=9 WR=33%).
    "WIN_M15": {
        "rsi_overbought": 78,
        "rsi_oversold": 28,
    },
}


def main() -> int:
    dry = "--dry-run" in sys.argv
    cfg = load_config(force=True)

    print("=== Wave W876 — Trader-IA 11h (2026-07-09) ===")
    print(f"Config inicial: v{cfg.get('_version')} by={cfg.get('_updated_by')}")
    print()

    pbt = cfg.setdefault("params_by_tf", {})
    print(f"=== PARAM TWEAKS ({len(PARAMS_TWEAKS)} TFs) ===")
    for tf_key, tweaks in sorted(PARAMS_TWEAKS.items()):
        existing = pbt.get(tf_key, {})
        print(f"  {tf_key}:")
        for k, v in sorted(tweaks.items()):
            old = existing.get(k, "(herda de win)")
            print(f"    {k}: {old} -> {v}")

    # Sanity invariants
    wm = pbt.get("WIN_M15", {})
    assert wm.get("rsi_overbought", 0) > wm.get("rsi_oversold", 100), "OB > OS"
    assert 0 < wm.get("rsi_oversold", 0) < wm.get("rsi_overbought", 100) < 100, \
        "RSI thresholds devem estar em (0, 100)"
    print("\nInvariants OK (OB > OS, ambos em 0-100)")

    if dry:
        print("\n[DRY-RUN] nenhuma mudança aplicada.")
        return 0

    for tf_key, tweaks in PARAMS_TWEAKS.items():
        pbt.setdefault(tf_key, {}).update(tweaks)
    cfg["params_by_tf"] = pbt

    save_full_config(cfg, updated_by="hermes_wave_11h_regime_tighten")
    cfg2 = load_config(force=True)

    print()
    print(f"=== Salvo v{cfg2.get('_version')} by={cfg2.get('_updated_by')} ===")
    print(f"Disabled TFs (inalterados): {sorted(cfg2.get('disabled_timeframes', []))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
