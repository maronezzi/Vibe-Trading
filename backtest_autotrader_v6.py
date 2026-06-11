"""
backtest_autotrader_v6.py — Replica exatamente a lógica do vt_autotrader.py.

Diferenças vs v5:
1. SL = 1.0x ATR (não 1.5x)
2. Trailing: ativa 1.5x ATR, distância 0.5x ATR (sem time-based)
3. Sem profit lock (breakeven)
4. Sem filtro ADX
5. Sem saída por sinal — só trailing/SL/16:45
6. Volume = 1 contrato (não 3)
7. SL mínimo: WIN=100pts, WDO=200pts, arredondado múltiplo de 5
8. VWAP(20): buy se close > vwap * 1.003, sell se close < vwap * 0.997
9. Máx 1 posição por símbolo por timeframe
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
COMMISSION = 2.5  # por contrato

# Config do autotrader
VWAP_PERIOD = 20
VWAP_BUY_THRESHOLD = 1.003
VWAP_SELL_THRESHOLD = 0.997
ATR_PERIOD = 14
SL_ATR_MULT = 1.0
TRAIL_ACTIVATE = 1.5
TRAIL_DISTANCE = 0.5
MAX_CT = 1  # 1 contrato
SL_MIN_WIN = 100  # pontos
SL_MIN_WDO = 200  # pontos


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


def calc_vwap(df, period=20):
    """VWAP rolling — igual ao autotrader."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["tick_volume"].replace(0, 1)
    return (typical * vol).rolling(period).sum() / vol.rolling(period).sum()


