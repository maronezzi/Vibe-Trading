#!/usr/bin/env python3
"""One-off: habilita TODOS os timeframes do WIN (remove WIN_* de disabled_timeframes)
e seta day_trade_intent=true para WIN_M30 e WIN_H1.

Autorizado: Bruno 2026-07-31. Rodar com autotrader PAUSADO.
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.vt_config_loader import load_config, save_full_config

PAUSE_MARKER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "autotrader.paused")

if not os.path.exists(PAUSE_MARKER):
    print("ABORT: data/autotrader.paused não existe. Pause o autotrader primeiro.")
    sys.exit(1)

cfg = load_config(force=True)

# Snapshot antes
print("ANTES disabled_timeframes:", cfg.get("disabled_timeframes", []))
print("ANTES day_trade_intent (WIN):", {k:v for k,v in cfg.get("day_trade_intent",{}).items() if k.startswith("WIN")})

# 1. Remove WIN_* de disabled_timeframes
dt = cfg.get("disabled_timeframes", [])
dt_new = [x for x in dt if not x.startswith("WIN_")]
removed = [x for x in dt if x.startswith("WIN_")]
cfg["disabled_timeframes"] = dt_new

# 2. Habilita day_trade_intent para todos TFs do WIN
dti = cfg.get("day_trade_intent", {})
for tf in ["WIN_M5", "WIN_M15", "WIN_M30", "WIN_H1"]:
    dti[tf] = True
cfg["day_trade_intent"] = dti

# Sanity: chaves essenciais sobrevivem
assert "symbols" in cfg, "FATAL: symbols sumiu"
assert "resolved_symbols" in cfg, "FATAL: resolved_symbols sumiu"
assert len(cfg) >= 20, f"FATAL: config encolheu demais ({len(cfg)} chaves)"

save_full_config(cfg, updated_by="w_enable_all_win_tfs_20260731")

# Snapshot depois
cfg2 = load_config(force=True)
print("\nDEPOIS disabled_timeframes:", cfg2.get("disabled_timeframes", []))
print("DEPOIS day_trade_intent (WIN):", {k:v for k,v in cfg2.get("day_trade_intent",{}).items() if k.startswith("WIN")})
print(f"\nRemovidos de disabled_timeframes: {removed}")
print(f"_version: {cfg2.get('_version')} | _updated_by: {cfg2.get('_updated_by')}")
print("OK")
