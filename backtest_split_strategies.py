"""
backtest_split_strategies.py — WDO com VWAP, WIN com Bollinger Bands.

WDO: VWAP(20) — buy > 1.003, sell < 0.997 (funciona bem em trending)
WIN: Bollinger Bands(20,2) — reversão à média em mercado choppy
"""
import sys, csv, io, subprocess, os
from pathlib import Path
import numpy as np, pandas as pd

WINE_PYTHON = os.path.expanduser("~/.wine/drive_c/Python311/python.exe")
FETCH_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mt5_fetch.py")

CLOSE_HOUR, CLOSE_MINUTE = 16, 45

CONTRACT_SPECS = {
    "WIN$": {"mult": 0.20, "name": "Mini Índice", "margin": 5000, "tick": 5, "slip_r": 1.0},
    "WDO$": {"mult": 10.0, "name": "Mini Dólar", "margin": 3000, "tick": 0.5, "slip_r": 5.0},
}
COMMISSION = 2.5

# VWAP config (WDO)
VWAP_PERIOD = 20
VWAP_BUY_THRESHOLD = 1.003
VWAP_SELL_THRESHOLD = 0.997

# Bollinger config (WIN)
BB_PERIOD = 20
BB_STD = 2.0

# Comum
ATR_PERIOD = 14
SL_ATR_MULT = 1.5
TRAIL_ACTIVATE = 1.5
TRAIL_DISTANCE = 0.5
MAX_CT = 1
SL_MIN_WIN = 100
SL_MIN_WDO = 200


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
    typical = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["tick_volume"].replace(0, 1)
    return (typical * vol).rolling(period).sum() / vol.rolling(period).sum()


