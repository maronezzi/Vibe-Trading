#!/usr/bin/env python3
"""SL Sensitivity Test — qual SL multiplier maximiza lucro por ativo."""
import subprocess, csv, io, os
import pandas as pd
import numpy as np

WINE_PYTHON = os.path.expanduser('~/.wine/drive_c/Python311/python.exe')
FETCH_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mt5_fetch.py')

def fetch(symbol, tf, n_bars=500):
    cmd = ['wine', WINE_PYTHON, FETCH_SCRIPT, 'rates', symbol, tf, str(n_bars)]
    env = {**os.environ, 'WINEDEBUG': '-all'}
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    if r.returncode != 0 or not r.stdout.strip():
        return pd.DataFrame()
    reader = csv.reader(io.StringIO(r.stdout.strip()))
    headers = next(reader)
    rows = [x for x in reader if x]
    df = pd.DataFrame(rows, columns=headers)
    for c in ['open','high','low','close','tick_volume','real_volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df['time'] = pd.to_datetime(df['time'].astype(int), unit='s')
    df = df.set_index('time')
    return df[['open','high','low','close','tick_volume']].dropna(subset=['close'])

def calc_atr(df, period=14):
    h, l = df['high'], df['low']
    c_prev = df['close'].shift(1)
    tr = pd.concat([h-l, (h-c_prev).abs(), (l-c_prev).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def calc_vwap(df, period=20):
    typical = (df['high'] + df['low'] + df['close']) / 3
    vol = df['tick_volume'].replace(0, 1)
    return (typical * vol).rolling(period).sum() / vol.rolling(period).sum()

def calc_bollinger(df, period=20, std=2.0):
    mid = df['close'].rolling(period).mean()
    s = df['close'].rolling(period).std()
    return mid + std * s, mid, mid - std * s

def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

CONTRACT = {
    'BIT': {'mult': 0.50, 'point': 100},
    'WDO': {'mult': 10.0, 'point': 1000},
    'IND': {'mult': 1.0,  'point': 1},
    'DOL': {'mult': 50.0, 'point': 1000},
    'WSP': {'mult': 2.5,  'point': 100},
}

SPECS = {
    'BIT': {'min_native': 30, 'max_native': 500, 'point_mult': 100},
    'WDO': {'min_native': 3, 'max_native': 12, 'point_mult': 1000},
    'IND': {'min_native': 150, 'max_native': 350, 'point_mult': 1},
    'DOL': {'min_native': 3, 'max_native': 200, 'point_mult': 1000},
    'WSP': {'min_native': 5, 'max_native': 200, 'point_mult': 100},
}

COMMISSION = 1.2
CLOSE_H, CLOSE_M = 16, 45

def run_backtest(df, name, strategy, sl_mult, vol=1):
    cs = CONTRACT[name]
    spec = SPECS[name]
    df = df.copy()
    df['atr'] = calc_atr(df)

    if strategy == 'VWAP':
        df['vwap'] = calc_vwap(df, 20)
        df['signal'] = np.where(df['close'] > df['vwap'] * 1.005, 1,
                                np.where(df['close'] < df['vwap'] * 0.995, -1, 0))
    elif strategy == 'BOLLINGER':
        upper, mid, lower = calc_bollinger(df, 20, 2.5)
        rsi = calc_rsi(df['close'], 14)
        df['signal'] = np.where((df['close'] < lower) & (rsi < 30), 1,
                                np.where((df['close'] > upper) & (rsi > 70), -1, 0))
    elif strategy == 'EMA_PULLBACK':
        ema_f = calc_ema(df['close'], 9)
        ema_s = calc_ema(df['close'], 21)
        df['signal'] = np.where((df['close'] > ema_s) & (df['close'] > ema_f) & (ema_f > ema_s), 1,
                                np.where((df['close'] < ema_s) & (df['close'] < ema_f) & (ema_f < ema_s), -1, 0))
    elif strategy == 'MACD_MOMENTUM':
        ema_f = calc_ema(df['close'], 9)
        ema_s = calc_ema(df['close'], 21)
        macd = ema_f - ema_s
        signal_line = calc_ema(macd, 12)
        df['signal'] = np.where(macd > signal_line, 1, np.where(macd < signal_line, -1, 0))
    else:
        df['signal'] = 0

    trades = []
    position = None
    cooldown = 0

    for i in range(len(df)):
        row = df.iloc[i]
        ts = df.index[i]

        if cooldown > 0:
            cooldown -= 1

        # Close at EOD
        if position and ts.hour >= CLOSE_H and ts.minute >= CLOSE_M:
            entry = position['entry']
            direction = position['dir']
            pnl = (row['close'] - entry) * direction * cs['mult'] * vol - COMMISSION
            trades.append({'pnl': pnl, 'bars': i - position['bar'], 'reason': 'EOD'})
            position = None
            continue

        # Check SL
        if position:
            entry = position['entry']
            direction = position['dir']
            sl_native = position['sl_native']
            bars_in = i - position['bar']

            hit_sl = False
            if direction == 1 and row['low'] <= entry - sl_native:
                hit_sl = True
            elif direction == -1 and row['high'] >= entry + sl_native:
                hit_sl = True

            if hit_sl:
                pnl = -(sl_native) * cs['mult'] * vol / spec['point_mult'] - COMMISSION
                trades.append({'pnl': pnl, 'bars': bars_in, 'reason': 'SL'})
                position = None
                cooldown = 6  # cooldown bars
                continue

            # Breakeven after 8 bars
            if bars_in > 8:
                position['sl_native'] = 0

            # Exit on opposite signal after 3 bars
            if bars_in > 3 and row['signal'] == -direction:
                pnl = (row['close'] - entry) * direction * cs['mult'] * vol - COMMISSION
                trades.append({'pnl': pnl, 'bars': bars_in, 'reason': 'SIGNAL'})
                position = None
                cooldown = 3
                continue

        # Entry
        if not position and cooldown == 0 and row['signal'] != 0 and not pd.isna(row['atr']) and row['atr'] > 0:
            # Time filter
            if ts.hour >= 9 and ts.hour < 16:
                direction = int(row['signal'])
                entry = row['close']
                atr = row['atr']
                sl_native = int(atr * sl_mult)
                sl_native = max(spec['min_native'], min(sl_native, spec['max_native']))
                position = {'entry': entry, 'dir': direction,
                           'sl_native': sl_native, 'bar': i, 'atr': atr}

    if position:
        entry = position['entry']
        direction = position['dir']
        last = df.iloc[-1]
        pnl = (last['close'] - entry) * direction * cs['mult'] * vol - COMMISSION
        trades.append({'pnl': pnl, 'bars': len(df) - position['bar'], 'reason': 'END'})

    return trades

# === MAIN ===
symbols = {
    'BIT': {'mt5': 'BITM26', 'tf': 'M5', 'strategy': 'VWAP', 'vol': 2, 'current_sl': 0.2},
    'WDO': {'mt5': 'WDON26', 'tf': 'M5', 'strategy': 'VWAP', 'vol': 2, 'current_sl': 0.8},
    'IND': {'mt5': 'INDM26', 'tf': 'M15', 'strategy': 'BOLLINGER', 'vol': 1, 'current_sl': 0.5},
    'DOL': {'mt5': 'DOLN26', 'tf': 'M5', 'strategy': 'EMA_PULLBACK', 'vol': 1, 'current_sl': 1.2},
    'WSP': {'mt5': 'WSPM26', 'tf': 'M15', 'strategy': 'MACD_MOMENTUM', 'vol': 1, 'current_sl': 1.2},
}

sl_multipliers = [0.3, 0.5, 0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0]

print("=" * 95)
print("  SL SENSITIVITY TEST — Qual SL maximiza lucro por ativo?")
print("=" * 95)
print()

for name, info in symbols.items():
    df = fetch(info['mt5'], info['tf'], 500)
    if len(df) < 50:
        print(f"{name}: dados insuficientes ({len(df)} barras)")
        continue

    atr = calc_atr(df).dropna()
    atr_medio = atr.iloc[-100:].mean()
    dates = df.index.date
    n_days = len(set(dates))

    print(f"{'='*95}")
    print(f"  {name} ({info['tf']}, {info['strategy']}, vol={info['vol']}) — {len(df)} barras | {n_days} dias | ATR={atr_medio:.1f}")
    print(f"  SL atual: {info['current_sl']}x ATR = {info['current_sl']*atr_medio:.0f} pts nativos")
    print(f"{'-'*95}")
    print(f"  {'SL':>5} {'pts':>7} {'R$ loss':>9} {'Trades':>7} {'WR%':>6} {'SL hits':>8} {'PnL':>10} {'R$/dia':>9}")
    print(f"{'-'*95}")

    best_pnl = -999999
    best_sl_mult = 0
    current_pnl = None

    for sl_m in sl_multipliers:
        trades = run_backtest(df, name, info['strategy'], sl_m, info['vol'])

        if not trades:
            print(f"  {sl_m:>5.1f} {sl_m*atr_medio:>7.0f} {'':>9} {0:>7} {0:>6.1f} {0:>8} R${0:>8.2f} {'':>9}")
            continue

        n = len(trades)
        wins = sum(1 for t in trades if t['pnl'] > 0)
        wr = wins / n * 100 if n > 0 else 0
        pnl = sum(t['pnl'] for t in trades)
        sl_hits = sum(1 for t in trades if t['reason'] == 'SL')
        pnl_day = pnl / max(n_days, 1)

        spec = SPECS[name]
        sl_pts = sl_m * atr_medio
        sl_clamped = max(spec['min_native'], min(sl_pts, spec['max_native']))
        sl_r = sl_clamped * CONTRACT[name]['mult'] * info['vol'] / spec['point_mult']

        marker = ""
        if abs(sl_m - info['current_sl']) < 0.01:
            marker = " < ATUAL"
            current_pnl = pnl
        if pnl > best_pnl:
            best_pnl = pnl
            best_sl_mult = sl_m

        print(f"  {sl_m:>5.1f} {sl_pts:>7.0f} R${sl_r:>7.0f} {n:>7} {wr:>5.1f}% {sl_hits:>8} R${pnl:>8.2f} R${pnl_day:>7.2f}{marker}")

    delta = best_pnl - (current_pnl or 0)
    print(f"{'-'*95}")
    print(f"  MELHOR SL: {best_sl_mult:.1f}x | PnL R${best_pnl:.2f} vs Atual R${current_pnl or 0:.2f} | Delta R${delta:+.2f}")
    print()
