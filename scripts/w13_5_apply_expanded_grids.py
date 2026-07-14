#!/usr/bin/env python3
"""
w13_5_apply_expanded_grids.py — Aplica candidatos do AGI v4 com grids expandidos (Wave 13.5).

Roda APENAS com autotrader PAUSADO (fora do horário de trading).

Candidatos validados pelo AGI v4 dry-run com optimization/exhaustive_strategy_search.py
grids expandidos (MAX_COMBOS 30→80, UNIVERSAL 768→16464 combos, +11 estratégias com grid):

  1. BIT_M5: SUPERTREND (4/4 walk-forward positivo, PF=2.14, Sharpe=4.09)
     vs baseline MACD_MOMENTUM (PnL R$ -19,96). Delta +R$ 226.

  2. WIN_H1: RSI_REVERSION (3/4 walk-forward, PF=1.25, Sharpe=1.40)
     vs baseline HTF_BIAS_LTF_ENTRY (PnL R$ -2.180,84). Delta +R$ 4.750.
     ⚠️ Janela 4 do walk-forward: -R$ 4.549 (risco de dia catastrófico).

AGENTS.md diz: "scripts/* invoked with the autotrader paused" podem escrever.
Este módulo está no whitelist via `scripts/*` (resolve_relative_path no loader).
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def main() -> int:
    from core.vt_config_loader import load_config, save_full_config

    cfg = load_config(force=True)
    old_ver = cfg.get("_version", 0)

    # ── 1. BIT_M5: SUPERTREND ──
    cfg["strategy_by_tf"]["BIT_M5"] = "SUPERTREND"
    cfg.setdefault("params_by_tf", {})["BIT_M5"] = {
        "atr_period": 18,
        "multiplier": 1.5,
        "sl_atr_mult": 0.8,
        "cooldown_seconds": 60,
        "max_consecutive_losses": 2,
        "halt_duration_minutes": 15,
        "profit_lock_r": 0.0,
    }

    # ── 2. WIN_H1: RSI_REVERSION ──
    cfg["strategy_by_tf"]["WIN_H1"] = "RSI_REVERSION"
    cfg["params_by_tf"]["WIN_H1"] = {
        "rsi_period": 5,
        "rsi_overbought": 65,
        "rsi_oversold": 30,
        "sl_atr_mult": 2.5,
        "cooldown_seconds": 180,
        "max_consecutive_losses": 3,
        "halt_duration_minutes": 30,
        "profit_lock_r": 2.0,
    }

    # Persistir
    save_full_config(cfg, updated_by="bruno_wave_13_5_expanded_grids")

    # Reload + diff
    cfg2 = load_config(force=True)
    print(f"✅ v{old_ver}→v{cfg2.get('_version')} (by {cfg2.get('_updated_by')})")
    print()
    print("strategy_by_tf diff:")
    for k in ("BIT_M5", "WIN_H1"):
        print(f"  {k}: {cfg.get('strategy_by_tf', {}).get(k)}  (params={cfg.get('params_by_tf', {}).get(k)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
