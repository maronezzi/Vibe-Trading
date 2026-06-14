#!/usr/bin/env python3
"""
Otimizador completo: encontra a MELHOR estratégia + SL + params
para cada ativo em cada timeframe, de forma que TODOS sejam positivos.
"""
import subprocess, csv, io, os, json, itertools
import pandas as pd
import numpy as np
from datetime import datetime

WINE_PYTHON = os.path.expanduser('~/.wine/drive_c/Python311/python.exe')
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FETCH_SCRIPT = os.path.join(SCRIPT_DIR, 'mt5_fetch.py')

# ─── Fetch ────────────────────────────────────────────────────────────────────

def fetch(symbol, tf, n_bars=500):
    cmd = ['wine', WINE_PYTHON, FETCH_SCRIPT, 'rates', symbol, tf, str(n_bars)]
    env = {**os.environ, 'WINEDEBUG': '-all'}
    try:
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
    except:
        return pd.DataFrame()

# ─── Indicators ───────────────────────────────────────────────────────────────

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

def calc_adx(df, period=14):
    h, l, c = df['high'], df['low'], df['close']
    plus_dm = (h.diff()).where((h.diff() > -l.diff()) & (h.diff() > 0), 0)
    minus_dm = (-l.diff()).where((-l.diff() > h.diff()) & (-l.diff() > 0), 0)
    tr = pd.concat([h-l, (h-c.shift(1)).abs(), (l-c.shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    plus_di = 100 * (plus_dm.rolling(period).mean() / atr.replace(0, np.nan))
    minus_di = 100 * (minus_dm.rolling(period).mean() / atr.replace(0, np.nan))
    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    return dx.rolling(period).mean()

# ─── Contract specs ───────────────────────────────────────────────────────────

CONTRACT = {
    'WIN': {'mult': 0.20, 'point': 1},
    'BIT': {'mult': 0.50, 'point': 100},
    'WDO': {'mult': 10.0, 'point': 1000},
    'DOL': {'mult': 50.0, 'point': 1000},
    'IND': {'mult': 1.0,  'point': 1},
    'WSP': {'mult': 2.5,  'point': 100},
}

SPECS = {
    'WIN': {'min': 150, 'max': 800, 'pm': 1},
    'BIT': {'min': 30,  'max': 500, 'pm': 100},
    'WDO': {'min': 3,   'max': 12,  'pm': 1000},
    'DOL': {'min': 3,   'max': 200, 'pm': 1000},
    'IND': {'min': 150, 'max': 350, 'pm': 1},
    'WSP': {'min': 5,   'max': 200, 'pm': 100},
}

COMMISSION = 1.2
CLOSE_H, CLOSE_M = 16, 45

# ─── Signal generators ────────────────────────────────────────────────────────

def gen_signals(df, strategy, params):
    """Retorna array de sinais: 1=compra, -1=venda, 0=neutro"""
    signals = np.zeros(len(df))

    if strategy == 'VWAP':
        vw = calc_vwap(df, params.get('vwap_period', 20))
        buy_th = params.get('vwap_buy_threshold', 1.005)
        sell_th = params.get('vwap_sell_threshold', 0.995)
        signals = np.where(df['close'].values > vw.values * buy_th, 1,
                np.where(df['close'].values < vw.values * sell_th, -1, 0))

    elif strategy == 'BOLLINGER':
        p = params.get('bb_period', 20)
        s = params.get('bb_std', 2.0)
        rsi_p = params.get('rsi_period', 14)
        rsi_ob = params.get('rsi_overbought', 70)
        rsi_os = params.get('rsi_oversold', 30)
        upper, mid, lower = calc_bollinger(df, p, s)
        rsi = calc_rsi(df['close'], rsi_p)
        signals = np.where((df['close'].values < lower.values) & (rsi.values < rsi_os), 1,
                np.where((df['close'].values > upper.values) & (rsi.values > rsi_ob), -1, 0))

    elif strategy == 'EMA_PULLBACK':
        ef = calc_ema(df['close'], params.get('ema_fast', 9))
        es = calc_ema(df['close'], params.get('ema_slow', 21))
        adx = calc_adx(df, params.get('adx_period', 14))
        adx_th = params.get('adx_threshold', 20)
        pb = params.get('pullback_pct', 0.1)
        uptrend = (ef > es) & (adx > adx_th)
        uptrend_pullback = uptrend & (df['close'] < ef * (1 + pb))
        downtrend = (ef < es) & (adx > adx_th)
        downtrend_pullback = downtrend & (df['close'] > ef * (1 - pb))
        signals = np.where(uptrend_pullback.values, 1,
                np.where(downtrend_pullback.values, -1, 0))

    elif strategy == 'MACD_MOMENTUM':
        ef = calc_ema(df['close'], params.get('macd_fast', 9))
        es = calc_ema(df['close'], params.get('macd_slow', 21))
        macd = ef - es
        signal_line = calc_ema(macd, params.get('macd_signal', 12))
        hist = macd - signal_line
        signals = np.where((hist > 0) & (hist.shift(1).fillna(0) <= 0), 1,
                np.where((hist < 0) & (hist.shift(1).fillna(0) >= 0), -1, 0))

    elif strategy == 'STRONG_TREND':
        ema20 = calc_ema(df['close'], 20)
        ema50 = calc_ema(df['close'], 50)
        adx = calc_adx(df, 14)
        strong_up = (ema20 > ema50) & (df['close'] > ema20) & (adx > 25)
        strong_dn = (ema20 < ema50) & (df['close'] < ema20) & (adx > 25)
        signals = np.where(strong_up.values, 1,
                np.where(strong_dn.values, -1, 0))

    elif strategy == 'RSI_REVERSION':
        p = params.get('rsi_period', 14)
        ob = params.get('rsi_overbought', 70)
        os_ = params.get('rsi_oversold', 30)
        rsi = calc_rsi(df['close'], p)
        signals = np.where(rsi.values < os_, 1,
                np.where(rsi.values > ob, -1, 0))

    elif strategy == 'BREAKOUT':
        p = params.get('breakout_period', 20)
        hh = df['high'].rolling(p).max().shift(1)
        ll = df['low'].rolling(p).min().shift(1)
        signals = np.where(df['close'].values > hh.values, 1,
                np.where(df['close'].values < ll.values, -1, 0))

    return signals

# ─── Backtest engine ──────────────────────────────────────────────────────────

def run_backtest(df, signals, name, sl_mult, vol=1, trail_mult=1.0):
    cs = CONTRACT[name]
    spec = SPECS[name]
    df = df.copy()
    df['atr'] = calc_atr(df)

    trades = []
    position = None
    cooldown = 0

    for i in range(len(df)):
        row = df.iloc[i]
        ts = df.index[i]

        if cooldown > 0:
            cooldown -= 1

        # EOD close
        if position and ts.hour >= CLOSE_H and ts.minute >= CLOSE_M:
            entry = position['entry']
            pnl = (row['close'] - entry) * position['dir'] * cs['mult'] * vol - COMMISSION
            trades.append({'pnl': pnl, 'bars': i - position['bar'], 'reason': 'EOD'})
            position = None
            continue

        # Hard exit at 45 min (≈9 bars M5, 3 bars M15)
        if position:
            bars_in = i - position['bar']
            hard_exit = 45 // max(1, int(df.index.to_series().diff().dropna().median().total_seconds() / 60)) if len(df) > 1 else 9
            if bars_in >= hard_exit:
                entry = position['entry']
                pnl = (row['close'] - entry) * position['dir'] * cs['mult'] * vol - COMMISSION
                trades.append({'pnl': pnl, 'bars': bars_in, 'reason': 'HARD_EXIT'})
                position = None
                cooldown = 3
                continue

        # SL check
        if position:
            entry = position['entry']
            direction = position['dir']
            sl_native = position['sl_native']
            bars_in = i - position['bar']

            hit = False
            if direction == 1 and row['low'] <= entry - sl_native:
                hit = True
            elif direction == -1 and row['high'] >= entry + sl_native:
                hit = True

            if hit:
                pnl = -(sl_native) * cs['mult'] * vol / spec['pm'] - COMMISSION
                trades.append({'pnl': pnl, 'bars': bars_in, 'reason': 'SL'})
                position = None
                cooldown = 6
                continue

            # Breakeven after 8 bars
            if bars_in > 8:
                position['sl_native'] = 0

            # Exit on opposite signal after 3 bars
            if bars_in > 3 and signals[i] == -direction:
                pnl = (row['close'] - entry) * direction * cs['mult'] * vol - COMMISSION
                trades.append({'pnl': pnl, 'bars': bars_in, 'reason': 'SIGNAL'})
                position = None
                cooldown = 3
                continue

        # Entry
        if not position and cooldown == 0 and signals[i] != 0 and not pd.isna(row['atr']) and row['atr'] > 0:
            if ts.hour >= 9 and ts.hour < 16:
                direction = int(signals[i])
                entry = row['close']
                atr = row['atr']
                sl_native = int(atr * sl_mult)
                sl_native = max(spec['min'], min(sl_native, spec['max']))
                position = {'entry': entry, 'dir': direction,
                           'sl_native': sl_native, 'bar': i, 'atr': atr}

    if position:
        entry = position['entry']
        last = df.iloc[-1]
        pnl = (last['close'] - entry) * position['dir'] * cs['mult'] * vol - COMMISSION
        trades.append({'pnl': pnl, 'bars': len(df) - position['bar'], 'reason': 'END'})

    return trades

# ─── Strategy param grid ─────────────────────────────────────────────────────

STRATEGIES = ['VWAP', 'BOLLINGER', 'EMA_PULLBACK', 'MACD_MOMENTUM', 'RSI_REVERSION', 'BREAKOUT', 'STRONG_TREND']

PARAM_GRID = {
    'VWAP': [
        {'vwap_period': 10, 'vwap_buy_threshold': 1.003, 'vwap_sell_threshold': 0.997},
        {'vwap_period': 15, 'vwap_buy_threshold': 1.005, 'vwap_sell_threshold': 0.995},
        {'vwap_period': 20, 'vwap_buy_threshold': 1.005, 'vwap_sell_threshold': 0.995},
        {'vwap_period': 20, 'vwap_buy_threshold': 1.01, 'vwap_sell_threshold': 0.99},
        {'vwap_period': 30, 'vwap_buy_threshold': 1.005, 'vwap_sell_threshold': 0.995},
        {'vwap_period': 30, 'vwap_buy_threshold': 1.01, 'vwap_sell_threshold': 0.99},
        {'vwap_period': 30, 'vwap_buy_threshold': 1.02, 'vwap_sell_threshold': 0.98},
        {'vwap_period': 50, 'vwap_buy_threshold': 1.005, 'vwap_sell_threshold': 0.995},
        {'vwap_period': 50, 'vwap_buy_threshold': 1.01, 'vwap_sell_threshold': 0.99},
    ],
    'BOLLINGER': [
        {'bb_period': 15, 'bb_std': 2.0, 'rsi_period': 14, 'rsi_overbought': 70, 'rsi_oversold': 30},
        {'bb_period': 20, 'bb_std': 2.0, 'rsi_period': 14, 'rsi_overbought': 70, 'rsi_oversold': 30},
        {'bb_period': 20, 'bb_std': 2.5, 'rsi_period': 14, 'rsi_overbought': 75, 'rsi_oversold': 25},
        {'bb_period': 20, 'bb_std': 3.0, 'rsi_period': 14, 'rsi_overbought': 80, 'rsi_oversold': 20},
        {'bb_period': 25, 'bb_std': 2.0, 'rsi_period': 14, 'rsi_overbought': 70, 'rsi_oversold': 30},
        {'bb_period': 25, 'bb_std': 2.5, 'rsi_period': 14, 'rsi_overbought': 75, 'rsi_oversold': 25},
        {'bb_period': 30, 'bb_std': 2.0, 'rsi_period': 14, 'rsi_overbought': 75, 'rsi_oversold': 25},
        {'bb_period': 30, 'bb_std': 2.5, 'rsi_period': 14, 'rsi_overbought': 80, 'rsi_oversold': 20},
        {'bb_period': 10, 'bb_std': 1.5, 'rsi_period': 7, 'rsi_overbought': 75, 'rsi_oversold': 25},
    ],
    'EMA_PULLBACK': [
        {'ema_fast': 5, 'ema_slow': 13, 'adx_period': 14, 'adx_threshold': 15, 'pullback_pct': 0.05},
        {'ema_fast': 5, 'ema_slow': 13, 'adx_period': 14, 'adx_threshold': 20, 'pullback_pct': 0.1},
        {'ema_fast': 9, 'ema_slow': 21, 'adx_period': 14, 'adx_threshold': 18, 'pullback_pct': 0.1},
        {'ema_fast': 9, 'ema_slow': 21, 'adx_period': 14, 'adx_threshold': 25, 'pullback_pct': 0.15},
        {'ema_fast': 9, 'ema_slow': 21, 'adx_period': 14, 'adx_threshold': 30, 'pullback_pct': 0.2},
        {'ema_fast': 12, 'ema_slow': 26, 'adx_period': 14, 'adx_threshold': 20, 'pullback_pct': 0.1},
        {'ema_fast': 12, 'ema_slow': 26, 'adx_period': 14, 'adx_threshold': 25, 'pullback_pct': 0.15},
    ],
    'MACD_MOMENTUM': [
        {'macd_fast': 5, 'macd_slow': 13, 'macd_signal': 5},
        {'macd_fast': 5, 'macd_slow': 13, 'macd_signal': 8},
        {'macd_fast': 9, 'macd_slow': 21, 'macd_signal': 12},
        {'macd_fast': 12, 'macd_slow': 26, 'macd_signal': 9},
        {'macd_fast': 12, 'macd_slow': 26, 'macd_signal': 12},
        {'macd_fast': 8, 'macd_slow': 17, 'macd_signal': 9},
    ],
    'RSI_REVERSION': [
        {'rsi_period': 7, 'rsi_overbought': 75, 'rsi_oversold': 25},
        {'rsi_period': 7, 'rsi_overbought': 80, 'rsi_oversold': 20},
        {'rsi_period': 14, 'rsi_overbought': 70, 'rsi_oversold': 30},
        {'rsi_period': 14, 'rsi_overbought': 75, 'rsi_oversold': 25},
        {'rsi_period': 14, 'rsi_overbought': 80, 'rsi_oversold': 20},
        {'rsi_period': 21, 'rsi_overbought': 75, 'rsi_oversold': 25},
        {'rsi_period': 21, 'rsi_overbought': 80, 'rsi_oversold': 20},
    ],
    'BREAKOUT': [
        {'breakout_period': 10},
        {'breakout_period': 15},
        {'breakout_period': 20},
        {'breakout_period': 25},
        {'breakout_period': 30},
        {'breakout_period': 40},
        {'breakout_period': 50},
    ],
    'STRONG_TREND': [
        {},  # defaults only
    ],
}

SL_GRID = [0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0]

# ─── Main optimizer ──────────────────────────────────────────────────────────

SYMBOLS = {
    'WIN': {'mt5': 'WINM26', 'vol': 1},
    'BIT': {'mt5': 'BITM26', 'vol': 2},
    'DOL': {'mt5': 'DOLN26', 'vol': 1},
    'IND': {'mt5': 'INDM26', 'vol': 1},
    'WSP': {'mt5': 'WSPM26', 'vol': 1},
    'WDO': {'mt5': 'WDON26', 'vol': 2},
}

TIMEFRAMES = {'M5': 500, 'M15': 500, 'M30': 300, 'H1': 200}

def optimize():
    results = {}

    for sym_name, sym_info in SYMBOLS.items():
        for tf, n_bars in TIMEFRAMES.items():
            key = f"{sym_name}_{tf}"
            print(f"\n{'='*80}")
            print(f"  Otimizando {key}...")
            print(f"{'='*80}")

            df = fetch(sym_info['mt5'], tf, n_bars)
            if len(df) < 50:
                print(f"  ⚠️ Dados insuficientes ({len(df)} barras) — pulando")
                continue

            atr = calc_atr(df).dropna()
            atr_medio = atr.iloc[-100:].mean() if len(atr) >= 100 else atr.mean()
            dates = df.index.date
            n_days = len(set(dates))

            print(f"  {len(df)} barras | {n_days} dias | ATR={atr_medio:.1f}")

            best = {'pnl': -999999, 'strategy': '', 'params': {}, 'sl': 0, 'trades': []}

            tested = 0
            for strategy in STRATEGIES:
                for params in PARAM_GRID[strategy]:
                    for sl_m in SL_GRID:
                        try:
                            signals = gen_signals(df, strategy, params)
                            trades = run_backtest(df, signals, sym_name, sl_m, sym_info['vol'])

                            if not trades:
                                continue

                            n = len(trades)
                            pnl = sum(t['pnl'] for t in trades)

                            tested += 1

                            if pnl > best['pnl']:
                                wins = sum(1 for t in trades if t['pnl'] > 0)
                                wr = wins / n * 100
                                best = {
                                    'pnl': pnl, 'strategy': strategy, 'params': params,
                                    'sl': sl_m, 'trades': trades, 'n': n, 'wr': wr,
                                    'pnl_day': pnl / max(n_days, 1),
                                    'atr': atr_medio, 'days': n_days
                                }
                        except Exception as e:
                            pass

            if best['pnl'] > -999999:
                status = "✅ POSITIVO" if best['pnl'] > 0 else "❌ NEGATIVO"
                print(f"\n  🏆 MELHOR: {best['strategy']} | SL={best['sl']:.1f}x | "
                      f"{best['n']} trades | WR={best['wr']:.1f}% | "
                      f"PnL=R${best['pnl']:.2f} | R${best['pnl_day']:.2f}/dia {status}")
                print(f"  Params: {json.dumps(best['params'], ensure_ascii=False)}")
                print(f"  Testadas: {tested} combinações")

                results[key] = best
            else:
                print(f"  ❌ Nenhuma combinação lucrativa encontrada em {tested} testes")

    # ─── Summary ─────────────────────────────────────────────────────────────
    print(f"\n\n{'#'*80}")
    print(f"  RESUMO FINAL — TODOS OS ATIVOS × TIMEFRAMES")
    print(f"{'#'*80}\n")
    print(f"  {'Ativo_TF':<12} {'Estratégia':<16} {'SL':>5} {'Trades':>7} {'WR%':>6} {'PnL':>10} {'R$/dia':>9} {'Status':>10}")
    print(f"  {'─'*80}")

    total_pnl = 0
    total_day = 0
    n_positive = 0
    n_negative = 0

    for key in sorted(results.keys()):
        r = results[key]
        total_pnl += r['pnl']
        total_day += r['pnl_day']
        if r['pnl'] > 0:
            n_positive += 1
            status = "✅"
        else:
            n_negative += 1
            status = "❌"
        print(f"  {key:<12} {r['strategy']:<16} {r['sl']:>4.1f}x {r['n']:>7} {r['wr']:>5.1f}% "
              f"R${r['pnl']:>8.2f} R${r['pnl_day']:>7.2f} {status:>10}")

    print(f"  {'─'*80}")
    print(f"  {'TOTAL':<12} {'':<16} {'':>5} {'':>7} {'':>6} R${total_pnl:>8.2f} R${total_day:>7.2f}")
    print(f"\n  ✅ Positivos: {n_positive} | ❌ Negativos: {n_negative}")
    print(f"  PnL total: R$ {total_pnl:.2f} | R$ {total_day:.2f}/dia")

    # Save results for config generation
    output = {}
    for key, r in results.items():
        output[key] = {
            'strategy': r['strategy'],
            'sl_atr_mult': r['sl'],
            'params': r['params'],
            'pnl': r['pnl'],
            'pnl_day': r['pnl_day'],
            'n_trades': r['n'],
            'wr': r['wr'],
            'days': r['days'],
            'atr': r['atr'],
            'positive': r['pnl'] > 0,
        }
    with open('/tmp/vt_optimization_results.json', 'w') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n  Resultados salvos: /tmp/vt_optimization_results.json")
    return results

if __name__ == '__main__':
    optimize()
