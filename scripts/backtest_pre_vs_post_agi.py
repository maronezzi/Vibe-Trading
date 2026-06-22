"""Validação final pré-AGI vs pós-AGI."""
import json
import pandas as pd
import sqlite3
from pathlib import Path

with open('/home/bruno/Projects/Vibe-Trading/vt_config.json') as f:
    cfg_now = json.load(f)

cfg_pre = json.loads(json.dumps(cfg_now))
cfg_pre['disabled_timeframes'] = [t for t in cfg_pre.get('disabled_timeframes', [])
                                   if t not in ['DOL_M30', 'DOL_M15', 'DOL_H1', 'IND_M30']]
cfg_pre['halt_duration_minutes'] = 60

mult_correct = {'WIN': 0.2, 'WDO': 10.0, 'BIT': 1.0, 'DOL': 50.0, 'IND': 1.0, 'WSP': 1.0}

def get_root(s):
    for r in ['WIN', 'WDO', 'BIT', 'DOL', 'IND', 'WSP']:
        if r in s: return r

conn = sqlite3.connect('/home/bruno/Projects/Vibe-Trading/vt_trades.db')
df = pd.read_sql_query("""
    SELECT symbol, timeframe, net_pnl, multiplier, entry_time
    FROM trades
    WHERE entry_time >= '2026-05-19'
""", conn)

def apply(df, cfg):
    df = df.copy()
    df['root'] = df['symbol'].apply(get_root)
    df['tf_key'] = df['root'] + '_' + df['timeframe']
    df['allowed'] = True
    df.loc[df['root'].isin(cfg.get('disabled_symbols', [])), 'allowed'] = False
    df.loc[df['tf_key'].isin(cfg.get('disabled_timeframes', [])), 'allowed'] = False
    df['pnl_real'] = df.apply(
        lambda r: r['net_pnl'] * (mult_correct.get(r['root'], 1.0) / (r['multiplier'] or 1.0))
                  if r['allowed'] else 0,
        axis=1
    )
    return df

old = apply(df, cfg_pre)
new = apply(df, cfg_now)

print("=" * 75)
print("BACKTEST FORWARD: Pré-AGI (rollback) vs Pós-AGI (atual)")
print("=" * 75)

print(f"\n{'Métrica':<35} {'Pré-AGI':>15} {'Pós-AGI':>15}")
print("-" * 75)
print(f"{'Trades permitidos':<35} {old['allowed'].sum():>15} {new['allowed'].sum():>15}")
print(f"{'Trades bloqueados':<35} {(~old['allowed']).sum():>15} {(~new['allowed']).sum():>15}")
pnl_o = old[old['allowed']]['pnl_real'].sum()
pnl_n = new[new['allowed']]['pnl_real'].sum()
print(f"{'PnL real (permitidos)':<35} R$ {pnl_o:>+12.2f} R$ {pnl_n:>+12.2f}")
print(f"\n>>> Δ PnL: R$ {(pnl_n - pnl_o):+.2f}")

print(f"\n=== Por símbolo (PnL_real) ===")
print(f"{'Sym':<6} {'N_old':>6} {'PnL_old':>10} {'N_new':>6} {'PnL_new':>10} {'Δ':>10}")
total_delta = 0
for sym in ['WIN', 'WDO', 'BIT', 'DOL', 'IND', 'WSP']:
    old_s = old[(old['allowed']) & (old['root'] == sym)]
    new_s = new[(new['allowed']) & (new['root'] == sym)]
    delta_s = new_s['pnl_real'].sum() - old_s['pnl_real'].sum()
    total_delta += delta_s
    print(f"{sym:<6} {len(old_s):>6} R$ {old_s['pnl_real'].sum():>+8.2f} {len(new_s):>6} R$ {new_s['pnl_real'].sum():>+8.2f} R$ {delta_s:>+8.2f}")
print(f"\nTotal Δ: R$ {total_delta:+.2f}")