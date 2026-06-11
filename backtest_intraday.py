"""
Backtest intraday puro — WIN$ + WDO$ — long AND short.
Zera posição no fim de cada pregão. Dados MT5.
Uso: PYTHONPATH=./agent ./agent/venv/bin/python backtest_intraday.py
"""

import sys, csv, io, subprocess, os, json
from pathlib import Path
import numpy as np, pandas as pd

WINE_PYTHON = os.path.expanduser("~/.wine/drive_c/Python311/python.exe")
FETCH_SCRIPT = os.path.join(os.path.dirname(__file__), "mt5_fetch.py")

# B3 session hours (UTC-3 → stored as UTC in MT5)
SESSION_START = 12  # 09h BRT = 12h UTC
SESSION_END = 21     # 18h BRT = 21h UTC

CONTRACT_SPECS = {
    "WIN$": {"mult": 0.20, "name": "Mini Índice", "margin": 5000, "tick": 5, "slippage_r": 1.0},
    "WDO$": {"mult": 10.0, "name": "Mini Dólar", "margin": 3000, "tick": 0.5, "slippage_r": 5.0},
}

COMMISSION_PER_CONTRACT = 2.5  # R$


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
    df["date"] = df.index.date
    return df[["open", "high", "low", "close", "tick_volume", "real_volume", "hour", "date"]].dropna(subset=["close"])


# ===== ESTRATÉGIAS INTRADAY (sinal: +1 long, -1 short, 0 flat) =====

def sma_cross(df, fast=9, slow=21):
    """SMA cross — otimizado para intraday."""
    f = df["close"].rolling(fast).mean()
    s = df["close"].rolling(slow).mean()
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[f > s] = 1
    sig[f < s] = -1
    return sig.shift(1).fillna(0)

