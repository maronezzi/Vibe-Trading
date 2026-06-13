#!/usr/bin/env python3
"""AGI 17h tuning — 2026-06-12"""
import sys
sys.path.insert(0, '.')
from vt_config_loader import load_config, save_params
import json

config = load_config(force=True)

print("=" * 60)
print("📊 ANÁLISE AGI — 2026-06-12 17h")
print("=" * 60)

print("\n📈 PERFORMANCE DO DIA:")
print("  BIT: 6 trades | PnL: -3,027.20 | CRÍTICO")
print("  IND: 3 trades | PnL:    -643.60 | RUIM")
print("  WIN: 2 trades | PnL:    -124.40 | RUIM")
print("  DOL: 1 trade  | PnL:     +10.80 | OK")
print("  WSP: 1 trade  | PnL:     +12.80 | OK")
print("  TOTAL: 13 trades | PnL: -3,771.60 | WR: 30.8%")

changes = []

# BIT (VWAP) — CRITICAL: -3027.20
print(f"\nBIT: vwap_buy {config['bit']['vwap_buy_threshold']}→1.008, sell {config['bit']['vwap_sell_threshold']}→0.992, cooldown {config['bit']['cooldown_seconds']}→600, max_trades {config['bit']['max_daily_trades']}→5")
bit_new = {
    "vwap_buy_threshold": 1.008,
    "vwap_sell_threshold": 0.992,
    "cooldown_seconds": 600,
    "max_daily_trades": 5
}
changes.append(("BIT", "VWAP", bit_new))

# IND (BOLLINGER) — -643.60
print(f"IND: bb_std {config['ind']['bb_std']}→2.2, rsi_ob {config['ind']['rsi_overbought']}→70, rsi_os {config['ind']['rsi_oversold']}→30, cooldown {config['ind']['cooldown_seconds']}→900")
ind_new = {
    "bb_std": 2.2,
    "rsi_overbought": 70,
    "rsi_oversold": 30,
    "cooldown_seconds": 900
}
changes.append(("IND", "BOLLINGER", ind_new))

# WIN (BOLLINGER) — -124.40
print(f"WIN: bb_std {config['win']['bb_std']}→2.1, rsi_ob {config['win']['rsi_overbought']}→70, rsi_os {config['win']['rsi_oversold']}→30, cooldown {config['win']['cooldown_seconds']}→900")
win_new = {
    "bb_std": 2.1,
    "rsi_overbought": 70,
    "rsi_oversold": 30,
    "cooldown_seconds": 900
}
changes.append(("WIN", "BOLLINGER", win_new))

# DOL (EMA_PULLBACK) — +10.80 (small sample)
print(f"DOL: adx {config['dol']['adx_threshold']}→18, pullback {config['dol']['pullback_pct']}→0.10")
dol_new = {
    "adx_threshold": 18,
    "pullback_pct": 0.10
}
changes.append(("DOL", "EMA_PULLBACK", dol_new))

# WSP (MACD_MOMENTUM) — +12.80 (small sample)
print(f"WSP: macd_signal {config['wsp']['macd_signal']}→12, adx {config['wsp']['adx_threshold']}→15")
wsp_new = {
    "macd_signal": 12,
    "adx_threshold": 15
}
changes.append(("WSP", "MACD_MOMENTUM", wsp_new))

# APPLY
print("\nAPPLYING...")
for symbol, strategy, params in changes:
    ok = save_params(symbol, params, updated_by="agi_17h_tuning")
    status = "OK" if ok else "FAIL"
    print(f"  {status} {symbol} ({strategy}) — {list(params.keys())}")

config_after = load_config(force=True)
print(f"\nConfig: v{config_after['_version']} by {config_after['_updated_by']}")
print("DONE")
