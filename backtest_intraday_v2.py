"""
Backtest intraday com stops inteligentes (ATR-based).
- Stop Loss: 1.5x ATR(14) — adapta à volatilidade
- Trailing Stop: ativa após 1x ATR de lucro, trail 0.5x ATR
- Fecha tudo às 16:45 BRT
- Long + Short, sem posição overnight

Uso: PYTHONPATH=./agent ./agent/venv/bin/python backtest_intraday_v2.py
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


# ===== ESTRATÉGIAS (sinal: +1 long, -1 short, 0 flat) =====

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


def backtest(df, signals, symbol, *, capital=100_000.0, max_ct=3, use_sl=True):
    """
    use_sl=True  → Stop Loss (1.5x ATR) + Trailing Stop
    use_sl=False → Só Trailing Stop (sem SL fixo — p/ estratégias de reversão)
    """
    spec = CONTRACT_SPECS[symbol]
    mult, margin, slip_r = spec["mult"], spec["margin"], spec["slip_r"]
    atr = calc_atr(df, 14)

    cash = capital
    pos = 0       # +1 long, -1 short
    ep = 0.0      # entry price
    e_date = None
    e_atr = 0.0   # ATR no momento da entrada
    best = 0.0    # melhor preço desde entrada (p/ trailing)
    sl_price = 0.0  # nível do stop loss
    trail_on = False

    equity, trade_log, daily_pnl = [], [], []
    n_trades = n_wins = n_long = n_short = 0
    n_sl = n_trail = n_sig = n_close = 0
    gross_win = 0.0
    gross_loss_val = 0.0

    def _close(price, reason):
        nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, e_atr
        nonlocal n_trades, n_wins, n_long, n_short, n_sl, n_trail, n_sig, n_close
        nonlocal gross_win, gross_loss_val

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
        elif reason == "SIGNAL": n_sig += 1
        elif reason == "1645": n_close += 1

        if pnl > 0:
            n_wins += 1; gross_win += pnl
        else:
            gross_loss_val += abs(pnl)

        trade_log.append({"type": "LONG" if pos == 1 else "SHORT",
                          "entry": str(e_date), "exit": "",
                          "ep": ep, "xp": price, "pnl": pnl, "reason": reason})
        daily_pnl.append(pnl)
        pos = 0; ep = 0; best = 0; sl_price = 0; trail_on = False

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

        # ===== POSIÇÃO ABERTA: GESTÃO =====
        if pos != 0:
            # Atualiza melhor preço
            if pos == 1:
                best = max(best, high)
            else:
                best = min(best, low) if best > 0 else low

            # Ativa trailing
            if not trail_on and e_atr > 0:
                activate = 1.0 * e_atr
                if pos == 1 and (best - ep) >= activate:
                    trail_on = True
                elif pos == -1 and (ep - best) >= activate:
                    trail_on = True

            # STOP LOSS fixo (1.5x ATR do entrada) — só se use_sl=True
            if use_sl and sl_price > 0:
                if pos == 1 and low <= sl_price:
                    _close(sl_price, "SL"); continue
                elif pos == -1 and high >= sl_price:
                    _close(sl_price, "SL"); continue

            # TRAILING STOP
            if trail_on and e_atr > 0:
                trail_dist = 0.5 * e_atr
                if pos == 1:
                    trail_level = best - trail_dist
                    if trail_level > sl_price or (not use_sl and sl_price == 0):
                        sl_price = trail_level  # move SL (ou define se não tinha)
                    if low <= trail_level:
                        _close(trail_level, "TRAIL"); continue
                elif pos == -1:
                    trail_level = best + trail_dist
                    if trail_level < sl_price or (not use_sl and sl_price == 0):
                        sl_price = trail_level
                    if high >= trail_level:
                        _close(trail_level, "TRAIL"); continue

            # 16:45 → fecha tudo
            if hour > CLOSE_HOUR or (hour == CLOSE_HOUR and minute >= CLOSE_MINUTE):
                _close(price, "1645"); continue

            # Sinal reverteu → sai
            if pos == 1 and target <= 0:
                _close(price, "SIGNAL")
            elif pos == -1 and target >= 0:
                _close(price, "SIGNAL")

            # Se fechou, tenta abrir na direção oposta no mesmo bar
            if pos == 0:
                sl_dist = 1.5 * cur_atr if use_sl else 0
                if target == 1 and cur_atr > 0:
                    cost = slip_r * max_ct + COMMISSION * max_ct
                    if cash >= margin * max_ct + cost:
                        cash -= margin * max_ct + cost
                        pos = 1; ep = price; e_date = date; e_atr = cur_atr
                        best = price; sl_price = price - sl_dist; trail_on = False
                elif target == -1 and cur_atr > 0:
                    cost = slip_r * max_ct + COMMISSION * max_ct
                    if cash >= margin * max_ct + cost:
                        cash -= margin * max_ct + cost
                        pos = -1; ep = price; e_date = date; e_atr = cur_atr
                        best = price; sl_price = price + sl_dist; trail_on = False
            continue

        # ===== SEM POSIÇÃO: ENTRADA =====
        if pos == 0 and cur_atr > 0:
            sl_dist = 1.5 * cur_atr if use_sl else 0
            cost = slip_r * max_ct + COMMISSION * max_ct
            if target == 1 and cash >= margin * max_ct + cost:
                cash -= margin * max_ct + cost
                pos = 1; ep = price; e_date = date; e_atr = cur_atr
                best = price; sl_price = price - sl_dist; trail_on = False
            elif target == -1 and cash >= margin * max_ct + cost:
                cash -= margin * max_ct + cost
                pos = -1; ep = price; e_date = date; e_atr = cur_atr
                best = price; sl_price = price + sl_dist; trail_on = False

    # Força fechar se sobrou posição
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

    # PnL por motivo
    pnl_by_reason = {}
    for t in trade_log:
        r = t["reason"]
        if r not in pnl_by_reason:
            pnl_by_reason[r] = {"count": 0, "pnl": 0, "wins": 0}
        pnl_by_reason[r]["count"] += 1
        pnl_by_reason[r]["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            pnl_by_reason[r]["wins"] += 1

    return {
        "ok": True, "trades": n_trades, "wins": n_wins, "wr": wr,
        "long": n_long, "short": n_short,
        "ret": total_ret, "sharpe": sharpe, "max_dd": max_dd,
        "pf": pf, "avg_daily": avg_daily, "n_days": n_days,
        "avg_win": avg_win, "avg_loss": avg_loss, "payoff": payoff,
        "n_sl": n_sl, "n_trail": n_trail, "n_sig": n_sig, "n_close": n_close,
        "reasons": pnl_by_reason,
        "trade_log": trade_log, "equity": eq,
    }


def live_state(df):
    close = df["close"]
    atr = calc_atr(df, 14)
    cur_atr = float(atr.iloc[-1]) if not pd.isna(atr.iloc[-1]) else 0
    price = float(close.iloc[-1])
    spec = CONTRACT_SPECS.get("WIN$", CONTRACT_SPECS["WDO$"])  # generic
    state = {"atr": cur_atr, "price": price}

    # SMA
    if len(close) >= 21:
        f, s = close.rolling(9).mean(), close.rolling(21).mean()
        state["SMA"] = ("🟢 bull" if f.iloc[-1] > s.iloc[-1] else "🔴 bear")

    # RSI
    delta = close.diff()
    gain = delta.where(delta > 0, 0).ewm(span=7).mean()
    loss = -delta.where(delta < 0, 0).ewm(span=7).mean()
    rsi = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))
    r = float(rsi.iloc[-1])
    tag = "🟢 OVERSOLD" if r < 25 else "🔴 OVERBOUGHT" if r > 75 else "⚪ neutral"
    state["RSI"] = (f"RSI {r:.0f} {tag}")

    # Bollinger
    sma, sd = close.rolling(20).mean(), close.rolling(20).std()
    if price < (sma - 2 * sd).iloc[-1]: state["BB"] = "🟢 below lower"
    elif price > (sma + 2 * sd).iloc[-1]: state["BB"] = "🔴 above upper"
    else: state["BB"] = "⚪ inside"

    # EMA
    f, s = close.ewm(span=10).mean(), close.ewm(span=30).mean()
    state["EMA"] = ("🟢 bull" if f.iloc[-1] > s.iloc[-1] else "🔴 bear")

    # VWAP
    typ = (df["high"].iloc[-20:] + df["low"].iloc[-20:] + close.iloc[-20:]) / 3
    vol = df["tick_volume"].iloc[-20:].replace(0, 1)
    vwap = (typ * vol).sum() / vol.sum()
    if price > vwap * 1.003: state["VWAP"] = "🟢 above"
    elif price < vwap * 0.997: state["VWAP"] = "🔴 below"
    else: state["VWAP"] = "⚪ at"

    return state


def run():
    print("\n" + "═" * 118)
    print("  ⚡ BACKTEST INTRADAY v2 — STOPS INTELIGENTES ATR")
    print("  " + "─" * 114)
    print("  Stop Loss:   1.5x ATR(14) — fixo no entrada, adapta à volatilidade")
    print("  Trailing:    ativa após 1.0x ATR de lucro, traila 0.5x ATR do topo")
    print("  Fecha tudo:  16:45 BRT")
    print("  Capital: R$ 100.000 | Max 3 contratos | Long + Short")
    print("═" * 118)

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
        sl_r = 1.5 * atr_r
        trail_r = 0.5 * atr_r

        print(f"  ✅ {len(df)} barras, {n_days} dias | {df.index[0].strftime('%d/%m')} → {df.index[-1].strftime('%d/%m')}")
        print(f"     {p0:.2f} → {p1:.2f} ({(p1/p0-1)*100:+.2f}%)")
        print(f"     ATR: {atr_avg:.1f} pts = R$ {atr_r:.1f}/cto | SL: R$ {sl_r:.1f}/cto | Trail: R$ {trail_r:.1f}/cto")
        print(f"     Config: SMA/EMA → SL+Trail | RSI/BB → só Trail | Fecha 16:45")

        row = {"symbol": sym, "tf": tf, "bars": len(df), "days": n_days,
               "atr_avg": atr_avg, "atr_r": atr_r, "sl_r": sl_r}

        print(f"\n  📊 {'Estratégia':<14} {'Ret%':>7} {'T':>4} {'L/S':>5} {'WR':>6} {'Sharpe':>7} {'DD':>7} {'PF':>6} {'SL':>4} {'Trail':>5} {'Sig':>4} {'16:45':>5} {'R$/dia':>9}")
        print(f"  {'─' * 14} {'─' * 7} {'─' * 4} {'─' * 5} {'─' * 6} {'─' * 7} {'─' * 7} {'─' * 6} {'─' * 4} {'─' * 5} {'─' * 4} {'─' * 5} {'─' * 9}")

        # SMA/EMA usam SL+Trail | RSI/BB/VWAP usam só Trail
        SL_CONFIG = {"SMA(9,21)": True, "EMA(10,30)": True,
                     "RSI(7)": False, "Bollinger": False, "VWAP": False}

        for name, fn in STRATEGIES.items():
            sig = fn(df)
            use_sl = SL_CONFIG.get(name, True)
            r = backtest(df, sig, sym, use_sl=use_sl)
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
                print(f"  {name:<14} {r['ret']:>+6.2f}% "
                      f"{r['trades']:>3}  "
                      f"{r['long']}/{r['short']:>2}  "
                      f"{r['wr']:>5.1f}% "
                      f"{r['sharpe']:>6.2f} "
                      f"{r['max_dd']:>6.2f}% "
                      f"{r['pf']:>5.2f} "
                      f"{r['n_sl']:>3} "
                      f"{r['n_trail']:>4} "
                      f"{r['n_sig']:>3} "
                      f"{r['n_close']:>4} "
                      f"R${r['avg_daily']:>+8.1f}")

                # Detalhe dos stops
                if r["trades"] > 0:
                    reasons = r["reasons"]
                    total_pnl = sum(x["pnl"] for x in reasons.values())
                    print(f"  {'':>14} ", end="")
                    for reason, data in reasons.items():
                        print(f"{reason}:{data['count']}(R${data['pnl']:+.0f}) ", end="")
                    print()

        all_results.append(row)

        # Sinais ao vivo
        state = live_state(df)
        price = state["price"]
        atr = state["atr"]

        print(f"\n  🔴🟢 SINAIS AO VIVO — {sym} @ {price:.2f} ({tf})")
        print(f"  ATR: {atr:.1f} pts | SL: {1.5*atr:.0f} pts (R$ {1.5*atr*spec['mult']:.0f}/cto)")
        for k in ("SMA", "RSI", "BB", "EMA", "VWAP"):
            if k in state:
                print(f"  {k:<5}: {state[k]}")

        # Score
        longs = sum(1 for k in state if k not in ("atr","price") and "🟢" in str(state[k]))
        shorts = sum(1 for k in state if k not in ("atr","price") and "🔴" in str(state[k]))
        flat = 5 - longs - shorts
        print(f"  Score: {longs} LONG / {shorts} SHORT / {flat} FLAT")
        if longs >= 3: print(f"  ✅ LONG FORTE")
        elif longs >= 2: print(f"  🟡 LONG MODERADO")
        elif shorts >= 3: print(f"  ❌ SHORT FORTE")
        elif shorts >= 2: print(f"  🟠 SHORT MODERADO")
        else: print(f"  ⚪ NEUTRO")

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
                    })
        ranking.sort(key=lambda x: x["ret"], reverse=True)

        print("\n\n" + "═" * 130)
        print("  📋 RANKING GERAL — COM STOPS INTELIGENTES")
        print("═" * 130)
        print(f"\n{'#':>2} {'Ativo':<7} {'TF':<4} {'Estratégia':<14} {'Ret%':>7} {'T':>4} {'WR':>6} {'Sharpe':>7} {'DD':>7} {'PF':>6} {'Payoff':>7} {'R$/dia':>9}")
        print("─" * 130)
        for i, r in enumerate(ranking, 1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i:>2}"
            print(f"{medal:>2} {r['symbol']:<7} {r['tf']:<4} {r['strategy']:<14} "
                  f"{r['ret']:>+6.2f}% {r['trades']:>3}  "
                  f"{r['wr']:>5.1f}% {r['sharpe']:>6.2f}  "
                  f"{r['dd']:>6.2f}% {r['pf']:>5.2f} "
                  f"{r['payoff']:>6.2f} "
                  f"R${r['avg_daily']:>+8.1f}")

        # M5 vs M15
        print("\n  ⏰ M5 vs M15:")
        for sym in ["WIN$", "WDO$"]:
            sub = [x for x in ranking if x["symbol"] == sym]
            for strat in STRATEGIES:
                m5 = next((x for x in sub if x["tf"] == "M5" and x["strategy"] == strat), None)
                m15 = next((x for x in sub if x["tf"] == "M15" and x["strategy"] == strat), None)
                if m5 and m15:
                    w = "M5" if m5["ret"] > m15["ret"] else "M15"
                    print(f"    {sym} {strat:<14} M5={m5['ret']:+.2f}%  M15={m15['ret']:+.2f}%  → {w}")

        # Conclusões
        profitable = [x for x in ranking if x["ret"] > 0]
        print(f"\n  💰 Lucrativos: {len(profitable)}/{len(ranking)}")
        for p in profitable:
            reasons = p["reasons"]
            r_pnl = sum(v["pnl"] for v in reasons.values())
            print(f"    ✅ {p['symbol']} {p['tf']} {p['strategy']} — {p['ret']:+.2f}% | "
                  f"{p['trades']} trades | Sharpe {p['sharpe']:.2f} | PF {p['pf']:.2f} | R${p['avg_daily']:+.0f}/dia")

        if profitable:
            b = profitable[0]
            print(f"\n  🏆 CAMPEÃ: {b['symbol']} {b['tf']} {b['strategy']}")
            print(f"     {b['ret']:+.2f}% em {b['days']} dias | Sharpe {b['sharpe']:.2f} | R${b['avg_daily']:+.0f}/dia")

        # Análise dos stops
        print(f"\n  🛡️ ANÁLISE DOS STOPS:")
        for r in all_results:
            sub = [x for x in ranking if x["symbol"] == r["symbol"] and x["tf"] == r["tf"]]
            if sub:
                best = max(sub, key=lambda x: x["ret"])
                reasons = best["reasons"]
                total_t = sum(v["count"] for v in reasons.values())
                total_pnl = sum(v["pnl"] for v in reasons.values())
                print(f"\n    {r['symbol']} {r['tf']} ({r['atr_avg']:.0f} pts ATR):")
                for reason, data in sorted(reasons.items(), key=lambda x: x[1]["pnl"], reverse=True):
                    pct = data["count"] / total_t * 100 if total_t else 0
                    wr_reason = data["wins"] / data["count"] * 100 if data["count"] else 0
                    print(f"      {reason:<6}: {data['count']:>3} trades ({pct:4.1f}%)  "
                          f"WR {wr_reason:.0f}%  PnL R${data['pnl']:+.0f}")

        out = Path("/tmp/intraday_v2.csv")
        pd.DataFrame(ranking).to_csv(out, index=False)
        print(f"\n  💾 CSV: {out}")

    print("\n" + "═" * 118 + "\n")


if __name__ == "__main__":
    run()
