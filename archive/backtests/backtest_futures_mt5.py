"""
Backtest completo de índices (WIN$ e WDO$) via MT5.
Timeframes: M15 e H1. 4 estratégias.
Contratos futuros B3 com multiplicador correto.

Uso:
  PYTHONPATH=./agent ./agent/venv/bin/python backtest_futures_mt5.py [n_bars]
"""

import sys, csv, io, subprocess, os
from datetime import datetime
from pathlib import Path
import numpy as np, pandas as pd

WINE_PYTHON = os.path.expanduser("~/.wine/drive_c/Python311/python.exe")
FETCH_SCRIPT = os.path.join(os.path.dirname(__file__), "mt5_fetch.py")

SYMBOLS = ["WIN$", "WDO$"]
TIMEFRAMES = ["H1", "M15"]

# Contratos B3 — 1 contrato = X pontos
CONTRACT_SPECS = {
    "WIN$": {"mult": 0.20, "name": "Mini Índice B3", "min_margin_per_contract": 5000, "tick": 5},
    "WDO$": {"mult": 10.0,  "name": "Mini Dólar B3",  "min_margin_per_contract": 3000, "tick": 0.5},
}


def fetch_mt5(symbol, tf, n_bars=2000):
    cmd = ["wine", WINE_PYTHON, FETCH_SCRIPT, "rates", symbol, tf, str(n_bars)]
    env = {**os.environ, "WINEDEBUG": "-all"}
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    if r.returncode != 0 or not r.stdout.strip():
        return pd.DataFrame()
    reader = csv.reader(io.StringIO(r.stdout.strip()))
    headers = next(reader)
    rows = [r for r in reader if r]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=headers)
    for col in ["open","high","low","close","tick_volume","spread","real_volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["time"] = pd.to_datetime(df["time"].astype(int), unit="s")
    df = df.set_index("time").rename(columns={"tick_volume":"tickvol","real_volume":"volume"})
    return df[["open","high","low","close","tickvol","volume"]].dropna(subset=["close"])


def sma_signals(df, fast=20, slow=50):
    sig = (df["close"].rolling(fast).mean() > df["close"].rolling(slow).mean()).astype(int)
    return sig.shift(1).fillna(0)

def rsi_signals(df, period=14, low=30, high=70):
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = -delta.where(delta < 0, 0).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[rsi < low] = 1
    sig[rsi > high] = 0
    return sig.shift(1).fillna(0).ffill().fillna(0)

def bollinger_signals(df, period=20, std=2.0):
    sma = df["close"].rolling(period).mean()
    sd = df["close"].rolling(period).std()
    lower, upper = sma - std * sd, sma + std * sd
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[df["close"] < lower] = 1
    sig[df["close"] > upper] = 0
    return sig.shift(1).fillna(0).ffill().fillna(0)

def momentum_signals(df, lookback=60, sma_filter=200):
    ret = df["close"].pct_change(lookback)
    if len(df) >= sma_filter:
        above = df["close"] > df["close"].rolling(sma_filter).mean()
    else:
        above = True
    return ((ret > 0) & above).astype(int).shift(1).fillna(0)

STRATEGIES = {"SMA(20,50)": sma_signals, "RSI(14)": rsi_signals, "Bollinger": bollinger_signals, "Momentum": momentum_signals}


def backtest_futures(df, signals, symbol, *, capital=100_000.0, slippage_pts=1, commission_per_contract=2.5):
    """
    Backtest para contratos futuros B3.
    
    capital: em R$
    slippage_pts: pontos de deslizamento por trade (WIN=5pts, WDO=0.5pts)
    commission_per_contract: custo por contrato rodado (em R$)
    """
    spec = CONTRACT_SPECS[symbol]
    mult = spec["mult"]
    margin = spec["min_margin_per_contract"]
    
    if len(signals) != len(df):
        return {"ok": False, "reason": "tamanho diferente"}

    cash = capital
    position = 0  # número de contratos (+long, -short)
    entry_price = 0.0
    entry_date = None
    equity_list = []
    n_trades, n_wins, n_long, n_short = 0, 0, 0, 0
    gross_profit, gross_loss = 0.0, 0.0
    trade_log = []

    for date, row in df.iterrows():
        price = float(row["close"])
        target_pos = int(signals.loc[date])
        
        # PnL da posição aberta (mark-to-market)
        if position > 0:
            unrealized = (price - entry_price) * position * mult
        elif position < 0:
            unrealized = (entry_price - price) * abs(position) * mult
        else:
            unrealized = 0
        equity_list.append(cash + unrealized + position * margin)  # incluir margem reservada

        if target_pos == 1 and position == 0:
            # Quantos contratos cabem no capital?
            margin_needed = price * mult  # 1 contrato = preço x multiplicador
            max_contracts = int(cash / (margin_needed + margin))  # margem + depósito
            qty = max(1, min(max_contracts, 5))  # min 1, max 5 contratos
            cost = qty * commission_per_contract + qty * slippage_pts * mult
            if cash >= margin_needed * qty + cost + qty * margin:
                cash -= qty * margin + cost  # reserva margem
                position = qty
                entry_price = price
                entry_date = date
                n_long += qty

        elif target_pos == 0 and position != 0:
            # Fecha posição
            slippage_cost = abs(slippage_pts) * mult * abs(position)
            comm_cost = commission_per_contract * abs(position)
            if position > 0:
                realized = (price - entry_price) * position * mult - slippage_cost - comm_cost
            else:
                realized = (entry_price - price) * abs(position) * mult - slippage_cost - comm_cost
            
            cash += position * margin + realized  # devolve margem + PnL
            n_trades += abs(position)
            if realized > 0:
                n_wins += abs(position)
                gross_profit += realized
            else:
                gross_loss += abs(realized)
            
            trade_log.append({
                "type": "LONG" if position > 0 else "SHORT",
                "entry": str(entry_date), "exit": str(date),
                "entry_p": entry_price, "exit_p": price,
                "contracts": abs(position), "pnl": realized,
                "pnl_pct": (realized / (abs(position) * entry_price * mult)) * 100,
            })
            position = 0
            entry_price = 0

    # Fecha posição aberta no último preço
    if position != 0:
        last = float(df["close"].iloc[-1])
        slippage_cost = abs(slippage_pts) * mult * abs(position)
        comm_cost = commission_per_contract * abs(position)
        if position > 0:
            realized = (last - entry_price) * position * mult - slippage_cost - comm_cost
        else:
            realized = (entry_price - last) * abs(position) * mult - slippage_cost - comm_cost
        cash += position * margin + realized
        n_trades += abs(position)
        if realized > 0:
            n_wins += abs(position)
            gross_profit += realized
        else:
            gross_loss += abs(realized)
        trade_log.append({
            "type": "LONG" if position > 0 else "SHORT",
            "entry": str(entry_date), "exit": str(df.index[-1]),
            "entry_p": entry_price, "exit_p": last,
            "contracts": abs(position), "pnl": realized,
            "pnl_pct": (realized / (abs(position) * entry_price * mult)) * 100,
        })
        position = 0

    # Buy & Hold (apenas long, 1 contrato)
    p0 = float(df["close"].iloc[0])
    p1 = float(df["close"].iloc[-1])
    bh_pnl = (p1 - p0) * mult  # 1 contrato

    eq = pd.Series(equity_list, index=df.index[:len(equity_list)])
    total_return = (cash - capital) / capital * 100
    bh_return = bh_pnl / capital * 100

    # Período em anos
    freq = df.index.inferred_freq if hasattr(df.index, 'inferred_freq') else None
    if freq and '15T' in str(freq):
        n_years = max(len(df) / (4 * 252), 0.01)
    else:
        n_years = max(len(df) / (252 * 6), 0.01)

    cagr = ((cash / capital) ** (1 / n_years) - 1) * 100 if cash > 0 else -100
    dd = (eq - eq.cummax()) / eq.cummax()
    max_dd = dd.min() * 100
    rets = eq.pct_change().fillna(0)
    sharpe = (rets.mean() / rets.std()) * np.sqrt(252 * 6) if rets.std() > 0 else 0
    calmar = cagr / abs(max_dd) if max_dd != 0 else 0
    pf = gross_profit / gross_loss if gross_loss > 0 else (999 if gross_profit > 0 else 0)

    # Média de PnL por trade
    avg_pnl = sum(t["pnl"] for t in trade_log) / len(trade_log) if trade_log else 0

    return {
        "ok": True, "trades": n_trades, "wins": n_wins,
        "win_rate": (n_wins / n_trades * 100) if n_trades else 0,
        "total_return": total_return, "cagr": cagr, "sharpe": sharpe,
        "max_dd": max_dd, "calmar": calmar, "profit_factor": pf,
        "bh_return": bh_return, "alpha": total_return - bh_return,
        "avg_pnl": avg_pnl, "trade_log": trade_log,
    }


def live_state(df):
    close = df["close"]
    state = {}

    if len(close) >= 50:
        sma20, sma50 = close.rolling(20).mean(), close.rolling(50).mean()
        diff_now = sma20.iloc[-1] - sma50.iloc[-1]
        diff_prev = sma20.iloc[-2] - sma50.iloc[-2]
        if diff_now > 0 and diff_prev <= 0:
            state["sma"] = ("CRUZOU ↑", "buy")
        elif diff_now > 0:
            state["sma"] = ("em alta", "hold")
        else:
            state["sma"] = ("em baixa", "sell")
    else:
        state["sma"] = ("dados insuf.", "neutral")

    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = -delta.where(delta < 0, 0).rolling(14).mean()
    rsi = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))
    rsi_now = float(rsi.iloc[-1])
    if rsi_now < 30:
        state["rsi"] = (f"RSI {rsi_now:.0f} SOBREVENDIDO", "buy")
    elif rsi_now > 70:
        state["rsi"] = (f"RSI {rsi_now:.0f} SOBRECOMPPRADO", "sell")
    else:
        state["rsi"] = (f"RSI {rsi_now:.0f}", "neutral")

    sma, sd = close.rolling(20).mean(), close.rolling(20).std()
    lower, upper = sma - 2*sd, sma + 2*sd
    pct_b = (close.iloc[-1] - lower.iloc[-1]) / (upper.iloc[-1] - lower.iloc[-1])
    if pct_b < 0:
        state["bb"] = (f"%B {pct_b:.2f} ABAIXO", "buy")
    elif pct_b > 1:
        state["bb"] = (f"%B {pct_b:.2f} ACIMA", "sell")
    else:
        state["bb"] = (f"%B {pct_b:.2f}", "neutral")

    if len(close) >= 60:
        ret_60 = close.iloc[-1] / close.iloc[-60] - 1
        if len(close) >= 200:
            above = close.iloc[-1] > close.rolling(200).mean().iloc[-1]
        else:
            above = None
        if ret_60 > 0 and (above is None or above):
            state["mom"] = (f"+{ret_60*100:.1f}% 60d" + ("" if above is None else " >SMA200"), "buy")
        else:
            state["mom"] = (f"{ret_60*100:.1f}% 60d", "sell")
    else:
        state["mom"] = ("dados insuf.", "neutral")

    return state