def backtest(df, symbol, *, capital=100_000.0):
    """Replica exatamente a lógica do vt_autotrader.py."""
    spec = CONTRACT_SPECS[symbol]
    mult, margin, slip_r = spec["mult"], spec["margin"], spec["slip_r"]
    is_win = "WIN" in symbol
    is_wdo = "WDO" in symbol

    atr = calc_atr(df, ATR_PERIOD)
    vwap = calc_vwap(df, VWAP_PERIOD)

    cash = capital
    pos = 0         # 0=flat, 1=long, -1=short
    ep = 0.0        # entry price
    e_date = None
    e_atr = 0.0     # ATR na entrada
    best = 0.0      # melhor preço (max pra long, min pra short)
    sl_price = 0.0  # nível SL atual
    trail_on = False
    sl_pts = 0      # SL original em pontos
    bars_in_trade = 0

    equity, trade_log, daily_pnl = [], [], []
    n_trades = n_wins = n_long = n_short = 0
    n_sl = n_trail = n_close = 0
    gross_win = 0.0
    gross_loss_val = 0.0
    daily_pnl_dict = {}

    def _close(price, reason):
        nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, e_atr
        nonlocal n_trades, n_wins, n_long, n_short, n_sl, n_trail, n_close
        nonlocal gross_win, gross_loss_val, bars_in_trade

        if pos == 0:
            return

        sl_cost = slip_r * MAX_CT
        comm = COMMISSION * MAX_CT

        if pos == 1:
            pnl = (price - ep) * mult * MAX_CT - sl_cost - comm
            n_long += 1
        else:
            pnl = (ep - price) * mult * MAX_CT - sl_cost - comm
            n_short += 1

        cash += margin * MAX_CT + pnl
        n_trades += 1

        if reason == "SL": n_sl += 1
        elif reason == "TRAIL": n_trail += 1
        elif reason == "1645": n_close += 1

        if pnl > 0:
            n_wins += 1; gross_win += pnl
        else:
            gross_loss_val += abs(pnl)

        trade_log.append({
            "type": "LONG" if pos == 1 else "SHORT",
            "entry": str(e_date), "exit": "",
            "ep": ep, "xp": price, "pnl": pnl, "reason": reason,
            "bars": bars_in_trade, "sl_pts": sl_pts,
        })
        daily_pnl.append(pnl)

        # daily PnL tracking
        d = e_date.date() if hasattr(e_date, 'date') else e_date
        if d not in daily_pnl_dict:
            daily_pnl_dict[d] = 0.0
        daily_pnl_dict[d] += pnl

        pos = 0; ep = 0; best = 0; sl_price = 0; trail_on = False
        bars_in_trade = 0

    def _open(direction, price, date, cur_atr):
        nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, e_atr, sl_pts, bars_in_trade

        if pos != 0:
            return False  # já tem posição (1 por símbolo)

        # Calcular SL
        raw_sl = int(cur_atr * SL_ATR_MULT)
        if is_win:
            raw_sl = max(raw_sl, SL_MIN_WIN)
        elif is_wdo:
            raw_sl = max(raw_sl, SL_MIN_WDO)
        raw_sl = ((raw_sl + 4) // 5) * 5  # múltiplo de 5

        cost = slip_r * MAX_CT + COMMISSION * MAX_CT
        if cash >= margin * MAX_CT + cost:
            cash -= margin * MAX_CT + cost
            pos = 1 if direction == "BUY" else -1
            ep = price; e_date = date; e_atr = cur_atr; sl_pts = raw_sl
            best = price; trail_on = False
            if pos == 1:
                sl_price = price - raw_sl
            else:
                sl_price = price + raw_sl
            bars_in_trade = 0
            return True
        return False

    for i, (date, row) in enumerate(df.iterrows()):
        price = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        hour = int(row["hour"])
        minute = int(row["minute"])
        cur_atr = float(atr.iloc[i]) if i > 0 and not pd.isna(atr.iloc[i]) else 0
        cur_vwap = float(vwap.iloc[i]) if not pd.isna(vwap.iloc[i]) else 0

        # Mark-to-market
        if pos == 1:
            eq_val = cash + (price - ep) * mult * MAX_CT + margin * MAX_CT
        elif pos == -1:
            eq_val = cash + (ep - price) * mult * MAX_CT + margin * MAX_CT
        else:
            eq_val = cash
        equity.append(eq_val)

        # ===== SEM POSIÇÃO: VERIFICAR ENTRADA =====
        if pos == 0:
            if cur_atr > 0 and cur_vwap > 0:
                buy_thresh = cur_vwap * VWAP_BUY_THRESHOLD
                sell_thresh = cur_vwap * VWAP_SELL_THRESHOLD

                if price > buy_thresh:
                    _open("BUY", price, date, cur_atr)
                elif price < sell_thresh:
                    _open("SELL", price, date, cur_atr)
            continue

        # ===== POSIÇÃO ABERTA =====
        bars_in_trade += 1

        # Atualizar melhor preço
        if pos == 1:
            best = max(best, high)
        elif pos == -1:
            best = min(best, low) if best > 0 else low

        # Lucro em pontos
        if pos == 1:
            profit_pts = best - ep
        else:
            profit_pts = ep - best

        # Ativar trailing?
        if not trail_on and e_atr > 0 and profit_pts >= TRAIL_ACTIVATE * e_atr:
            trail_on = True

        # Calcular trailing
        if trail_on and e_atr > 0:
            trail_dist = TRAIL_DISTANCE * e_atr
            if pos == 1:
                new_sl = best - trail_dist
                if new_sl > sl_price:
                    sl_price = new_sl
            elif pos == -1:
                new_sl = best + trail_dist
                if new_sl < sl_price:
                    sl_price = new_sl

        # SL fixo
        if sl_price > 0:
            if pos == 1 and low <= sl_price:
                _close(sl_price, "SL"); continue
            elif pos == -1 and high >= sl_price:
                _close(sl_price, "SL"); continue

        # 16:45
        if hour > CLOSE_HOUR or (hour == CLOSE_HOUR and minute >= CLOSE_MINUTE):
            _close(price, "1645"); continue

    # Força fechar
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

    # Daily PnL para Sharpe
    daily_vals = list(daily_pnl_dict.values())
    avg_daily = sum(t["pnl"] for t in trade_log) / n_days if n_days else 0

    if len(daily_vals) > 1:
        sharpe = np.mean(daily_vals) / np.std(daily_vals) * np.sqrt(252) if np.std(daily_vals) > 0 else 0
    else:
        sharpe = 0

    # Max drawdown
    eq_arr = np.array(equity)
    running_max = np.maximum.accumulate(eq_arr)
    drawdowns = (running_max - eq_arr) / running_max * 100
    max_dd = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0

    pf = gross_win / gross_loss_val if gross_loss_val > 0 else (999 if gross_win > 0 else 0)
    wr = (n_wins / n_trades * 100) if n_trades else 0

    wins_p = [t["pnl"] for t in trade_log if t["pnl"] > 0]
    losses_p = [t["pnl"] for t in trade_log if t["pnl"] <= 0]
    avg_win = np.mean(wins_p) if wins_p else 0
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
        "n_sl": n_sl, "n_trail": n_trail, "n_close": n_close,
        "avg_bars": avg_bars,
        "reasons": pnl_by_reason,
        "trade_log": trade_log, "equity": eq,
        "daily_pnl": daily_pnl_dict,
    }


