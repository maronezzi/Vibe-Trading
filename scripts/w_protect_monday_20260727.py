"""
One-off: proteção pra segunda 27/07 — anti-repetição da sexta 24/07.
Autorizado por Bruno 2026-07-26.

Mudanças:
1. max_consecutive_losses_by_tf: 999 → 5 (WIN/BIT ativos)
2. loss_cooldown: 30min/2 losses → 60min/3 losses
3. strategy_by_tf: WIN_H1 e BIT_M30 → ENHANCED_RSI_REVERSION (tem ADX gate)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.vt_config_loader import load_config, save_full_config

cfg = load_config()

# 1. Freio de perdas consecutivas
mcl = cfg.get("max_consecutive_losses_by_tf", {})
for key in list(mcl.keys()):
    if mcl[key] == 999:
        mcl[key] = 5
cfg["max_consecutive_losses_by_tf"] = mcl

# 2. Cooldown mais longo
lc = cfg.get("loss_cooldown", {})
lc["max_consecutive"] = 3
lc["cooldown_minutes"] = 60
cfg["loss_cooldown"] = lc

# 3. Trocar piores combos pra ENHANCED_RSI_REVERSION
sbt = cfg.get("strategy_by_tf", {})
if sbt.get("WIN_H1") == "RSI_REVERSION":
    sbt["WIN_H1"] = "ENHANCED_RSI_REVERSION"
if sbt.get("BIT_M30") == "RSI_REVERSION":
    sbt["BIT_M30"] = "ENHANCED_RSI_REVERSION"
cfg["strategy_by_tf"] = sbt

save_full_config(cfg, updated_by="w_protect_monday_20260727")
print("✅ Config salva com proteções pra segunda")
print(f"   max_consecutive_losses: {mcl.get('WIN_M5')} (antes 999)")
print(f"   loss_cooldown: {lc['cooldown_minutes']}min / {lc['max_consecutive']} losses")
print(f"   WIN_H1: {sbt.get('WIN_H1')}")
print(f"   BIT_M30: {sbt.get('BIT_M30')}")
