#!/usr/bin/env python3
"""One-off: rolar BIT de BITN26 (vence 31/07) para BITQ26 (ago/2026)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.vt_config_loader import load_config, save_full_config

config = load_config()
resolved = config.get("resolved_symbols", {})
old = resolved.get("BIT")
if old == "BITQ26":
    print("BIT já é BITQ26 — nada a fazer.")
    sys.exit(0)

resolved["BIT"] = "BITQ26"
config["resolved_symbols"] = resolved
save_full_config(config, updated_by="w_roll_bit_q26_20260731")
print(f"✅ BIT: {old} → BITQ26")
