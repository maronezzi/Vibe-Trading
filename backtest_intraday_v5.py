"""
Backtest intraday v5 — saídas inteligentes + filtro ADX + SIG removido.

Mudanças vs v4:
1. SAÍDA POR SINAL ELIMINADA — trades saem só por:
   - Trailing Stop (100% lucrativo quando ativa)
   - Profit Lock / SL fixo (proteção)
   - 16:45 (fecha intraday)

Mantido:
2. Filtro ADX: SMA/EMA só entram se ADX(14) > 25 (tendência forte)
3. Trailing: gatilho 1.5x ATR (menos saídas prematuras)
4. Profit Lock: só RSI/BB/VWAP (SMA/EMA sem)
5. Time-based: trailing aperta (0.3x ATR) após 20 barras

Config:
  SMA/EMA → ADX>25 + SL fixo (1.5x ATR) + Trailing (1.5x ATR) + sem profit lock
  RSI/BB/VWAP → só Trailing (1.5x ATR) + Profit lock (0.5x ATR)
Fecha tudo às 16:45 BRT.

Uso: PYTHONPATH=./agent ./agent/venv/bin/python backtest_intraday_v5.py
"""

import sys, csv, io, subprocess, os
from pathlib import Path
import numpy as np, pandas as pd

WINE_PYTHON = os.path.expanduser("~/.wine/drive_c/Python311/python.exe")
FETCH_SCRIPT = os.path.join(os.path.dirname(__file__), "mt5_fetch.py")

CLOSE_HOUR, CLOSE_MINUTE = 16, 45

CONTRACT_SPECS = {
    "WIN$": {"mult": 0.20, "name": "Mini Índice", "margin": 5000, "tick": 5, "slip_r": 1.0},
    "WDO$": {"mult": 10.0, "name": "Mini Dólar", "margin": 3000, "tick": 0.5, "slip_r": 5.0},
}
COMMISSION = 2.5


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
    df["hour"] = df.index.hour
    df["minute"] = df.index.minute
    df["date"] = df.index.date
    return df[["open", "high", "low", "close", "tick_volume", "real_volume", "hour", "minute", "date"]].dropna(subset=["close"])


def calc_atr(df, period=14):
    h, l = df["high"], df["low"]
    c_prev = df["close"].shift(1)
    tr = pd.concat([h - l, (h - c_prev).abs(), (l - c_prev).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def calc_adx(df, period=14):
    """ADX (Average Directional Index) — mede força da tendência."""
    h, l, c = df["high"], df["low"], df["close"]
    c_prev = c.shift(1)
    plus_dm = h.diff()
    minus_dm = -l.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)
    atr = pd.concat([h - l, (h - c_prev).abs(), (l - c_prev).abs()], axis=1).max(axis=1).rolling(period).mean()
    plus_di = 100 * (plus_dm.rolling(period).mean() / atr.replace(0, np.nan))
    minus_di = 100 * (minus_dm.rolling(period).mean() / atr.replace(0, np.nan))
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.rolling(period).mean().fillna(0)
    return adx


# ===== ESTRATÉGIAS =====

def sma_cross(df):
    f, s = df["close"].rolling(9).mean(), df["close"].rolling(21).mean()
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[f > s] = 1; sig[f < s] = -1
    return sig.shift(1).fillna(0)

def rsi_reversal(df):
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).ewm(span=7).mean()
    loss = -delta.where(delta < 0, 0).ewm(span=7).mean()
    rsi = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[rsi < 25] = 1; sig[rsi > 75] = -1
    return sig.shift(1).fillna(0)

def bollinger_bounce(df):
    sma = df["close"].rolling(20).mean()
    sd = df["close"].rolling(20).std()
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[df["close"] < sma - 2 * sd] = 1; sig[df["close"] > sma + 2 * sd] = -1
    return sig.shift(1).fillna(0)

def ema_trend(df):
    f, s = df["close"].ewm(span=10).mean(), df["close"].ewm(span=30).mean()
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[f > s] = 1; sig[f < s] = -1
    return sig.shift(1).fillna(0)

def vwap_trend(df):
    typical = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["tick_volume"].replace(0, 1)
    vwap = (typical * vol).rolling(20).sum() / vol.rolling(20).sum()
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[df["close"] > vwap * 1.003] = 1; sig[df["close"] < vwap * 0.997] = -1
    return sig.shift(1).fillna(0)