def run():
    print("\n" + "═" * 100)
    print("  🤖 BACKTEST v6 — REPLICA DO AUTOTRADER (vt_autotrader.py)")
    print("  " + "─" * 96)
    print("  ✅ VWAP(20): buy > 1.003, sell < 0.997")
    print("  ✅ SL: 1.0x ATR (min WIN=100, WDO=200, mult 5)")
    print("  ✅ Trailing: ativa 1.5x ATR, distância 0.5x ATR")
    print("  ✅ Sem profit lock, sem ADX, sem saída por sinal")
    print("  ✅ 1 contrato | Fecha 16:45 BRT")
    print("═" * 100)

    combos = [
        ("WIN$", "M5", 500),
        ("WIN$", "M15", 500),
        ("WDO$", "M5", 500),
        ("WDO$", "M15", 500),
    ]
    all_results = []

    for sym, tf, n_bars in combos:
        spec = CONTRACT_SPECS[sym]
        print(f"\n📡 {sym} ({spec['name']}) {tf} — {n_bars} barras...")
        df = fetch(sym, tf, n_bars)
        if df.empty:
            print("  ❌ Sem dados"); continue

        p0, p1 = float(df["close"].iloc[0]), float(df["close"].iloc[-1])
        n_days = df["date"].nunique()
        atr_avg = calc_atr(df, ATR_PERIOD).mean()

        print(f"  ✅ {len(df)} barras, {n_days} dias | {df.index[0].strftime('%d/%m')} → {df.index[-1].strftime('%d/%m')}")
        print(f"     {p0:.2f} → {p1:.2f} ({(p1/p0-1)*100:+.2f}%)")
        print(f"     ATR médio: {atr_avg:.1f} pts")

        r = backtest(df, sym)
        if r["ok"]:
            print(f"\n  📊 VWAP(20) — {sym} {tf}")
            print(f"  {'─' * 60}")
            print(f"  Retorno: {r['ret']:+.2f}%")
            print(f"  Trades:  {r['trades']} (Long: {r['long']}, Short: {r['short']})")
            print(f"  Win Rate: {r['wr']:.1f}%")
            print(f"  Sharpe:   {r['sharpe']:.2f}")
            print(f"  Max DD:   {r['max_dd']:.2f}%")
            print(f"  PF:       {r['pf']:.2f}")
            print(f"  Avg Win:  R$ {r['avg_win']:+.1f}")
            print(f"  Avg Loss: R$ {r['avg_loss']:+.1f}")
            print(f"  Payoff:   {r['payoff']:.2f}")
            print(f"  R$/dia:   R$ {r['avg_daily']:+.1f}")
            print(f"  Avg Barras: {r['avg_bars']:.1f}")
            print(f"\n  Motivos de saída:")
            for reason, data in sorted(r["reasons"].items(), key=lambda x: x[1]["pnl"], reverse=True):
                pct = data["count"] / r["trades"] * 100 if r["trades"] else 0
                wr_r = data["wins"] / data["count"] * 100 if data["count"] else 0
                print(f"    {reason:<6}: {data['count']:>3} ({pct:4.1f}%) WR {wr_r:.0f}% PnL R${data['pnl']:+.0f}")

            all_results.append({
                "symbol": sym, "tf": tf, "strategy": "VWAP(20)",
                **r,
                "reasons": r["reasons"],
                "daily_pnl": r["daily_pnl"],
            })

    # ===== RANKING =====
    if all_results:
        ranking = sorted(all_results, key=lambda x: x["ret"], reverse=True)

        print("\n\n" + "═" * 100)
        print("  📋 RANKING GERAL — v6 (Autotrader)")
        print("═" * 100)
        print(f"\n{'#':>2} {'Ativo':<7} {'TF':<4} {'Ret%':>7} {'T':>4} {'WR':>6} {'Sharpe':>7} {'DD':>7} {'PF':>6} {'R$/dia':>9}")
        print("─" * 100)
        for i, r in enumerate(ranking, 1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i:>2}"
            print(f"{medal:>2} {r['symbol']:<7} {r['tf']:<4} "
                  f"{r['ret']:>+6.2f}% {r['trades']:>3}  "
                  f"{r['wr']:>5.1f}% {r['sharpe']:>6.2f}  "
                  f"{r['max_dd']:>6.2f}% {r['pf']:>5.2f} "
                  f"R${r['avg_daily']:>+8.1f}")

        # Comparação com estudo anterior (v5)
        print("\n\n" + "═" * 100)
        print("  📈 COMPARAÇÃO v6 (Autotrader) vs ESTUDO ANTERIOR (v5)")
        print("═" * 100)

        v5_data = {
            "WIN$ M5": {"SMA(9,21)": -1.84, "RSI(7)": -0.17, "Bollinger": 0.77, "EMA(10,30)": -2.70, "VWAP": -0.85},
            "WIN$ M15": {"SMA(9,21)": -2.41, "RSI(7)": -2.22, "Bollinger": 0.10, "EMA(10,30)": -3.39, "VWAP": -0.16},
            "WDO$ M5": {"SMA(9,21)": -3.25, "RSI(7)": -4.15, "Bollinger": -1.94, "EMA(10,30)": -3.03, "VWAP": 0.08},
            "WDO$ M15": {"SMA(9,21)": -2.19, "RSI(7)": -1.54, "Bollinger": -0.47, "EMA(10,30)": -0.63, "VWAP": -3.01},
        }

        # Melhor resultado do estudo por combo
        v5_best = {}
        for combo, strats in v5_data.items():
            best_name = max(strats, key=strats.get)
            v5_best[combo] = (best_name, strats[best_name])

        print(f"\n{'Ativo':<10} {'TF':<4} {'v5 Melhor':>14} {'v5 Ret%':>8} {'v6 Ret%':>8} {'Δ':>7} {'v5 Melhor':>12}")
        print("─" * 80)
        for r in all_results:
            key = f"{r['symbol']} {r['tf']}"
            if key in v5_best:
                v5_name, v5_ret = v5_best[key]
                v6_ret = r["ret"]
                delta = v6_ret - v5_ret
                icon = "✅" if delta > 0 else "❌" if delta < -0.5 else "➡️"
                print(f"  {r['symbol']:<7} {r['tf']:<4} {v5_name:<14} {v5_ret:>+7.2f}% {v6_ret:>+7.2f}% {delta:>+6.2f}% {icon}")

        # Lucrativos?
        profitable = [x for x in all_results if x["ret"] > 0]
        print(f"\n  💰 Lucrativos: {len(profitable)}/{len(all_results)}")
        for p in profitable:
            print(f"    ✅ {p['symbol']} {p['tf']} — {p['ret']:+.2f}% | "
                  f"Sharpe {p['sharpe']:.2f} | PF {p['pf']:.2f} | R${p['avg_daily']:+.0f}/dia")

        # Salvar CSV
        out = Path("/tmp/backtest_v6_autotrader.csv")
        rows_csv = []
        for r in all_results:
            rows_csv.append({
                "symbol": r["symbol"], "tf": r["tf"],
                "ret": r["ret"], "trades": r["trades"],
                "wr": r["wr"], "sharpe": r["sharpe"],
                "max_dd": r["max_dd"], "pf": r["pf"],
                "avg_daily": r["avg_daily"], "n_days": r["n_days"],
                "payoff": r["payoff"], "avg_bars": r["avg_bars"],
            })
        pd.DataFrame(rows_csv).to_csv(out, index=False)
        print(f"\n  💾 CSV: {out}")

    print("\n" + "═" * 100 + "\n")


if __name__ == "__main__":
    run()
