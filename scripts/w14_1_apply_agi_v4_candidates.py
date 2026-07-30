#!/usr/bin/env python3
"""w14_1_apply_agi_v4_candidates.py - Wave 14.1 candidates."""
from __future__ import annotations
import sys
from pathlib import Path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def main() -> int:
    from core.vt_config_loader import load_config, save_full_config
    cfg = load_config(force=True)
    cfg.setdefault("params_by_tf", {})
    cfg["strategy_by_tf"]["WIN_M5"] = "SMART_EMA"
    cfg["params_by_tf"]["WIN_M5"] = {"cooldown_seconds": 900, "ema_fast": 10, "ema_slow": 26, "halt_duration_minutes": 45, "max_consecutive_losses": 6, "profit_lock_r": 1.5, "sl_atr_mult": 1.8}
    cfg["strategy_by_tf"]["WIN_M15"] = "HTF_BIAS_LTF_ENTRY"
    cfg["params_by_tf"]["WIN_M15"] = {"cooldown_seconds": 900, "halt_duration_minutes": 45, "max_consecutive_losses": 6, "profit_lock_r": 0.3, "sl_atr_mult": 3.0}
    cfg["strategy_by_tf"]["WIN_M30"] = "HTF_BIAS_LTF_ENTRY"
    cfg["params_by_tf"]["WIN_M30"] = {"cooldown_seconds": 60, "halt_duration_minutes": 15, "max_consecutive_losses": 5, "profit_lock_r": 1.0, "sl_atr_mult": 2.0}
    cfg["strategy_by_tf"]["WIN_H1"] = "RSI_REVERSION"
    cfg["params_by_tf"]["WIN_H1"] = {"cooldown_seconds": 60, "halt_duration_minutes": 15, "max_consecutive_losses": 5, "profit_lock_r": 1.0, "rsi_overbought": 80, "rsi_oversold": 30, "rsi_period": 5, "sl_atr_mult": 2.5}
    cfg["strategy_by_tf"]["BIT_M5"] = "MACD_MOMENTUM"
    cfg["params_by_tf"]["BIT_M5"] = {"cooldown_seconds": 60, "halt_duration_minutes": 60, "macd_fast": 12, "macd_signal": 9, "macd_slow": 26, "max_consecutive_losses": 4, "profit_lock_r": 0.3, "sl_atr_mult": 2.5}
    cfg["strategy_by_tf"]["BIT_M15"] = "EMA_PULLBACK"
    cfg["params_by_tf"]["BIT_M15"] = {"cooldown_seconds": 300, "ema_fast": 5, "ema_slow": 20, "halt_duration_minutes": 60, "max_consecutive_losses": 8, "profit_lock_r": 1.0, "pullback_pct": 0.15, "sl_atr_mult": 3.0}
    cfg["strategy_by_tf"]["BIT_M30"] = "RSI_REVERSION"
    cfg["params_by_tf"]["BIT_M30"] = {"cooldown_seconds": 120, "halt_duration_minutes": 90, "max_consecutive_losses": 2, "profit_lock_r": 0.3, "rsi_overbought": 65, "rsi_oversold": 20, "rsi_period": 7, "sl_atr_mult": 1.8}
    save_full_config(cfg, updated_by="bruno_wave_14_1_agi_v4_candidates")
    cfg2 = load_config(force=True)
    print(f"OK v{cfg.get('_version', 0)} -> v{cfg2.get('_version')} (by {cfg2.get('_updated_by')})")
    for k in ("WIN_M5", "WIN_M15", "WIN_M30", "WIN_H1", "BIT_M5", "BIT_M15", "BIT_M30"):
        print(f"  {k}: {cfg2['strategy_by_tf'].get(k)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())