def run(n_bars=2000):
    print("\n" + "═" * 110)
    print("  🚀 BACKTEST FUTUROS B3 — WIN$ + WDO$ — M15 + H1")
    print("  " + "─" * 106)
    print(f"  Dados: MT5 (XP Investimentos) | Capital: R$ 100.000 | Contratos: Mini (WIN=0,2 pts, WDO=US$10)")
    print(f"  Slippage: WIN 1 tick (R$1), WDO 0.5 tick (R$5) | Comissão: R$2,50/contrato")
    print("═" * 110)

    all_results = []

    for sym in SYMBOLS:
        for tf in TIMEFRAMES:
            spec = CONTRACT_SPECS[sym]
            print(f"\n📡 {sym} ({spec['name']}) {tf} — baixando {n_bars} barras...")
            df = fetch_mt5(sym, tf, n_bars)
            if df.empty:
                print(f"  ❌ Sem dados")
                continue

            p0, p1 = float(df["close"].iloc[0]), float(df["close"].iloc[-1])
            ret_total = (p1 / p0 - 1) * 100
            print(f"  ✅ {len(df)} barras  {df.index[0]} → {df.index[-1]}")
            print(f"     {p0:.2f} → {p1:.2f} ({ret_total:+.2f}%)")

            row = {"symbol": sym, "tf": tf, "bars": len(df),
                   "date_start": str(df.index[0]), "date_end": str(df.index[-1]),
                   "p0": p0, "p1": p1, "ret_bh": ret_total}

            slippage_pts = spec["tick"]
            print(f"\n  📊 ESTRATÉGIAS {sym} {tf}:")
            print(f"  {'Estratégia':<14} {'Retorno':>9} {'Trades':>7} {'WinRate':>8} {'Sharpe':>7} {'MaxDD':>8} {'Alpha':>8} {'PF':>6} {'AvgPnL':>9}")
            print(f"  {'─'*14} {'─'*9} {'─'*7} {'─'*8} {'─'*7} {'─'*8} {'─'*8} {'─'*6} {'─'*9}")
            
            bh_r = ret_total * spec["mult"] / 100 * 100  # normalizar
            print(f"  {'Buy & Hold':<14} {spec['mult']*ret_total/100*100:>+8.2f}% {'(1cto)':>7} {'—':>8} {'—':>7} {'—':>8} {'—':>8} {'—':>6} {'—':>9}")

            for name, fn in STRATEGIES.items():
                sig = fn(df)
                r = backtest_futures(df, sig, sym, slippage_pts=slippage_pts)
                if r["ok"]:
                    key = name.strip()
                    row[f"{key}_ret"] = r["total_return"]
                    row[f"{key}_trades"] = r["trades"]
                    row[f"{key}_wr"] = r["win_rate"]
                    row[f"{key}_sharpe"] = r["sharpe"]
                    row[f"{key}_dd"] = r["max_dd"]
                    row[f"{key}_alpha"] = r["alpha"]
                    row[f"{key}_pf"] = r["profit_factor"]
                    row[f"{key}_avg"] = r["avg_pnl"]
                    print(f"  {name:<14} {r['total_return']:>+8.2f}% "
                          f"{r['trades']:>6d}  "
                          f"{r['win_rate']:>6.1f}%  "
                          f"{r['sharpe']:>6.2f}  "
                          f"{r['max_dd']:>6.2f}%  "
                          f"{r['alpha']:>+6.2f}%  "
                          f"{r['profit_factor']:>5.2f} "
                          f"R${r['avg_pnl']:>+8.1f}")

            all_results.append(row)

            # Sinais ao vivo
            state = live_state(df)
            buys = sum(1 for _, (_, t) in state.items() if t == "buy")
            sells = sum(1 for _, (_, t) in state.items() if t == "sell")
            neutral = 4 - buys - sells

            print(f"\n  🔴🟢 SINAIS AO VIVO ({tf}) — {sym} @ {p1:.2f}")
            def fmt(key):
                txt, tag = state[key]
                if tag == "buy": return f"🟢 {txt:<22}"
                elif tag == "sell": return f"🔴 {txt:<22}"
                return f"⚪ {txt:<22}"
            print(f"  SMA:   {fmt('sma')}  RSI: {fmt('rsi')}")
            print(f"  BB:    {fmt('bb')}   Mom: {fmt('mom')}")
            print(f"  Score: {buys}🟢 / {sells}🔴 / {neutral}⚪")

            if buys >= 2: print(f"  ✅ COMPRA FORTE ({buys}/4)")
            elif buys == 1 and sells == 0: print(f"  🟡 COMPRA MODERADA")
            elif sells >= 2: print(f"  ❌ VENDA FORTE ({sells}/4)")
            elif sells == 1 and buys == 0: print(f"  🟠 VENDA MODERADA")
            else: print(f"  ⚪ NEUTRO")

    # ===== TABELA FINAL =====
    if all_results:
        print("\n\n" + "═" * 130)
        print("  📋 TABELA COMPARATIVA FINAL")
        print("═" * 130)
        print(f"\n{'Ativo':<7} {'TF':<4} {'Barras':>6} {'Ret%':>7} │ {'SMA':>9} {'RSI':>9} {'BB':>9} {'Mom':>9} │ {'B&H':>7} {'Melhor':>12} {'Melhor Ret':>10}")
        print("─" * 130)
        for r in all_results:
            strats = {}
            for name in STRATEGIES:
                key = name.strip()
                if f"{key}_ret" in r:
                    strats[name] = r[f"{key}_ret"]
            best = max(strats, key=strats.get, default="—") if strats else "—"
            best_ret = strats.get(best, 0)
            print(f"{r['symbol']:<7} {r['tf']:<4} {r['bars']:>6} {r['ret_bh']:>+6.2f}% │ "
                  f"{strats.get('SMA(20,50)', 0):>+8.2f}% "
                  f"{strats.get('RSI(14)', 0):>+8.2f}% "
                  f"{strats.get('Bollinger', 0):>+8.2f}% "
                  f"{strats.get('Momentum', 0):>+8.2f}% │ "
                  f"{r['ret_bh']:>+6.2f}% "
                  f"{best:>12} "
                  f"{best_ret:>+9.2f}%")

        print("\n" + "═" * 130)
        print("  🧠 ANÁLISE CRUZADA")
        print("═" * 130)

        print("\n  📈 MELHOR POR ATIVO/TF:")
        for r in all_results:
            strats = {}
            for name in STRATEGIES:
                key = name.strip()
                if f"{key}_ret" in r:
                    strats[name] = (r[f"{key}_ret"], r.get(f"{key}_trades",0), r.get(f"{key}_wr",0), r.get(f"{key}_sharpe",0))
            best = max(strats, key=lambda k: strats[k][0])
            ret, tr, wr, sh = strats[best]
            print(f"    {r['symbol']} {r['tf']:<4} → {best:<12}  ret={ret:+.2f}%  trades={tr}  WR={wr:.1f}%  sharpe={sh:.2f}")

        print("\n  ⏰ H1 vs M15:")
        for sym in SYMBOLS:
            h1 = next((r for r in all_results if r["symbol"] == sym and r["tf"] == "H1"), None)
            m15 = next((r for r in all_results if r["symbol"] == sym and r["tf"] == "M15"), None)
            if h1 and m15:
                print(f"\n    {sym}:")
                for name in STRATEGIES:
                    key = name.strip()
                    h1r = h1.get(f"{key}_ret", 0)
                    m15r = m15.get(f"{key}_ret", 0)
                    w = "H1" if h1r > m15r else "M15"
                    print(f"      {name:<14} H1={h1r:+.2f}%  M15={m15r:+.2f}%  → {w}")

        print("\n  💰 WIN$ vs WDO$:")
        for tf in TIMEFRAMES:
            win = next((r for r in all_results if r["symbol"] == "WIN$" and r["tf"] == tf), None)
            wdo = next((r for r in all_results if r["symbol"] == "WDO$" and r["tf"] == tf), None)
            if win and wdo:
                w_spec = CONTRACT_SPECS["WIN$"]
                d_spec = CONTRACT_SPECS["WDO$"]
                win_bh_r = win["ret_bh"] * w_spec["mult"] / 100
                wdo_bh_r = wdo["ret_bh"] * d_spec["mult"] / 100
                print(f"    {tf}: WIN$ BH={win_bh_r:+.2f} R$100k  |  WDO$ BH={wdo_bh_r:+.2f} R$100k")

        # Conclusões
        print("\n" + "═" * 130)
        print("  🎯 CONCLUSÕES")
        print("═" * 130)

        # Best overall
        best_overall = max(all_results, key=lambda r: r.get("Bollinger_ret", r.get("SMA(20,50)_ret", -999)))
        print(f"\n  1. Melhor resultado absoluto: {best_overall['symbol']} {best_overall['tf']}")

        # Bollinger best across all
        for name in STRATEGIES:
            key = name.strip()
            vals = [(r["symbol"], r["tf"], r.get(f"{key}_ret", -999), r.get(f"{key}_sharpe", -999)) for r in all_results if f"{key}_ret" in r]
            if vals:
                best = max(vals, key=lambda x: x[2])
                print(f"  2. {name:<14} melhor em: {best[0]} {best[1]} (ret {best[2]:+.2f}%, sharpe {best[3]:.2f})")

        out = Path("/tmp/futures_backtest_mt5.csv")
        pd.DataFrame(all_results).to_csv(out, index=False)
        print(f"\n  💾 CSV: {out}")

    print("\n" + "═" * 110 + "\n")


if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 2000)
