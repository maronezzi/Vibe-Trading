#!/usr/bin/env python3
"""W874 Trader-IA 13h — Optimize WIN-overtrading + (NO) pause WIN_M15.

Wave: hermes_wave_13h_pause_losing (2026-07-08, qua, 13h00-13h10).

Contexto CRÍTICO (validado 13:00):
  W873 pausou na noite 07/07 todos os BIT (4) + WSP (4) + WDO (4) = 12 TFs.
  Restam 4 WIN TFs (M5/M15/M30/H1) como ÚNICOS ativos operacionais.
  Hoje (08/07) vimos 4 trades WIN virando GHOST em <15min — total PnL R$0.
  WIN_M15 30d: 38 trades, WR 15.8%, PnL -R$290 — overtrading clássico.

Decisão (NÃO pausar WIN_M15, mas otimizar):
  - Reabilitar WIN_M15 com breakeven 8min (era 3, gerou 100% GHOST hoje)
    e cooldown 600s (era 120s — gerava ruído).
  - Reduzir max_daily_trades WIN_M5/M30 para 4 cada (anti-overtrading).
  - Aumentar breakeven WIN_M30 de 3 (herdado de base win) para 10min.
  - NÃO pausar WIN_M15 — Estratégia=RSI_REVERSION está boa, só parâmetros
    mal calibrados. Pausar quebraria Lei 2 (nunca tirar símbolo inteiro).

Uso: autotrader PAUSADO (data/autotrader.paused presente). Mudanças são
hot-reloaded em ≤30s pelo autotrader, sem restart.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.vt_config_loader import load_config, save_full_config

# Parâmetros por TF (path params_by_tf)
PARAMS_TWEAKS = {
    # WIN_M15: reabilitar com RSI_REVERSION bom — hoje 2 GHOST com breakeven 3.
    # Cooldown 120→600s evita sinais duplicados em M15.
    "WIN_M15": {"cooldown_seconds": 600, "breakeven_minutes": 8},
    # WIN_M5: cooldown 300→600 (28 trades/30d), max_daily 8→4.
    "WIN_M5": {"cooldown_seconds": 600, "max_daily_trades": 4},
    # WIN_M30: breakeven herda 3 da base win, mas hoje GHOST em <15min.
    "WIN_M30": {"breakeven_minutes": 10, "max_daily_trades": 4},
}


def main() -> int:
    dry = "--dry-run" in sys.argv
    cfg = load_config(force=True)

    print(f"=== Wave W874 — Trader-IA 13h (2026-07-08) ===")
    print(f"Config inicial: v{cfg.get('_version')} by={cfg.get('_updated_by')}")
    print()

    pbt = cfg.setdefault("params_by_tf", {})
    print(f"=== PARAM TWEAKS ({len(PARAMS_TWEAKS)} WIN TFs) ===")
    for tf_key, tweaks in sorted(PARAMS_TWEAKS.items()):
        existing = pbt.get(tf_key, {})
        print(f"  {tf_key}:")
        for k, v in sorted(tweaks.items()):
            print(f"    {k}: {existing.get(k, '(inherit from base win)')} → {v}")

    print()
    print("Importante: NÃO pausamos nenhum TF adicional.")
    print("BIT/WSP/WDO seguem pausados desde W873 (07/07).")
    print("Restam WIN_M5/M15/M30/H1 = 4 pares ativos (todos WIN).")

    if dry:
        print("\n[DRY-RUN] nenhuma mudança aplicada.")
        return 0

    for tf_key, tweaks in PARAMS_TWEAKS.items():
        pbt.setdefault(tf_key, {}).update(tweaks)
    cfg["params_by_tf"] = pbt

    save_full_config(cfg, updated_by="hermes_wave_13h_pause_losing")
    cfg2 = load_config(force=True)

    print()
    print(f"=== Salvo v{cfg2.get('_version')} by={cfg2.get('_updated_by')} ===")
    print(f"Ativos WIN: {sorted(cfg2.get('disabled_timeframes',[])) or '(4 WIN ativos)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