STRATEGIES = {"SMA(9,21)": sma_cross, "RSI(7)": rsi_reversal, "Bollinger": bollinger_bounce, "EMA(10,30)": ema_trend, "VWAP": vwap_trend}
SL_CONFIG = {"SMA(9,21)": True, "EMA(10,30)": True, "RSI(7)": False, "Bollinger": False, "VWAP": False}
ADX_FILTER = {"SMA(9,21)": True, "EMA(10,30)": True, "RSI(7)": False, "Bollinger": False, "VWAP": False}
ADX_THRESHOLD = 25  # só entra se ADX > 25 (tendência forte)
PROFIT_LOCK_CONFIG = {"SMA(9,21)": False, "EMA(10,30)": False, "RSI(7)": True, "Bollinger": True, "VWAP": True}
TRAIL_ACTIVATE = 1.5  # 1.5x ATR para ativar trailing (era 1x)


def backtest(df, signals, symbol, *, capital=100_000.0, max_ct=3, use_sl=True,
            use_adx_filter=False, use_profit_lock=True, trail_activate=1.5, adx_series=None):
    """
    v4 — saídas inteligentes + filtro ADX:
    - ADX filter: só entra se ADX > 25 (quando use_adx_filter=True)
    - Exit por sinal: só se trailing já ativo OU 2 barras consecutivas neutras
    - Time-based: após 20 barras, trailing aperta pra 0.3x ATR
    - Profit lock: após 0.5x ATR de lucro, SL vai pro breakeven (quando use_profit_lock=True)
    - Trailing ativa em trail_activate * ATR de lucro (padrão 1.5x)
    """
    spec = CONTRACT_SPECS[symbol]
    mult, margin, slip_r = spec["mult"], spec["margin"], spec["slip_r"]
    atr = calc_atr(df, 14)
    if adx_series is None:
        adx_series = calc_adx(df, 14)

    cash = capital
    pos = 0
    ep = 0.0           # entry price
    e_date = None
    e_atr = 0.0        # ATR na entrada
    best = 0.0         # melhor preço desde entrada
    sl_price = 0.0     # nível do stop
    trail_on = False
    breakeven_on = False  # profit lock
    bars_in_trade = 0  # barras desde entrada
    neutral_count = 0  # barras consecutivas de sinal neutro/oposto

    equity, trade_log, daily_pnl = [], [], []
    n_trades = n_wins = n_long = n_short = 0
    n_sl = n_trail = n_sig = n_close = n_be = n_time = n_filtered = 0
    gross_win = 0.0
    gross_loss_val = 0.0

    def _close(price, reason):
        nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, breakeven_on, e_atr
        nonlocal n_trades, n_wins, n_long, n_short, n_sl, n_trail, n_sig, n_close, n_be, n_time
        nonlocal gross_win, gross_loss_val, bars_in_trade, neutral_count

        if pos == 0:
            return

        sl_cost = slip_r * max_ct
        comm = COMMISSION * max_ct

        if pos == 1:
            pnl = (price - ep) * mult * max_ct - sl_cost - comm
            n_long += 1
        else:
            pnl = (ep - price) * mult * max_ct - sl_cost - comm
            n_short += 1

        cash += margin * max_ct + pnl
        n_trades += 1

        if reason == "SL": n_sl += 1
        elif reason == "TRAIL": n_trail += 1
        elif reason == "SIG": n_sig += 1
        elif reason == "1645": n_close += 1
        elif reason == "BE": n_be += 1
        elif reason == "TIME": n_time += 1

        if pnl > 0:
            n_wins += 1; gross_win += pnl
        else:
            gross_loss_val += abs(pnl)

        trade_log.append({
            "type": "LONG" if pos == 1 else "SHORT",
            "entry": str(e_date), "exit": "",
            "ep": ep, "xp": price, "pnl": pnl, "reason": reason,
            "bars": bars_in_trade,
        })
        daily_pnl.append(pnl)
        pos = 0; ep = 0; best = 0; sl_price = 0; trail_on = False
        breakeven_on = False; bars_in_trade = 0; neutral_count = 0

    def _open_long(price, date, cur_atr):
        nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, breakeven_on, e_atr, bars_in_trade, neutral_count
        sl_dist = 1.5 * cur_atr if use_sl else 0
        cost = slip_r * max_ct + COMMISSION * max_ct
        if cash >= margin * max_ct + cost:
            cash -= margin * max_ct + cost
            pos = 1; ep = price; e_date = date; e_atr = cur_atr
            best = price; sl_price = price - sl_dist; trail_on = False
            breakeven_on = False; bars_in_trade = 0; neutral_count = 0
            return True
        return False

    def _open_short(price, date, cur_atr):
        nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, breakeven_on, e_atr, bars_in_trade, neutral_count
        sl_dist = 1.5 * cur_atr if use_sl else 0
        cost = slip_r * max_ct + COMMISSION * max_ct
        if cash >= margin * max_ct + cost:
            cash -= margin * max_ct + cost
            pos = -1; ep = price; e_date = date; e_atr = cur_atr
            best = price; sl_price = price + sl_dist; trail_on = False
            breakeven_on = False; bars_in_trade = 0; neutral_count = 0
            return True
        return False

    def _is_exit_signal(target):
        """Verifica se o sinal atual pede saída."""
        if pos == 1 and target <= 0:
            return True
        if pos == -1 and target >= 0:
            return True
        return False

    for i, (date, row) in enumerate(df.iterrows()):
        price = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        hour = int(row["hour"])
        minute = int(row["minute"])
        target = int(signals.loc[date])
        cur_atr = float(atr.loc[date]) if i > 0 and not pd.isna(atr.iloc[i]) else 0

        # Mark-to-market
        if pos == 1:
            eq_val = cash + (price - ep) * mult * max_ct
        elif pos == -1:
            eq_val = cash + (ep - price) * mult * max_ct
        else:
            eq_val = cash
        equity.append(eq_val)

        # ===== SEM POSIÇÃO: ENTRADA =====
        if pos == 0:
            if cur_atr > 0:
                cur_adx = float(adx_series.iloc[i]) if i < len(adx_series) and not pd.isna(adx_series.iloc[i]) else 0
                # Filtro ADX: só entra se tendência forte (quando ativo)
                if use_adx_filter and cur_adx < ADX_THRESHOLD:
                    n_filtered += 1
                    continue
                if target == 1:
                    _open_long(price, date, cur_atr)
                elif target == -1:
                    _open_short(price, date, cur_atr)
            continue

        # ===== POSIÇÃO ABERTA =====
        bars_in_trade += 1

        # Atualiza melhor preço
        if pos == 1:
            best = max(best, high)
        elif pos == -1:
            best = min(best, low) if best > 0 else low

        # Lucro atual
        if pos == 1:
            cur_profit_pts = best - ep
        else:
            cur_profit_pts = ep - best

        # === OTIMIZAÇÃO 1: PROFIT LOCK (breakeven após 0.5x ATR) — só se habilitado ===
        if use_profit_lock and e_atr > 0 and cur_profit_pts >= 0.5 * e_atr and not breakeven_on:
            breakeven_on = True
            # Move SL pro breakeven (+ custo de entrada)
            entry_cost = slip_r * max_ct + COMMISSION * max_ct
            be_price = ep + (entry_cost / (mult * max_ct)) if pos == 1 else ep - (entry_cost / (mult * max_ct))
            if pos == 1 and (sl_price < be_price or sl_price == 0):
                sl_price = be_price
            elif pos == -1 and (sl_price > be_price or sl_price == 0):
                sl_price = be_price

        # === OTIMIZAÇÃO 3: TIME-BASED — após 20 barras, trailing aperta ===
        time_trail_mult = 0.5  # default
        if bars_in_trade >= 20:
            time_trail_mult = 0.3  # mais apertado depois de 20 barras

        # Ativa trailing (trail_activate * ATR de lucro)
        if not trail_on and e_atr > 0:
            activate = trail_activate * e_atr
            if cur_profit_pts >= activate:
                trail_on = True

        # === STOP LOSS fixo (só se use_sl=True) ===
        if use_sl and sl_price > 0:
            if pos == 1 and low <= sl_price:
                reason = "BE" if breakeven_on else "SL"
                _close(sl_price, reason); continue
            elif pos == -1 and high >= sl_price:
                reason = "BE" if breakeven_on else "SL"
                _close(sl_price, reason); continue

        # === TRAILING STOP (com time-based adjustment) ===
        if trail_on and e_atr > 0:
            trail_dist = time_trail_mult * e_atr
            if pos == 1:
                trail_level = best - trail_dist
                if trail_level > sl_price or sl_price == 0:
                    sl_price = trail_level
                if low <= trail_level:
                    _close(trail_level, "TRAIL"); continue
            elif pos == -1:
                trail_level = best + trail_dist
                if trail_level < sl_price or sl_price == 0:
                    sl_price = trail_level
                if high >= trail_level:
                    _close(trail_level, "TRAIL"); continue

        # === 16:45 → fecha tudo ===
        if hour > CLOSE_HOUR or (hour == CLOSE_HOUR and minute >= CLOSE_MINUTE):
            _close(price, "1645"); continue

        # === SIG ELIMINADO: trades saem só por TRAIL / BE / SL / 16:45 ===
        # (v5 — sinal de saída causou perdas; removido)

    # Força fechar posição restante
    if pos != 0:
        _close(float(df["close"].iloc[-1]), "FORCE")

    # Preenche "exit" nos trade logs
    trade_idx = 0
    for i, (date, _) in enumerate(df.iterrows()):
        if trade_idx >= len(trade_log):
            break
        if trade_log[trade_idx]["reason"] != "FORCE":
            trade_log[trade_idx]["exit"] = str(date)

    # STATS
    eq = pd.Series(equity, index=df.index[:len(equity)])
    total_ret = (cash - capital) / capital * 100
    n_days = df["date"].nunique()
    avg_daily = sum(t["pnl"] for t in trade_log) / n_days if n_days else 0
    pf = gross_win / gross_loss_val if gross_loss_val > 0 else (999 if gross_win > 0 else 0)
    wr = (n_wins / n_trades * 100) if n_trades else 0

    if len(daily_pnl) > 1:
        sharpe = np.mean(daily_pnl) / np.std(daily_pnl) * np.sqrt(252) if np.std(daily_pnl) > 0 else 0
    else:
        sharpe = 0

    dd = (eq - eq.cummax()) / eq.cummax()
    max_dd = dd.min() * 100

    wins = [t["pnl"] for t in trade_log if t["pnl"] > 0]
    losses_p = [t["pnl"] for t in trade_log if t["pnl"] <= 0]
    avg_win = np.mean(wins) if wins else 0
    avg_loss = abs(np.mean(losses_p)) if losses_p else 1
    payoff = avg_win / avg_loss if avg_loss > 0 else 0

    pnl_by_reason = {}
    for t in trade_log:
        r = t["reason"]
        if r not in pnl_by_reason:
            pnl_by_reason[r] = {"count": 0, "pnl": 0, "wins": 0}
        pnl_by_reason[r]["count"] += 1
        pnl_by_reason[r]["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            pnl_by_reason[r]["wins"] += 1

    avg_bars = np.mean([t["bars"] for t in trade_log]) if trade_log else 0

    return {
        "ok": True, "trades": n_trades, "wins": n_wins, "wr": wr,
        "long": n_long, "short": n_short,
        "ret": total_ret, "sharpe": sharpe, "max_dd": max_dd,
        "pf": pf, "avg_daily": avg_daily, "n_days": n_days,
        "avg_win": avg_win, "avg_loss": avg_loss, "payoff": payoff,
        "n_sl": n_sl, "n_trail": n_trail, "n_sig": n_sig, "n_close": n_close,
        "n_be": n_be, "n_time": n_time, "n_filtered": n_filtered,
        "avg_bars": avg_bars,
        "reasons": pnl_by_reason,
        "trade_log": trade_log, "equity": eq,
    }


def live_state(df):
    close = df["close"]
    atr = calc_atr(df, 14)
    cur_atr = float(atr.iloc[-1]) if not pd.isna(atr.iloc[-1]) else 0
    price = float(close.iloc[-1])
    state = {"atr": cur_atr, "price": price}

    if len(close) >= 21:
        f, s = close.rolling(9).mean(), close.rolling(21).mean()
        state["SMA"] = ("🟢 bull" if f.iloc[-1] > s.iloc[-1] else "🔴 bear")

    delta = close.diff()
    gain = delta.where(delta > 0, 0).ewm(span=7).mean()
    loss = -delta.where(delta < 0, 0).ewm(span=7).mean()
    rsi = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))
    r = float(rsi.iloc[-1])
    tag = "🟢 OVERSOLD" if r < 25 else "🔴 OVERBOUGHT" if r > 75 else "⚪ neutral"
    state["RSI"] = (f"RSI {r:.0f} {tag}")

    sma, sd = close.rolling(20).mean(), close.rolling(20).std()
    if price < (sma - 2 * sd).iloc[-1]: state["BB"] = "🟢 below lower"
    elif price > (sma + 2 * sd).iloc[-1]: state["BB"] = "🔴 above upper"
    else: state["BB"] = "⚪ inside"

    f, s = close.ewm(span=10).mean(), close.ewm(span=30).mean()
    state["EMA"] = ("🟢 bull" if f.iloc[-1] > s.iloc[-1] else "🔴 bear")

    typ = (df["high"].iloc[-20:] + df["low"].iloc[-20:] + close.iloc[-20:]) / 3
    vol = df["tick_volume"].iloc[-20:].replace(0, 1)
    vwap = (typ * vol).sum() / vol.sum()
    if price > vwap * 1.003: state["VWAP"] = "🟢 above"
    elif price < vwap * 0.997: state["VWAP"] = "🔴 below"
    else: state["VWAP"] = "⚪ at"

    return state


