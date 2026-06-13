#!/usr/bin/env python3
"""Analyze morning session trades and identify problems."""
import sqlite3
import json

conn = sqlite3.connect('vt_trades.db')
c = conn.cursor()

# Today's morning session trades
print("=== TRADES HOJE (manha < 12h) ===")
c.execute("""
SELECT symbol, timeframe, COUNT(*) as trades, 
       ROUND(SUM(net_pnl), 2) as pnl, 
       ROUND(AVG(CASE WHEN net_pnl>0 THEN 1.0 ELSE 0.0 END)*100, 1) as wr
FROM trades 
WHERE date(entry_time) = date('now') 
  AND strftime('%H', entry_time) < '12'
GROUP BY symbol, timeframe 
ORDER BY symbol, timeframe
""")
rows = c.fetchall()
if rows:
    print("Symbol     TF     Trades       PnL    WR%")
    print("-" * 45)
    for r in rows:
        print(f"{r[0]:<10} {r[1]:<6} {r[2]:>7} {r[3]:>10} {r[4]:>6}")
else:
    print("Nenhum trade de manha hoje.")

print()

# Also check all trades today
print("=== TODOS OS TRADES HOJE ===")
c.execute("""
SELECT symbol, timeframe, COUNT(*) as trades, 
       ROUND(SUM(net_pnl), 2) as pnl, 
       ROUND(AVG(CASE WHEN net_pnl>0 THEN 1.0 ELSE 0.0 END)*100, 1) as wr
FROM trades 
WHERE date(entry_time) = date('now')
GROUP BY symbol, timeframe 
ORDER BY symbol, timeframe
""")
rows = c.fetchall()
if rows:
    print("Symbol     TF     Trades       PnL    WR%")
    print("-" * 45)
    for r in rows:
        print(f"{r[0]:<10} {r[1]:<6} {r[2]:>7} {r[3]:>10} {r[4]:>6}")
else:
    print("Nenhum trade hoje.")

print()

# Check all symbols from config
print("=== COBERTURA POR ATIVO ===")
all_symbols = ["WINM26", "BITM26", "DOLN26", "INDM26", "WSPM26"]
c.execute("""
SELECT symbol, COUNT(*) as total, 
       ROUND(SUM(net_pnl), 2) as pnl,
       ROUND(AVG(CASE WHEN net_pnl>0 THEN 1.0 ELSE 0.0 END)*100, 1) as wr
FROM trades 
WHERE date(entry_time) = date('now')
GROUP BY symbol
""")
traded = {r[0]: r for r in c.fetchall()}
for s in all_symbols:
    if s in traded:
        r = traded[s]
        print(f"  {s}: {r[1]} trades, PnL={r[2]}, WR={r[3]}%")
    else:
        print(f"  {s}: SEM TRADES HOJE")

print()

# Last 10 trades for context
print("=== ULTIMOS 10 TRADES ===")
c.execute("""
SELECT symbol, timeframe, entry_time, exit_time, net_pnl, exit_reason
FROM trades 
ORDER BY entry_time DESC 
LIMIT 10
""")
for r in c.fetchall():
    print(f"  {r[0]:<10} {r[1]:<6} entry={r[2]} pnl={r[4]:>8} reason={r[5]}")

# Strategy per timeframe
print()
print("=== ESTRATEGIAS POR ATIVO/TF (config v19) ===")
with open('vt_config.json') as f:
    cfg = json.load(f)
print("strategy_by_tf overrides:")
for k, v in cfg.get('strategy_by_tf', {}).items():
    print(f"  {k} -> {v}")
print()
print("Base strategies:")
for k, v in cfg.get('strategy', {}).items():
    print(f"  {k} -> {v}")

# Show current params for each asset
print()
print("=== PARAMETROS ATUAIS ===")
for asset in ['win', 'bit', 'dol', 'ind', 'wsp']:
    if asset in cfg:
        print(f"\n{asset.upper()}:")
        for k, v in cfg[asset].items():
            print(f"  {k}: {v}")
