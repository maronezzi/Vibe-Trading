#!/usr/bin/env python3
"""Apply midday parameter optimizations for Vibe-Trading."""
import json
from datetime import datetime

# Load current config
with open('vt_config.json') as f:
    cfg = json.load(f)

old_version = cfg.get('_version', 0)
changes = []

# === BIT (VWAP) — 30 trades, 30% WR, R$-3034.8 ===
# Problem: thresholds too tight (1.002/0.998) causing overtrading on H1
old_buy = cfg['bit']['vwap_buy_threshold']
old_sell = cfg['bit']['vwap_sell_threshold']
cfg['bit']['vwap_buy_threshold'] = 1.005
cfg['bit']['vwap_sell_threshold'] = 0.995
changes.append(f"BIT: vwap_buy_threshold {old_buy} -> 1.005, vwap_sell_threshold {old_sell} -> 0.995 (widen to reduce overtrading)")

# === DOL (EMA_PULLBACK) — 0 trades ===
# Problem: adx_threshold=20 too strict, pullback_pct=0.1 too restrictive
old_adx = cfg['dol']['adx_threshold']
old_pb = cfg['dol']['pullback_pct']
cfg['dol']['adx_threshold'] = 15
cfg['dol']['pullback_pct'] = 0.08
changes.append(f"DOL: adx_threshold {old_adx} -> 15, pullback_pct {old_pb} -> 0.08 (relax filters for 0 trades)")

# === WSP (MACD_MOMENTUM) — 0 trades ===
# Problem: standard MACD (12,26,9) not generating signals
old_fast = cfg['wsp']['macd_fast']
old_slow = cfg['wsp']['macd_slow']
cfg['wsp']['macd_fast'] = 9
cfg['wsp']['macd_slow'] = 21
changes.append(f"WSP: macd_fast {old_fast} -> 9, macd_slow {old_slow} -> 21 (faster MACD for responsiveness)")

# === IND (BOLLINGER) — 1 trade, 0% WR ===
old_ob = cfg['ind']['rsi_overbought']
old_os = cfg['ind']['rsi_oversold']
cfg['ind']['rsi_overbought'] = 65
cfg['ind']['rsi_oversold'] = 35
changes.append(f"IND: rsi_overbought {old_ob} -> 65, rsi_oversold {old_os} -> 35 (widen RSI band)")

# === WIN (BOLLINGER) — 1 trade, 0% WR ===
old_ob_w = cfg['win']['rsi_overbought']
old_os_w = cfg['win']['rsi_oversold']
cfg['win']['rsi_overbought'] = 65
cfg['win']['rsi_oversold'] = 35
changes.append(f"WIN: rsi_overbought {old_ob_w} -> 65, rsi_oversold {old_os_w} -> 35 (widen RSI band)")

# Save config
cfg['_version'] = old_version + 1
cfg['_updated_at'] = datetime.now().isoformat()
cfg['_updated_by'] = 'meio_dia_12h'
cfg['_notes'] = f"v{cfg['_version']}: meio-dia param tuning - BIT widen VWAP, DOL relax ADX, WSP faster MACD, IND/WIN wider RSI"

with open('vt_config.json', 'w', encoding='utf-8') as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)

print(f"Config salva v{cfg['_version']} (by {cfg['_updated_by']})")
print()
print("=== RESUMO DE MUDANCAS ===")
for c in changes:
    print(f"  - {c}")
print()
print(f"Total: {len(changes)} ativos ajustados, v{old_version} -> v{cfg['_version']}")