def run():
    print("\n" + "═" * 120)
    print("  ⚡ BACKTEST INTRADAY v5 — SIG REMOVIDO")
    print("  " + "─" * 116)
    print("  ✅ SAÍDA POR SINAL ELIMINADA — só Trailing / Profit Lock / SL / 16:45")
    print("  ✅ Filtro ADX: SMA/EMA só entram se ADX(14) > 25")
    print("  ✅ Profit Lock: só RSI/BB/VWAP")
    print("  ✅ Trailing gatilho: 1.5x ATR")
    print("  ✅ Time-based: trailing aperta (0.3x ATR) após 20 barras")
    print("  ✅ Fecha 16:45 BRT")
    print("═" * 120)

    combos = [("WIN$", "M5", 500), ("WIN$", "M15", 500), ("WDO$", "M5", 500), ("WDO$", "M15", 500)]
    all_results = []

    for sym, tf, n_bars in combos:
        spec = CONTRACT_SPECS[sym]
        print(f"\n📡 {sym} ({spec['name']}) {tf} — {n_bars} barras...")
        df = fetch(sym, tf, n_bars)
        if df.empty:
            print("  ❌ Sem dados"); continue

        p0, p1 = float(df["close"].iloc[0]), float(df["close"].iloc[-1])
        n_days = df["date"].nunique()
        atr_avg = calc_atr(df, 14).mean()
        atr_r = atr_avg * spec["mult"]

        print(f"  ✅ {len(df)} barras, {n_days} dias | {df.index[0].strftime('%d/%m')} → {df.index[-1].strftime('%d/%m')}")
        print(f"     {p0:.2f} → {p1:.2f} ({(p1/p0-1)*100:+.2f}%)")
        print(f"     ATR: {atr_avg:.1f} pts = R$ {atr_r:.1f}/cto")

        row = {"symbol": sym, "tf": tf, "bars": len(df), "days": n_days,
               "atr_avg": atr_avg, "atr_r": atr_r}

        print(f"\n  📊 {'Estratégia':<14} {'Ret%':>7} {'T':>4} {'WR':>6} {'Sharpe':>7} {'DD':>7} {'PF':>6} {'SL':>4} {'BE':>4} {'Trail':>5} {'Sig':>4} {'16:45':>5} {'Barras':>6} {'R$/dia':>9}")
        print(f"  {'─' * 14} {'─' * 7} {'─' * 4} {'─' * 6} {'─' * 7} {'─' * 7} {'─' * 6} {'─' * 4} {'─' * 4} {'─' * 5} {'─' * 4} {'─' * 5} {'─' * 6} {'─' * 9}")

        for name, fn in STRATEGIES.items():
            sig = fn(df)
            use_sl = SL_CONFIG.get(name, True)
            use_adx = ADX_FILTER.get(name, False)
            use_be = PROFIT_LOCK_CONFIG.get(name, False)
            adx_s = calc_adx(df, 14)
            r = backtest(df, sig, sym, use_sl=use_sl,
                        use_adx_filter=use_adx, use_profit_lock=use_be,
                        trail_activate=TRAIL_ACTIVATE, adx_series=adx_s)
            if r["ok"]:
                key = name.replace("(", "").replace(")", "").replace(",", "").replace(" ", "")
                row[f"{key}_ret"] = r["ret"]
                row[f"{key}_trades"] = r["trades"]
                row[f"{key}_wr"] = r["wr"]
                row[f"{key}_sharpe"] = r["sharpe"]
                row[f"{key}_dd"] = r["max_dd"]
                row[f"{key}_pf"] = r["pf"]
                row[f"{key}_avg_daily"] = r["avg_daily"]
                row[f"{key}_payoff"] = r["payoff"]
                row[f"{key}_reasons"] = r["reasons"]
                row[f"{key}_avg_bars"] = r["avg_bars"]
                print(f"  {name:<14} {r['ret']:>+6.2f}% "
                      f"{r['trades']:>3}  "
                      f"{r['wr']:>5.1f}% "
                      f"{r['sharpe']:>6.2f} "
                      f"{r['max_dd']:>6.2f}% "
                      f"{r['pf']:>5.2f} "
                      f"{r['n_sl']:>3} "
                      f"{r['n_be']:>3} "
                      f"{r['n_trail']:>4} "
                      f"{r['n_sig']:>3} "
                      f"{r['n_close']:>4} "
                      f"{r['avg_bars']:>5.1f} "
                      f"R${r['avg_daily']:>+8.1f}")

                # Detalhe das saídas
                if r["trades"] > 0:
                    reasons = r["reasons"]
                    print(f"  {'':>14} ", end="")
                    for reason, data in sorted(reasons.items(), key=lambda x: x[1]["pnl"], reverse=True):
                        print(f"{reason}:{data['count']}(R${data['pnl']:+.0f}) ", end="")
                    print()

        all_results.append(row)

        # Sinais ao vivo
        state = live_state(df)
        price = state["price"]
        print(f"\n  🔴🟢 SINAIS AO VIVO — {sym} @ {price:.2f} ({tf})")
        print(f"  ATR: {state['atr']:.1f} pts | BE: 0.5x ATR ({0.5*state['atr']:.0f} pts)")
        for k in ("SMA", "RSI", "BB", "EMA", "VWAP"):
            if k in state:
                print(f"  {k:<5}: {state[k]}")

    # ===== RANKING =====
    if all_results:
        ranking = []
        for r in all_results:
            for name in STRATEGIES:
                key = name.replace("(", "").replace(")", "").replace(",", "").replace(" ", "")
                if f"{key}_ret" in r:
                    ranking.append({
                        "symbol": r["symbol"], "tf": r["tf"], "strategy": name,
                        "ret": r[f"{key}_ret"], "trades": r[f"{key}_trades"],
                        "wr": r[f"{key}_wr"], "sharpe": r[f"{key}_sharpe"],
                        "dd": r[f"{key}_dd"], "pf": r[f"{key}_pf"],
                        "avg_daily": r[f"{key}_avg_daily"], "days": r["days"],
                        "payoff": r.get(f"{key}_payoff", 0),
                        "reasons": r.get(f"{key}_reasons", {}),
                        "avg_bars": r.get(f"{key}_avg_bars", 0),
                    })
        ranking.sort(key=lambda x: x["ret"], reverse=True)

        print("\n\n" + "═" * 140)
        print("  📋 RANKING GERAL — v5 SIG REMOVIDO")
        print("═" * 140)
        print(f"\n{'#':>2} {'Ativo':<7} {'TF':<4} {'Estratégia':<14} {'Ret%':>7} {'T':>4} {'WR':>6} {'Sharpe':>7} {'DD':>7} {'PF':>6} {'Barras':>6} {'R$/dia':>9}")
        print("─" * 140)
        for i, r in enumerate(ranking, 1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i:>2}"
            print(f"{medal:>2} {r['symbol']:<7} {r['tf']:<4} {r['strategy']:<14} "
                  f"{r['ret']:>+6.2f}% {r['trades']:>3}  "
                  f"{r['wr']:>5.1f}% {r['sharpe']:>6.2f}  "
                  f"{r['dd']:>6.2f}% {r['pf']:>5.2f} "
                  f"{r['avg_bars']:>5.1f} "
                  f"R${r['avg_daily']:>+8.1f}")

        # Comparação v4 vs v5
        print("\n\n" + "═" * 140)
        print("  📈 COMPARAÇÃO v4 (saídas inteligentes) vs v5 (SIG removido)")
        print("═" * 140)

        v4_data = {
            "WIN$ M5": {"SMA(9,21)": -1.84, "RSI(7)": -0.17, "Bollinger": 0.77, "EMA(10,30)": -2.70, "VWAP": -0.85},
            "WIN$ M15": {"SMA(9,21)": -2.41, "RSI(7)": -2.22, "Bollinger": 0.10, "EMA(10,30)": -3.39, "VWAP": -0.16},
            "WDO$ M5": {"SMA(9,21)": -3.25, "RSI(7)": -4.15, "Bollinger": -1.94, "EMA(10,30)": -3.03, "VWAP": 0.08},
            "WDO$ M15": {"SMA(9,21)": -2.19, "RSI(7)": -1.54, "Bollinger": -0.47, "EMA(10,30)": -0.63, "VWAP": -3.01},
        }

        improvements = 0
        print(f"\n{'Ativo':<10} {'TF':<4} {'Estratégia':<14} {'v4 Ret%':>8} {'v5 Ret%':>8} {'Δ':>7}")
        print("─" * 60)
        for r in ranking:
            key = f"{r['symbol']} {r['tf']}"
            if key in v4_data and r["strategy"] in v4_data[key]:
                v4_ret = v4_data[key][r["strategy"]]
                v5_ret = r["ret"]
                delta = v5_ret - v4_ret
                icon = "✅" if delta > 0 else "❌" if delta < -0.5 else "➡️"
                print(f"  {r['symbol']:<7} {r['tf']:<4} {r['strategy']:<14} {v4_ret:>+7.2f}% {v5_ret:>+7.2f}% {delta:>+6.2f}% {icon}")
                if delta > 0:
                    improvements += 1

        total = sum(len(v) for v in v4_data.values())
        print(f"\n  Melhorias: {improvements}/{total} ({improvements/total*100:.0f}%)")

        # Análise dos motivos de saída
        print("\n  🛡️ ANÁLISE SAÍDAS v5:")
        for r in all_results:
            sub = [x for x in ranking if x["symbol"] == r["symbol"] and x["tf"] == r["tf"]]
            if sub:
                best = max(sub, key=lambda x: x["ret"])
                reasons = best["reasons"]
                total_t = sum(v["count"] for v in reasons.values())
                total_pnl = sum(v["pnl"] for v in reasons.values())
                print(f"\n    {r['symbol']} {r['tf']} — {best['strategy']} ({best['ret']:+.2f}%):")
                for reason, data in sorted(reasons.items(), key=lambda x: x[1]["pnl"], reverse=True):
                    pct = data["count"] / total_t * 100 if total_t else 0
                    wr_r = data["wins"] / data["count"] * 100 if data["count"] else 0
                    print(f"      {reason:<6}: {data['count']:>3} trades ({pct:4.1f}%)  "
                          f"WR {wr_r:.0f}%  PnL R${data['pnl']:+.0f}")

        profitable = [x for x in ranking if x["ret"] > 0]
        print(f"\n  💰 Lucrativos: {len(profitable)}/{len(ranking)}")
        for p in profitable:
            print(f"    ✅ {p['symbol']} {p['tf']} {p['strategy']} — {p['ret']:+.2f}% | "
                  f"Sharpe {p['sharpe']:.2f} | PF {p['pf']:.2f} | R${p['avg_daily']:+.0f}/dia")

        out = Path("/tmp/intraday_v5.csv")
        pd.DataFrame(ranking).to_csv(out, index=False)
        print(f"\n  💾 CSV: {out}")

    print("\n" + "═" * 120 + "\n")


if __name__ == "__main__":
    run()