def calc_bollinger(df, period=20, num_std=2.0):
    """Retorna (upper, middle, lower) das Bollinger Bands."""
    mid = df["close"].rolling(period).mean()
    std = df["close"].rolling(period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


def calc_rsi(series, period=14):
    """RSI simples."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    rs = gain / loss.replace(0, 1)
    return 100 - (100 / (1 + rs))


def backtest_vwap(df, symbol, capital=100_000.0):
    """Estratégia VWAP — para WDO."""
    spec = CONTRACT_SPECS[symbol]
    mult, margin, slip_r = spec["mult"], spec["margin"], spec["slip_r"]
    is_win = "WIN" in symbol
    is_wdo = "WDO" in symbol

    atr = calc_atr(df, ATR_PERIOD)
    vwap = calc_vwap(df, VWAP_PERIOD)

    cash = capital
    pos = 0; ep = 0.0; e_date = None; e_atr = 0.0; best = 0.0
    sl_price = 0.0; trail_on = False; sl_pts = 0; bars_in_trade = 0

    equity, trade_log = [], []
    n_trades = n_wins = n_long = n_short = 0
    n_sl = n_trail = n_close = 0
    gross_win = 0.0; gross_loss_val = 0.0
    daily_pnl_dict = {}

    def _close(price, reason):
        nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, e_atr
        nonlocal n_trades, n_wins, n_long, n_short, n_sl, n_trail, n_close
        nonlocal gross_win, gross_loss_val, bars_in_trade
        if pos == 0: return
        sl_cost = slip_r * MAX_CT; comm = COMMISSION * MAX_CT
        if pos == 1:
            pnl = (price - ep) * mult * MAX_CT - sl_cost - comm; n_long += 1
        else:
            pnl = (ep - price) * mult * MAX_CT - sl_cost - comm; n_short += 1
        cash += margin * MAX_CT + pnl; n_trades += 1
        if reason == "SL": n_sl += 1
        elif reason == "TRAIL": n_trail += 1
        elif reason == "1645": n_close += 1
        if pnl > 0: n_wins += 1; gross_win += pnl
        else: gross_loss_val += abs(pnl)
        trade_log.append({"type": "LONG" if pos == 1 else "SHORT", "ep": ep, "xp": price, "pnl": pnl, "reason": reason, "bars": bars_in_trade})
        d = e_date.date() if hasattr(e_date, 'date') else e_date
        daily_pnl_dict.setdefault(d, 0.0); daily_pnl_dict[d] += pnl
        pos = 0; ep = 0; best = 0; sl_price = 0; trail_on = False; bars_in_trade = 0

    def _open(direction, price, date, cur_atr):
        nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, e_atr, sl_pts, bars_in_trade
        if pos != 0: return False
        raw_sl = int(cur_atr * SL_ATR_MULT)
        if is_win: raw_sl = max(raw_sl, SL_MIN_WIN)
        elif is_wdo: raw_sl = max(raw_sl, SL_MIN_WDO)
        raw_sl = ((raw_sl + 4) // 5) * 5
        cost = slip_r * MAX_CT + COMMISSION * MAX_CT
        if cash >= margin * MAX_CT + cost:
            cash -= margin * MAX_CT + cost
            pos = 1 if direction == "BUY" else -1
            ep = price; e_date = date; e_atr = cur_atr; sl_pts = raw_sl
            best = price; trail_on = False
            sl_price = price - raw_sl if pos == 1 else price + raw_sl
            bars_in_trade = 0; return True
        return False

    for i, (date, row) in enumerate(df.iterrows()):
        price = float(row["close"]); high = float(row["high"]); low = float(row["low"])
        hour = int(row["hour"]); minute = int(row["minute"])
        cur_atr = float(atr.iloc[i]) if i > 0 and not pd.isna(atr.iloc[i]) else 0
        cur_vwap = float(vwap.iloc[i]) if not pd.isna(vwap.iloc[i]) else 0

        if pos == 1: eq_val = cash + (price - ep) * mult * MAX_CT + margin * MAX_CT
        elif pos == -1: eq_val = cash + (ep - price) * mult * MAX_CT + margin * MAX_CT
        else: eq_val = cash
        equity.append(eq_val)

        if pos == 0:
            if cur_atr > 0 and cur_vwap > 0:
                if price > cur_vwap * VWAP_BUY_THRESHOLD: _open("BUY", price, date, cur_atr)
                elif price < cur_vwap * VWAP_SELL_THRESHOLD: _open("SELL", price, date, cur_atr)
            continue

        bars_in_trade += 1
        if pos == 1: best = max(best, high)
        elif pos == -1: best = min(best, low) if best > 0 else low
        profit_pts = best - ep if pos == 1 else ep - best

        if not trail_on and e_atr > 0 and profit_pts >= TRAIL_ACTIVATE * e_atr:
            trail_on = True
        if trail_on and e_atr > 0:
            trail_dist = TRAIL_DISTANCE * e_atr
            if pos == 1:
                new_sl = best - trail_dist
                if new_sl > sl_price: sl_price = new_sl
            else:
                new_sl = best + trail_dist
                if new_sl < sl_price: sl_price = new_sl

        if sl_price > 0:
            if pos == 1 and low <= sl_price: _close(sl_price, "SL"); continue
            elif pos == -1 and high >= sl_price: _close(sl_price, "SL"); continue
        if hour > CLOSE_HOUR or (hour == CLOSE_HOUR and minute >= CLOSE_MINUTE):
            _close(price, "1645"); continue

    if pos != 0: _close(float(df["close"].iloc[-1]), "FORCE")

    return _build_stats(trade_log, equity, df, cash, capital, n_trades, n_wins,
                        n_long, n_short, n_sl, n_trail, n_close, gross_win, gross_loss_val, daily_pnl_dict)


def backtest_bollinger(df, symbol, capital=100_000.0):
    """Estratégia Bollinger Bands — para WIN (reversão à média)."""
    spec = CONTRACT_SPECS[symbol]
    mult, margin, slip_r = spec["mult"], spec["margin"], spec["slip_r"]
    is_win = "WIN" in symbol
    is_wdo = "WDO" in symbol

    atr = calc_atr(df, ATR_PERIOD)
    bb_upper, bb_mid, bb_lower = calc_bollinger(df, BB_PERIOD, BB_STD)
    rsi = calc_rsi(df["close"], 14)

    cash = capital
    pos = 0; ep = 0.0; e_date = None; e_atr = 0.0; best = 0.0
    sl_price = 0.0; trail_on = False; sl_pts = 0; bars_in_trade = 0

    equity, trade_log = [], []
    n_trades = n_wins = n_long = n_short = 0
    n_sl = n_trail = n_close = 0
    gross_win = 0.0; gross_loss_val = 0.0
    daily_pnl_dict = {}

    def _close(price, reason):
        nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, e_atr
        nonlocal n_trades, n_wins, n_long, n_short, n_sl, n_trail, n_close
        nonlocal gross_win, gross_loss_val, bars_in_trade
        if pos == 0: return
        sl_cost = slip_r * MAX_CT; comm = COMMISSION * MAX_CT
        if pos == 1:
            pnl = (price - ep) * mult * MAX_CT - sl_cost - comm; n_long += 1
        else:
            pnl = (ep - price) * mult * MAX_CT - sl_cost - comm; n_short += 1
        cash += margin * MAX_CT + pnl; n_trades += 1
        if reason == "SL": n_sl += 1
        elif reason == "TRAIL": n_trail += 1
        elif reason == "1645": n_close += 1
        elif reason == "MID": n_close += 1
        if pnl > 0: n_wins += 1; gross_win += pnl
        else: gross_loss_val += abs(pnl)
        trade_log.append({"type": "LONG" if pos == 1 else "SHORT", "ep": ep, "xp": price, "pnl": pnl, "reason": reason, "bars": bars_in_trade})
        d = e_date.date() if hasattr(e_date, 'date') else e_date
        daily_pnl_dict.setdefault(d, 0.0); daily_pnl_dict[d] += pnl
        pos = 0; ep = 0; best = 0; sl_price = 0; trail_on = False; bars_in_trade = 0

    def _open(direction, price, date, cur_atr):
        nonlocal cash, pos, ep, e_date, best, sl_price, trail_on, e_atr, sl_pts, bars_in_trade
        if pos != 0: return False
        raw_sl = int(cur_atr * SL_ATR_MULT)
        if is_win: raw_sl = max(raw_sl, SL_MIN_WIN)
        elif is_wdo: raw_sl = max(raw_sl, SL_MIN_WDO)
        raw_sl = ((raw_sl + 4) // 5) * 5
        cost = slip_r * MAX_CT + COMMISSION * MAX_CT
        if cash >= margin * MAX_CT + cost:
            cash -= margin * MAX_CT + cost
            pos = 1 if direction == "BUY" else -1
            ep = price; e_date = date; e_atr = cur_atr; sl_pts = raw_sl
            best = price; trail_on = False
            sl_price = price - raw_sl if pos == 1 else price + raw_sl
            bars_in_trade = 0; return True
        return False

    for i, (date, row) in enumerate(df.iterrows()):
        price = float(row["close"]); op = float(row["open"])
        high = float(row["high"]); low = float(row["low"])
        hour = int(row["hour"]); minute = int(row["minute"])
        cur_atr = float(atr.iloc[i]) if i > 0 and not pd.isna(atr.iloc[i]) else 0
        cur_bb_up = float(bb_upper.iloc[i]) if not pd.isna(bb_upper.iloc[i]) else 0
        cur_bb_mid = float(bb_mid.iloc[i]) if not pd.isna(bb_mid.iloc[i]) else 0
        cur_bb_low = float(bb_lower.iloc[i]) if not pd.isna(bb_lower.iloc[i]) else 0
        cur_rsi = float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else 50

        if pos == 1: eq_val = cash + (price - ep) * mult * MAX_CT + margin * MAX_CT
        elif pos == -1: eq_val = cash + (ep - price) * mult * MAX_CT + margin * MAX_CT
        else: eq_val = cash
        equity.append(eq_val)

        # ===== SEM POSIÇÃO: BOLLINGER REVERSÃO =====
        if pos == 0:
            if cur_atr > 0 and cur_bb_up > 0 and cur_bb_low > 0:
                # Compra: preço toca banda inferior E RSI oversold (< 35)
                if low <= cur_bb_low and cur_rsi < 35:
                    _open("BUY", price, date, cur_atr)
                # Venda: preço toca banda superior E RSI overbought (> 65)
                elif high >= cur_bb_up and cur_rsi > 65:
                    _open("SELL", price, date, cur_atr)
            continue

        # ===== POSIÇÃO ABERTA: SAÍDA NA MÉDIA (BOLLINGER MID) =====
        bars_in_trade += 1
        if pos == 1: best = max(best, high)
        elif pos == -1: best = min(best, low) if best > 0 else low
        profit_pts = best - ep if pos == 1 else ep - best

        # Trailing (mesma lógica)
        if not trail_on and e_atr > 0 and profit_pts >= TRAIL_ACTIVATE * e_atr:
            trail_on = True
        if trail_on and e_atr > 0:
            trail_dist = TRAIL_DISTANCE * e_atr
            if pos == 1:
                new_sl = best - trail_dist
                if new_sl > sl_price: sl_price = new_sl
            else:
                new_sl = best + trail_dist
                if new_sl < sl_price: sl_price = new_sl

        # SL
        if sl_price > 0:
            if pos == 1 and low <= sl_price: _close(sl_price, "SL"); continue
            elif pos == -1 and high >= sl_price: _close(sl_price, "SL"); continue

        # Take profit na banda oposta ou média
        if pos == 1 and price >= cur_bb_up:
            _close(price, "BB_UP"); continue
        elif pos == -1 and price <= cur_bb_low:
            _close(price, "BB_LOW"); continue

        # 16:45
        if hour > CLOSE_HOUR or (hour == CLOSE_HOUR and minute >= CLOSE_MINUTE):
            _close(price, "1645"); continue

    if pos != 0: _close(float(df["close"].iloc[-1]), "FORCE")
    return _build_stats(trade_log, equity, df, cash, capital, n_trades, n_wins,
                        n_long, n_short, n_sl, n_trail, n_close, gross_win, gross_loss_val, daily_pnl_dict)


def _build_stats(trade_log, equity, df, cash, capital, n_trades, n_wins,
                 n_long, n_short, n_sl, n_trail, n_close, gross_win, gross_loss_val, daily_pnl_dict):
    eq = pd.Series(equity, index=df.index[:len(equity)])
    total_ret = (cash - capital) / capital * 100
    n_days = df["date"].nunique()
    daily_vals = list(daily_pnl_dict.values())
    avg_daily = sum(t["pnl"] for t in trade_log) / n_days if n_days else 0
    if len(daily_vals) > 1:
        sharpe = np.mean(daily_vals) / np.std(daily_vals) * np.sqrt(252) if np.std(daily_vals) > 0 else 0
    else:
        sharpe = 0
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
        pnl_by_reason.setdefault(r, {"count": 0, "pnl": 0, "wins": 0})
        pnl_by_reason[r]["count"] += 1; pnl_by_reason[r]["pnl"] += t["pnl"]
        if t["pnl"] > 0: pnl_by_reason[r]["wins"] += 1
    avg_bars = np.mean([t["bars"] for t in trade_log]) if trade_log else 0
    return {
        "ok": True, "trades": n_trades, "wins": n_wins, "wr": wr,
        "long": n_long, "short": n_short, "ret": total_ret, "sharpe": sharpe,
        "max_dd": max_dd, "pf": pf, "avg_daily": avg_daily, "n_days": n_days,
        "avg_win": avg_win, "avg_loss": avg_loss, "payoff": payoff,
        "n_sl": n_sl, "n_trail": n_trail, "n_close": n_close,
        "avg_bars": avg_bars, "reasons": pnl_by_reason,
        "trade_log": trade_log, "equity": eq, "daily_pnl": daily_pnl_dict,
    }


def run():
    print("\n" + "═" * 90)
    print("  🧪 BACKTEST SPLIT — WDO: VWAP | WIN: Bollinger Bands")
    print("  " + "─" * 86)
    print("  WDO$ → VWAP(20): buy > 1.003, sell < 0.997")
    print("  WIN$ → Bollinger(20,2) + RSI(14): reversão à média")
    print("  SL: 1.0x ATR | Trailing: 1.5x/0.5x ATR | 1 contrato | Fecha 16:45")
    print("═" * 90)

    combos = [
        ("WDO$", "M5",  1000, "VWAP"),
        ("WDO$", "M15", 500,  "VWAP"),
        ("WIN$", "M5",  1000, "Bollinger"),
        ("WIN$", "M15", 500,  "Bollinger"),
    ]
    all_results = []

    for sym, tf, n_bars, strategy in combos:
        spec = CONTRACT_SPECS[sym]
        print(f"\n📡 {sym} ({spec['name']}) {tf} — {strategy} — {n_bars} barras...")
        df = fetch(sym, tf, n_bars)
        if df.empty:
            print("  ❌ Sem dados"); continue

        p0, p1 = float(df["close"].iloc[0]), float(df["close"].iloc[-1])
        n_days = df["date"].nunique()
        atr_avg = calc_atr(df, ATR_PERIOD).mean()

        print(f"  ✅ {len(df)} barras, {n_days} dias | {df.index[0].strftime('%d/%m')} → {df.index[-1].strftime('%d/%m')}")
        print(f"     {p0:.2f} → {p1:.2f} ({(p1/p0-1)*100:+.2f}%) | ATR: {atr_avg:.1f} pts")

        if strategy == "VWAP":
            r = backtest_vwap(df, sym)
        else:
            r = backtest_bollinger(df, sym)

        if r["ok"]:
            pnl_total = sum(t["pnl"] for t in r["trade_log"])
            print(f"\n  📊 {strategy} — {sym} {tf}")
            print(f"  {'─' * 60}")
            print(f"  Trades:    {r['trades']} (L:{r['long']} S:{r['short']})")
            print(f"  Win Rate:  {r['wr']:.1f}%")
            print(f"  PnL:       R$ {pnl_total:+.2f}")
            print(f"  Sharpe:    {r['sharpe']:.2f}")
            print(f"  Max DD:    {r['max_dd']:.2f}%")
            print(f"  PF:        {r['pf']:.2f}")
            print(f"  R$/dia:    R$ {r['avg_daily']:+.1f}")
            print(f"  Payoff:    {r['payoff']:.2f}")
            print(f"  Avg Barras: {r['avg_bars']:.1f}")

            print(f"\n  Saídas:")
            for reason, data in sorted(r["reasons"].items(), key=lambda x: x[1]["pnl"], reverse=True):
                pct = data["count"] / r["trades"] * 100 if r["trades"] else 0
                wr_r = data["wins"] / data["count"] * 100 if data["count"] else 0
                print(f"    {reason:<8}: {data['count']:>3} ({pct:4.1f}%) WR {wr_r:.0f}% PnL R${data['pnl']:+.0f}")

            all_results.append({"symbol": sym, "tf": tf, "strategy": strategy, **r, "pnl": pnl_total})

    # RANKING
    if all_results:
        ranking = sorted(all_results, key=lambda x: x["pnl"], reverse=True)
        print("\n\n" + "═" * 90)
        print("  📋 RANKING — ESTRATÉGIAS SPLIT")
        print("═" * 90)
        print(f"\n  {'#':>2} {'Ativo':<7} {'TF':<4} {'Strategy':<10} {'Trades':>6} {'WR':>6} {'PnL':>10} {'Sharpe':>7} {'PF':>6} {'R$/dia':>9}")
        print(f"  {'─'*75}")
        for i, r in enumerate(ranking, 1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i:>2}"
            print(f"  {medal} {r['symbol']:<7} {r['tf']:<4} {r['strategy']:<10} {r['trades']:>5}  {r['wr']:>5.1f}% "
                  f"R${r['pnl']:>+8.2f} {r['sharpe']:>6.2f} {r['pf']:>5.2f} R${r['avg_daily']:>+7.1f}")

        # Comparação com o backtest anterior (tudo VWAP)
        print(f"\n\n  📈 COMPARAÇÃO: Split vs VWAP-only")
        print(f"  {'─'*70}")
        vwap_only = {"WIN$ M5": -66.18, "WIN$ M15": -128.50, "WDO$ M5": 136.1, "WDO$ M15": 22.3}
        for r in all_results:
            key = f"{r['symbol']} {r['tf']}"
            old_pnl = vwap_only.get(key, 0)
            new_pnl = r["avg_daily"]
            delta = new_pnl - old_pnl
            icon = "✅" if delta > 0 else "❌"
            print(f"  {r['symbol']:<7} {r['tf']:<4} {r['strategy']:<10} "
                  f"VWAP: R${old_pnl:>+7.1f}/dia  Split: R${new_pnl:>+7.1f}/dia  Δ: {icon} R${delta:>+6.1f}")

        # Total
        total_pnl = sum(r["pnl"] for r in all_results)
        total_trades = sum(r["trades"] for r in all_results)
        total_wins = sum(r["wins"] for r in all_results)
        overall_wr = (total_wins / total_trades * 100) if total_trades else 0
        print(f"\n  {'TOTAL':<22} {total_trades:>5}  {overall_wr:>5.1f}% R${total_pnl:>+8.2f}")

    print("\n" + "═" * 90 + "\n")


if __name__ == "__main__":
    run()