def rsi_reversal(df, period=7, extreme=25):
    """RSI reversal — sobrevenda compra, sobrecompra vende."""
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).ewm(span=period).mean()
    loss = -delta.where(delta < 0, 0).ewm(span=period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[rsi < extreme] = 1       # sobrevenda → compra
    sig[rsi > (100 - extreme)] = -1  # sobrecompra → venda
    return sig.shift(1).fillna(0)

def bollinger_bounce(df, period=20, std=2.0):
    """Bollinger bounce — toca banda inferior compra, superior vende."""
    sma = df["close"].rolling(period).mean()
    sd = df["close"].rolling(period).std()
    lower, upper = sma - std * sd, sma + std * sd
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[df["close"] < lower] = 1
    sig[df["close"] > upper] = -1
    return sig.shift(1).fillna(0)

def momentum_trend(df, fast=10, slow=30):
    """Momentum com filtro de tendência — EMA cross."""
    f = df["close"].ewm(span=fast).mean()
    s = df["close"].ewm(span=slow).mean()
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[f > s] = 1
    sig[f < s] = -1
    return sig.shift(1).fillna(0)

def vwap_trend(df, period=20):
    """Tendência baseada em preço vs VWAP."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["tick_volume"].replace(0, 1)
    cum_tp_vol = (typical * vol).rolling(period).sum()
    cum_vol = vol.rolling(period).sum()
    vwap = cum_tp_vol / cum_vol
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[df["close"] > vwap * 1.003] = 1   # acima VWAP → compra
    sig[df["close"] < vwap * 0.997] = -1   # abaixo VWAP → venda
    return sig.shift(1).fillna(0)

STRATEGIES = {
    "SMA(9,21)": sma_cross,
    "RSI(7)": rsi_reversal,
    "Bollinger": bollinger_bounce,
    "EMA(10,30)": momentum_trend,
    "VWAP": vwap_trend,
}


def backtest_intraday(df, signals, symbol, *, capital=100_000.0, max_contracts=3):
    """Backtest intraday: long + short, zera no fim do pregão."""
    spec = CONTRACT_SPECS[symbol]
    mult = spec["mult"]
    margin = spec["margin"]
    slippage_r = spec["slippage_r"]

    if len(signals) != len(df):
        return {"ok": False}

    cash = capital
    position = 0  # +1 long, -1 short
    entry_price = 0.0
    entry_date = None
    equity = []
    n_trades, n_wins, n_long, n_short = 0, 0, 0, 0
    gross_profit, gross_loss = 0.0, 0.0
    trade_log = []
    daily_pnl = []

    for date, row in df.iterrows():
        price = float(row["close"])
        hour = int(row["hour"])
        target = int(signals.loc[date])

        # Mark-to-market
        if position == 1:
            unrealized = (price - entry_price) * mult * max_contracts
        elif position == -1:
            unrealized = (entry_price - price) * mult * max_contracts
        else:
            unrealized = 0
        equity.append(cash + unrealized)

        # FIM DO PREGÃO → zera posição
        if hour >= SESSION_END and position != 0:
            slippage = slippage_r * max_contracts
            comm = COMMISSION_PER_CONTRACT * max_contracts
            if position == 1:
                realized = (price - entry_price) * mult * max_contracts - slippage - comm
                n_long += 1
            else:
                realized = (entry_price - price) * mult * max_contracts - slippage - comm
                n_short += 1
            cash += realized
            n_trades += 1
            if realized > 0:
                n_wins += 1
                gross_profit += realized
            else:
                gross_loss += abs(realized)
            trade_log.append({
                "type": "LONG" if position == 1 else "SHORT",
                "entry": str(entry_date), "exit": str(date),
                "ep": entry_price, "xp": price,
                "pnl": realized,
            })
            daily_pnl.append(realized)
            position = 0
            entry_price = 0
            continue

        # INÍCIO DO PREGÃO → ignora primeiro bar
        if hour < SESSION_START + 1:
            continue

        # SINAIS
        if target == 1 and position == 0:
            # LONG
            cost = slippage_r * max_contracts + COMMISSION_PER_CONTRACT * max_contracts
            if cash >= margin * max_contracts + cost:
                cash -= margin * max_contracts + cost
                position = 1
                entry_price = price
                entry_date = date

        elif target == -1 and position == 0:
            # SHORT
            cost = slippage_r * max_contracts + COMMISSION_PER_CONTRACT * max_contracts
            if cash >= margin * max_contracts + cost:
                cash -= margin * max_contracts + cost
                position = -1
                entry_price = price
                entry_date = date

        elif target == 0 and position != 0:
            # FECHA posição (sinal neutro)
            slippage = slippage_r * max_contracts
            comm = COMMISSION_PER_CONTRACT * max_contracts
            if position == 1:
                realized = (price - entry_price) * mult * max_contracts - slippage - comm
                n_long += 1
            else:
                realized = (entry_price - price) * mult * max_contracts - slippage - comm
                n_short += 1
            cash += margin * max_contracts + realized
            n_trades += 1
            if realized > 0:
                n_wins += 1
                gross_profit += realized
            else:
                gross_loss += abs(realized)
            trade_log.append({
                "type": "LONG" if position == 1 else "SHORT",
                "entry": str(entry_date), "exit": str(date),
                "ep": entry_price, "xp": price,
                "pnl": realized,
            })
            daily_pnl.append(realized)
            position = 0
            entry_price = 0

        # Inverter posição (ex: sinal muda de +1 pra -1)
        elif target == 1 and position == -1:
            slippage = slippage_r * max_contracts
            comm = COMMISSION_PER_CONTRACT * max_contracts * 2  # fecha + abre
            realized = (entry_price - price) * mult * max_contracts - slippage - comm
            cash += margin * max_contracts + realized
            n_trades += 1
            n_short += 1
            if realized > 0:
                n_wins += 1
                gross_profit += realized
            else:
                gross_loss += abs(realized)
            trade_log.append({
                "type": "SHORT", "entry": str(entry_date), "exit": str(date),
                "ep": entry_price, "xp": price, "pnl": realized,
            })
            daily_pnl.append(realized)
            # Abre long
            cost2 = slippage_r * max_contracts + COMMISSION_PER_CONTRACT * max_contracts
            if cash >= margin * max_contracts + cost2:
                cash -= margin * max_contracts + cost2
                position = 1
                entry_price = price
                entry_date = date
            else:
                position = 0

        elif target == -1 and position == 1:
            slippage = slippage_r * max_contracts
            comm = COMMISSION_PER_CONTRACT * max_contracts * 2
            realized = (price - entry_price) * mult * max_contracts - slippage - comm
            cash += margin * max_contracts + realized
            n_trades += 1
            n_long += 1
            if realized > 0:
                n_wins += 1
                gross_profit += realized
            else:
                gross_loss += abs(realized)
            trade_log.append({
                "type": "LONG", "entry": str(entry_date), "exit": str(date),
                "ep": entry_price, "xp": price, "pnl": realized,
            })
            daily_pnl.append(realized)
            # Abre short
            cost2 = slippage_r * max_contracts + COMMISSION_PER_CONTRACT * max_contracts
            if cash >= margin * max_contracts + cost2:
                cash -= margin * max_contracts + cost2
                position = -1
                entry_price = price
                entry_date = date
            else:
                position = 0

    # Force close if still open
    if position != 0:
        last = float(df["close"].iloc[-1])
        slippage = slippage_r * max_contracts
        comm = COMMISSION_PER_CONTRACT * max_contracts
        if position == 1:
            realized = (last - entry_price) * mult * max_contracts - slippage - comm
        else:
            realized = (entry_price - last) * mult * max_contracts - slippage - comm
        cash += margin * max_contracts + realized
        n_trades += 1
        if realized > 0:
            n_wins += 1
            gross_profit += realized
        else:
            gross_loss += abs(realized)

    eq = pd.Series(equity, index=df.index[:len(equity)])
    total_return = (cash - capital) / capital * 100

    # Intraday stats
    n_days = df["date"].nunique()
    avg_daily = sum(t["pnl"] for t in trade_log) / n_days if n_days else 0
    pf = gross_profit / gross_loss if gross_loss > 0 else (999 if gross_profit > 0 else 0)

    # Sharpe (diário)
    if len(daily_pnl) > 1:
        sharpe = np.mean(daily_pnl) / np.std(daily_pnl) * np.sqrt(252) if np.std(daily_pnl) > 0 else 0
    else:
        sharpe = 0

    # Max DD
    dd = (eq - eq.cummax()) / eq.cummax()
    max_dd = dd.min() * 100

    # Win rate
    wr = (n_wins / n_trades * 100) if n_trades else 0

    # Avg win / avg loss
    wins = [t["pnl"] for t in trade_log if t["pnl"] > 0]
    losses = [t["pnl"] for t in trade_log if t["pnl"] <= 0]
    avg_win = np.mean(wins) if wins else 0
    avg_loss = abs(np.mean(losses)) if losses else 1

    return {
        "ok": True, "trades": n_trades, "wins": n_wins, "wr": wr,
        "long_trades": n_long, "short_trades": n_short,
        "ret": total_return, "sharpe": sharpe, "max_dd": max_dd,
        "pf": pf, "avg_daily": avg_daily, "n_days": n_days,
        "avg_win": avg_win, "avg_loss": avg_loss, "payoff": avg_win / avg_loss if avg_loss > 0 else 0,
        "trade_log": trade_log, "equity": eq, "daily_pnl": daily_pnl,
    }


def live_state(df):
    close = df["close"]
    state = {}

    # SMA
    if len(close) >= 21:
        f, s = close.rolling(9).mean(), close.rolling(21).mean()
        if f.iloc[-1] > s.iloc[-1]:
            state["sma"] = (f"SMA bull {close.iloc[-1]:.0f}", "long")
        else:
            state["sma"] = (f"SMA bear {close.iloc[-1]:.0f}", "short")
    else:
        state["sma"] = ("—", "flat")

    # RSI
    delta = close.diff()
    gain = delta.where(delta > 0, 0).ewm(span=7).mean()
    loss = -delta.where(delta < 0, 0).ewm(span=7).mean()
    rsi = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))
    r = float(rsi.iloc[-1])
    if r < 25:
        state["rsi"] = (f"RSI {r:.0f} OVERSOLD 🟢", "long")
    elif r > 75:
        state["rsi"] = (f"RSI {r:.0f} OVERBOUGHT 🔴", "short")
    else:
        state["rsi"] = (f"RSI {r:.0f}", "flat")

    # Bollinger
    sma, sd = close.rolling(20).mean(), close.rolling(20).std()
    lower, upper = sma - 2 * sd, sma + 2 * sd
    p = close.iloc[-1]
    if p < lower.iloc[-1]:
        state["bb"] = (f"%B < 0 BUY 🟢", "long")
    elif p > upper.iloc[-1]:
        state["bb"] = (f"%B > 1 SELL 🔴", "short")
    else:
        state["bb"] = (f"inside bands", "flat")

    # EMA
    f, s = close.ewm(span=10).mean(), close.ewm(span=30).mean()
    if f.iloc[-1] > s.iloc[-1]:
        state["ema"] = ("EMA bull", "long")
    else:
        state["ema"] = ("EMA bear", "short")

    # VWAP
    typical = (df["high"].iloc[-20:] + df["low"].iloc[-20:] + df["close"].iloc[-20:]) / 3
    vol = df["tick_volume"].iloc[-20:].replace(0, 1)
    vwap = (typical * vol).sum() / vol.sum()
    if p > vwap * 1.003:
        state["vwap"] = (f"above VWAP ({vwap:.0f})", "long")
    elif p < vwap * 0.997:
        state["vwap"] = (f"below VWAP ({vwap:.0f})", "short")
    else:
        state["vwap"] = (f"at VWAP ({vwap:.0f})", "flat")

    return state


def run():
    print("\n" + "═" * 110)
    print("  ⚡ BACKTEST INTRADAY — WIN$ + WDO$ — LONG & SHORT — ZERA NO FIM DO PREGÃO")
    print("  " + "─" * 106)
    print("  Capital: R$ 100.000 | Max 3 contratos | Comissão R$ 2,50/cto")
    print("  Slippage: WIN R$ 1/cto | WDO R$ 5/cto")
    print("  Intraday puro: sem posição overnight")
    print("═" * 110)

    combos = [
        ("WIN$", "M5", 500),   # ~1 semana
        ("WIN$", "M15", 500),  # ~3 semanas
        ("WDO$", "M5", 500),
        ("WDO$", "M15", 500),
    ]

    all_results = []

    for sym, tf, n_bars in combos:
        spec = CONTRACT_SPECS[sym]
        print(f"\n📡 {sym} ({spec['name']}) {tf} — {n_bars} barras...")
        df = fetch(sym, tf, n_bars)
        if df.empty:
            print("  ❌ Sem dados")
            continue

        p0, p1 = float(df["close"].iloc[0]), float(df["close"].iloc[-1])
        ret_period = (p1 / p0 - 1) * 100
        n_days = df["date"].nunique()
        print(f"  ✅ {len(df)} barras, {n_days} dias úteis")
        print(f"     {df.index[0]} → {df.index[-1]}")
        print(f"     Preço: {p0:.2f} → {p1:.2f} ({ret_period:+.2f}%)")

        row = {"symbol": sym, "tf": tf, "bars": len(df), "days": n_days,
               "p0": p0, "p1": p1, "ret_period": ret_period}

        print(f"\n  📊 {'Estratégia':<14} {'Ret%':>7} {'Trades':>7} {'L/S':>5} {'WR':>6} {'Sharpe':>7} {'DD':>7} {'PF':>6} {'Payoff':>7} {'R$/dia':>9}")
        print(f"  {'─' * 14} {'─' * 7} {'─' * 7} {'─' * 5} {'─' * 6} {'─' * 7} {'─' * 7} {'─' * 6} {'─' * 7} {'─' * 9}")

        for name, fn in STRATEGIES.items():
            sig = fn(df)
            r = backtest_intraday(df, sig, sym)
            if r["ok"]:
                key = name.strip().replace("(", "").replace(")", "").replace(",", "").replace(" ", "")
                row[f"{key}_ret"] = r["ret"]
                row[f"{key}_trades"] = r["trades"]
                row[f"{key}_wr"] = r["wr"]
                row[f"{key}_sharpe"] = r["sharpe"]
                row[f"{key}_dd"] = r["max_dd"]
                row[f"{key}_pf"] = r["pf"]
                row[f"{key}_avg_daily"] = r["avg_daily"]
                row[f"{key}_payoff"] = r["payoff"]
                print(f"  {name:<14} {r['ret']:>+6.2f}% "
                      f"{r['trades']:>6d}  "
                      f"{r['long_trades']}/{r['short_trades']:>2}  "
                      f"{r['wr']:>5.1f}%  "
                      f"{r['sharpe']:>6.2f}  "
                      f"{r['max_dd']:>6.2f}%  "
                      f"{r['pf']:>5.2f}  "
                      f"{r['payoff']:>6.2f}  "
                      f"R${r['avg_daily']:>+8.1f}")

        all_results.append(row)

        # Sinais ao vivo
        state = live_state(df)
        longs = sum(1 for _, (_, t) in state.items() if t == "long")
        shorts = sum(1 for _, (_, t) in state.items() if t == "short")
        flat = 5 - longs - shorts

        print(f"\n  🔴🟢 SINAIS AO VIVO — {sym} @ {p1:.2f} ({tf})")
        for k, (txt, tag) in state.items():
            icon = "🟢" if tag == "long" else "🔴" if tag == "short" else "⚪"
            print(f"  {icon} {k:<6}: {txt}")
        print(f"  Score: {longs} LONG / {shorts} SHORT / {flat} FLAT")

        if longs >= 3:
            print(f"  ✅ LONG FORTE ({longs}/5)")
        elif longs >= 2:
            print(f"  🟡 LONG MODERADO ({longs}/5)")
        elif shorts >= 3:
            print(f"  ❌ SHORT FORTE ({shorts}/5)")
        elif shorts >= 2:
            print(f"  🟠 SHORT MODERADO ({shorts}/5)")
        else:
            print(f"  ⚪ NEUTRO / SEM CONVICÇÃO")

    # ===== TABELA FINAL =====
    if all_results:
        print("\n\n" + "═" * 130)
        print("  📋 RANKING GERAL — INTRADAY LONG/SHORT")
        print("═" * 130)

        # Coleta todas as estratégias
        ranking = []
        for r in all_results:
            for name in STRATEGIES:
                key = name.strip().replace("(", "").replace(")", "").replace(",", "").replace(" ", "")
                if f"{key}_ret" in r:
                    ranking.append({
                        "symbol": r["symbol"], "tf": r["tf"],
                        "strategy": name,
                        "ret": r[f"{key}_ret"],
                        "trades": r[f"{key}_trades"],
                        "wr": r[f"{key}_wr"],
                        "sharpe": r[f"{key}_sharpe"],
                        "dd": r[f"{key}_dd"],
                        "pf": r[f"{key}_pf"],
                        "avg_daily": r[f"{key}_avg_daily"],
                        "days": r["days"],
                    })

        ranking.sort(key=lambda x: x["ret"], reverse=True)

        print(f"\n{'#':>2} {'Ativo':<7} {'TF':<4} {'Estratégia':<14} {'Ret%':>7} {'Trades':>7} {'WR':>6} {'Sharpe':>7} {'DD':>7} {'PF':>6} {'R$/dia':>9}")
        print("─" * 130)
        for i, r in enumerate(ranking, 1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i:>2}"
            print(f"{medal:>2} {r['symbol']:<7} {r['tf']:<4} {r['strategy']:<14} "
                  f"{r['ret']:>+6.2f}% {r['trades']:>6d}  "
                  f"{r['wr']:>5.1f}% {r['sharpe']:>6.2f}  "
                  f"{r['dd']:>6.2f}% {r['pf']:>5.2f} "
                  f"R${r['avg_daily']:>+8.1f}")

        # MELHOR POR ATIVO/TF
        print("\n" + "═" * 130)
        print("  🏆 MELHOR POR ATIVO/TIMEFRAME")
        print("═" * 130)
        for r in all_results:
            sub = [x for x in ranking if x["symbol"] == r["symbol"] and x["tf"] == r["tf"]]
            if sub:
                best = max(sub, key=lambda x: x["ret"])
                print(f"\n  {best['symbol']} {best['tf']}:")
                print(f"    🥇 {best['strategy']:<14} ret={best['ret']:+.2f}%  trades={best['trades']}  "
                      f"WR={best['wr']:.1f}%  sharpe={best['sharpe']:.2f}  "
                      f"R${best['avg_daily']:+.1f}/dia")

        # H1 vs M15 vs M5
        print("\n" + "═" * 130)
        print("  ⏰ M5 vs M15 — QUAL É MELHOR?")
        print("═" * 130)
        for sym in ["WIN$", "WDO$"]:
            sub = [x for x in ranking if x["symbol"] == sym]
            for strat in STRATEGIES:
                m5 = next((x for x in sub if x["tf"] == "M5" and x["strategy"] == strat), None)
                m15 = next((x for x in sub if x["tf"] == "M15" and x["strategy"] == strat), None)
                if m5 and m15:
                    w = "M5" if m5["ret"] > m15["ret"] else "M15"
                    print(f"  {sym} {strat:<14} M5={m5['ret']:+.2f}%  M15={m15['ret']:+.2f}%  → {w}")

        # CONCLUSÕES
        print("\n" + "═" * 130)
        print("  🧠 CONCLUSÕES")
        print("═" * 130)

        profitable = [x for x in ranking if x["ret"] > 0]
        print(f"\n  💰 Estratégias lucrativas: {len(profitable)}/{len(ranking)}")
        for p in profitable[:5]:
            print(f"    ✅ {p['symbol']} {p['tf']} {p['strategy']} — {p['ret']:+.2f}% ({p['trades']} trades, R${p['avg_daily']:+.0f}/dia)")

        losing = [x for x in ranking if x["ret"] <= 0]
        print(f"\n  📉 Estratégias negativas: {len(losing)}/{len(ranking)}")
        for l in losing[:3]:
            print(f"    ❌ {l['symbol']} {l['tf']} {l['strategy']} — {l['ret']:+.2f}%")

        if profitable:
            best_all = profitable[0]
            print(f"\n  🏆 CAMPEÃ: {best_all['symbol']} {best_all['tf']} {best_all['strategy']}")
            print(f"     Retorno: {best_all['ret']:+.2f}% em {best_all['days']} dias ({best_all['ret']/best_all['days']*100 if best_all['days'] else 0:+.2f}%/dia)")
            print(f"     Sharpe: {best_all['sharpe']:.2f}  |  PF: {best_all['pf']:.2f}  |  R${best_all['avg_daily']:+.0f}/dia")

        # Salvar CSV
        out = Path("/tmp/intraday_backtest.csv")
        pd.DataFrame(ranking).to_csv(out, index=False)
        print(f"\n  💾 CSV: {out}")

    print("\n" + "═" * 110 + "\n")


if __name__ == "__main__":
    run()
