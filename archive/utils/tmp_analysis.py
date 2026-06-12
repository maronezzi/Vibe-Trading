#!/usr/bin/env python3
"""Inteligencia Trader 11h analysis"""
import sys, sqlite3, time
sys.path.insert(0, '/home/bruno/Projects/Vibe-Trading')
from mt5_orchestrator import status, tick

s = status()
acc = s['account']
positions = s['positions']

now_str = time.strftime('%H:%M')
print(f'INTELIGENCIA TRADER - 11h Analysis')
print(f'Date: {time.strftime("%d/%m/%Y")} | Time: {now_str}')
print()

print('ACCOUNT:')
print(f'  Balance: R$ {acc["balance"]:,.2f}')
print(f'  Equity: R$ {acc["equity"]:,.2f}')

print()
print('OPEN POSITION:')
for p in positions:
    current = tick(p['symbol'])
    diff = current['last'] - p['price_open']
    print(f'  {p["type"]} {p["symbol"]} @ {p["price_open"]}')
    print(f'  Current: {current["last"]} | Diff: {diff:.1f} pts | P&L: R$ {p["profit"]:.2f}')

print()
print('TODAY TRADES:')
db = sqlite3.connect('vt_trades.db')
db.row_factory = sqlite3.Row
trades = db.execute('''
    SELECT symbol, direction, entry_price, exit_price, net_pnl, exit_reason, entry_time, exit_time
    FROM trades WHERE date(entry_time) >= date('now', '-3 hours')
    ORDER BY entry_time
''').fetchall()

total_pnl = 0
wins = 0
for i, t in enumerate(trades, 1):
    st = '+' if t['net_pnl'] > 0 else '-' if t['net_pnl'] < 0 else '='
    if t['net_pnl'] > 0:
        wins += 1
    total_pnl += t['net_pnl']
    entry = str(t['entry_time'])[:16] if t['entry_time'] else '?'
    exit_ = str(t['exit_time'])[:16] if t['exit_time'] else 'OPEN'
    print(f'  {i}. {st} {t["symbol"]} {t["direction"]} | {entry} -> {exit_} | R$ {t["net_pnl"]:.2f} | {t["exit_reason"]}')

wr = (wins / len(trades) * 100) if trades else 0
print(f'  Summary: {len(trades)} trades | WR: {wr:.0f}% | PnL: R$ {total_pnl:.2f}')

print()
print('5-DAY PERFORMANCE:')
five_day = db.execute('''
    SELECT symbol, timeframe,
           COUNT(*) as total,
           SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as wins,
           SUM(net_pnl) as pnl
    FROM trades WHERE exit_time IS NOT NULL
    AND date(entry_time) >= date('now', '-5 days')
    GROUP BY symbol, timeframe
''').fetchall()
for t in five_day:
    wr5 = (t['wins'] / t['total'] * 100) if t['total'] else 0
    pnl = t['pnl']
    sign = '+' if pnl > 0 else '-'
    print(f'  {sign} {t["symbol"]} {t["timeframe"]}: {t["total"]} trades | WR: {wr5:.0f}% | PnL: R$ {pnl:.2f}')

db.close()
