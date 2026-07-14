#!/usr/bin/env python3
"""W873 Recovery (2026-07-07) — Re-aplica calibração broker-truth contract_specs.

Script cirúrgico de recovery do incidente W873 (AGI v3 restaurado pelo Hermes
sobrescreveu a calibração W872 do vt_config.json). Re-aplica os multipliers/slip
W873 (= W872 original) no contract_specs via save_full_config.

Autorizado: ALLOWED_WRITERS inclui scripts/* (uso com autotrader pausado).

Uso:
    python3 scripts/w873_recovery_20260707.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.vt_config_loader import load_config, save_full_config

# W873 = W872 (2026-07-06): broker-truth MT5.
#   slip_r = slippage_pts × mult (1-2 ticks B3). Ver Lei 4 (MT5 é truth).
W873_SPECS = {
    "WIN$": {"mult": 1.0,    "margin": 155, "slip_r": 5.0,    "tick": 5},
    "WDO$": {"mult": 0.0015, "margin": 140, "slip_r": 0.0015, "tick": 0.5},
    "BIT$": {"mult": 0.01,   "margin": 45,  "slip_r": 0.0002, "tick": 0.01},
    "WSP$": {"mult": 0.01,   "margin": 100, "slip_r": 0.0002, "tick": 0.01},
    "DOL$": {"mult": 0.0018, "margin": 140, "slip_r": 0.0018, "tick": 0.5},
    "IND$": {"mult": 1.0,    "margin": 155, "slip_r": 5.0,    "tick": 5},
}


def main():
    cfg = load_config()

    print("=== ANTES (W872 perdido pelo AGI v3 em 07/07) ===")
    cs = cfg.get("contract_specs", {})
    for k in W873_SPECS:
        print(f"  {k}: {cs.get(k)}")

    cfg["contract_specs"] = W873_SPECS

    print("\n=== DEPOIS (W873 aplicado) ===")
    for k in W873_SPECS:
        print(f"  {k}: {cfg['contract_specs'][k]}")

    save_full_config(cfg, updated_by="w873_recovery_vt_config")
    print("\n✓ vt_config.json salvo com contract_specs W873 (by=w873_recovery_vt_config)")


if __name__ == "__main__":
    main()
