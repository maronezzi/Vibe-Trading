import sys, csv, io, subprocess, os
import numpy as np, pandas as pd

WINE_PYTHON = os.path.expanduser("~/.wine/drive_c/Python311/python.exe")
FETCH_SCRIPT = os.path.join(os.path.expanduser("~/Projects/Vibe-Trading"), "mt5_fetch.py")

def fetch(symbol, tf, n_bars):
    cmd = ["wine", WINE_PYTHON, FETCH_SCRIPT, "rates", symbol, tf, str(n_bars)]
    env = {**os.environ, "WINEDEBUG": "-all"}
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    if r.returncode != 0 or not r.stdout.strip():
        return pd.DataFrame()
    reader = csv.reader(io.StringIO(r.stdout.strip()))
    headers = next(reader)
    rows = [x for x in reader if x]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=headers)
    for c in ["open", "high", "low", "close", "tick_volume", "real_volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["time"] = pd.to_datetime(df["time"].astype(int), unit="s")
    df = df.set_index("time")
    return df

def calc_atr(df, period=14):
    h, l = df["high"], df["low"]
    c_prev = df["close"].shift(1)
    tr = pd.concat([h-l, (h-c_prev).abs(), (l-c_prev).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def calc_rsi(df, period=14):
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calc_ema(df, period):
    return df["close"].ewm(span=period, adjust=False).mean()

def calc_macd(df, fast=12, slow=26, signal=9):
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    sig = macd.ewm(span=signal, adjust=False).mean()
    hist = macd - sig
    return macd, sig, hist

def calc_bollinger(df, period=20, num_std=2.0):
    mid = df["close"].rolling(period).mean()
    std = df["close"].rolling(period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower

def calc_adx(df, period=14):
    h, l, c = df["high"], df["low"], df["close"]
    plus_dm = h.diff()
    minus_dm = -l.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)
    tr = pd.concat([h-l, (h-c.shift(1)).abs(), (l-c.shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    plus_di = 100 * (plus_dm.rolling(period).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(period).mean() / atr)
    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di))
    adx = dx.rolling(period).mean()
    return adx, plus_di, minus_di

combos = [
    ("WIN$", "M5"),
    ("WIN$", "M15"),
    ("WDO$", "M5"),
    ("WDO$", "M15"),
]

for sym, tf in combos:
    df = fetch(sym, tf, 500)
    if df.empty:
        print(f"{sym} {tf}: SEM DADOS"); continue
    
    print(f"\n{'='*80}")
    print(f"  ANALISE TECNICA: {sym} {tf}")
    print(f"{'='*80}")
    print(f"  Periodo: {df.index[0].strftime('%d/%m/%Y')} -> {df.index[-1].strftime('%d/%m/%Y')}")
    print(f"  Barras: {len(df)} | Dias: {df['close'].index.normalize().nunique()}")
    print(f"  Preco: {df['close'].iloc[0]:.2f} -> {df['close'].iloc[-1]:.2f} ({(df['close'].iloc[-1]/df['close'].iloc[0]-1)*100:+.2f}%)")
    
    atr = calc_atr(df, 14)
    atr_cur = atr.iloc[-1]
    atr_pct = atr_cur / df['close'].iloc[-1] * 100
    print(f"\n  ATR(14): {atr_cur:.1f} pts ({atr_pct:.2f}% do preco)")
    print(f"     ATR medio: {atr.mean():.1f} | Min: {atr.min():.1f} | Max: {atr.max():.1f}")
    
    rsi = calc_rsi(df, 14)
    rsi_cur = rsi.iloc[-1]
    rsi_zone = "SOBRECOMPRADO" if rsi_cur > 70 else "SOBREVENDIDO" if rsi_cur < 30 else "NEUTRO"
    print(f"\n  RSI(14): {rsi_cur:.1f} -- {rsi_zone}")
    
    ema9 = calc_ema(df, 9)
    ema21 = calc_ema(df, 21)
    ema_diff = (ema9.iloc[-1] - ema21.iloc[-1]) / df['close'].iloc[-1] * 100
    trend = "UP" if ema9.iloc[-1] > ema21.iloc[-1] else "DOWN"
    print(f"\n  EMA(9): {ema9.iloc[-1]:.1f} | EMA(21): {ema21.iloc[-1]:.1f}")
    print(f"     Diferenca: {ema_diff:+.3f}% -- Tendencia: {trend}")
    
    macd_line, macd_sig, macd_hist = calc_macd(df)
    macd_dir = "BULLISH" if macd_hist.iloc[-1] > 0 else "BEARISH"
    print(f"\n  MACD: {macd_line.iloc[-1]:.1f} | Signal: {macd_sig.iloc[-1]:.1f} | Hist: {macd_hist.iloc[-1]:.1f}")
    print(f"     Direcao: {macd_dir}")
    
    bb_upper, bb_mid, bb_lower = calc_bollinger(df, 20, 2.0)
    bb_width = (bb_upper.iloc[-1] - bb_lower.iloc[-1]) / bb_mid.iloc[-1] * 100
    bb_pos = (df['close'].iloc[-1] - bb_lower.iloc[-1]) / (bb_upper.iloc[-1] - bb_lower.iloc[-1]) * 100
    print(f"\n  Bollinger(20,2): Upper={bb_upper.iloc[-1]:.1f} Mid={bb_mid.iloc[-1]:.1f} Lower={bb_lower.iloc[-1]:.1f}")
    print(f"     Largura: {bb_width:.2f}% | Posicao: {bb_pos:.0f}%")
    
    adx_val, plus_di, minus_di = calc_adx(df, 14)
    adx_cur = adx_val.iloc[-1]
    adx_str = "FORTE" if adx_cur > 25 else "MODERADA" if adx_cur > 20 else "FRACA"
    print(f"\n  ADX(14): {adx_cur:.1f} -- Tendencia {adx_str}")
    print(f"     +DI: {plus_di.iloc[-1]:.1f} | -DI: {minus_di.iloc[-1]:.1f}")
    
    vol_avg = df['tick_volume'].rolling(20).mean().iloc[-1]
    vol_cur = df['tick_volume'].iloc[-1]
    vol_ratio = vol_cur / vol_avg if vol_avg > 0 else 1
    print(f"\n  Volume: atual={vol_cur:.0f} media20={vol_avg:.0f} ratio={vol_ratio:.2f}x")
    
    if adx_cur > 25 and ema9.iloc[-1] > ema21.iloc[-1]:
        regime = "TRENDING UP"
    elif adx_cur > 25 and ema9.iloc[-1] < ema21.iloc[-1]:
        regime = "TRENDING DOWN"
    else:
        regime = "CHOPPY/RANGE"
    print(f"\n  Regime: {regime}")
