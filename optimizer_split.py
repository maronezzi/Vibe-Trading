"""
optimizer_split.py — Otimiza parâmetros separados para WDO (VWAP) e WIN (Bollinger).
Testa combinações e encontra a config ótima pra cada ativo.
"""
import sys, os, csv, io, subprocess
from itertools import product
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
ATR_PERIOD = 14
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
    mid = df["close"].rolling(period).mean()
    std = df["close"].rolling(period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    rs = gain / loss.replace(0, 1)
    return 100 - (100 / (1 + rs))


def backtest_vwap(df, symbol, params):
    """Backtest VWAP com parâmetros customizáveis."""
    spec = CONTRACT_SPECS[symbol]
    mult, margin, slip_r = spec["mult"], spec["margin"], spec["slip_r"]
    is_win = "WIN" in symbol
    is_wdo = "WDO" in symbol

    sl_mult = params["sl_atr_mult"]
    trail_act = params["trail_activate"]
    trail_dist = params["trail_distance"]
    buy_thresh = params["vwap_buy"]
    sell_thresh = params["vwap_sell"]
    vwap_period = params.get("vwap_period", 20)

    atr = calc_atr(df, ATR_PERIOD)
    vwap = calc_vwap(df, vwap_period)

    cash = 100_000.0
    pos = 0; ep = 0.0; e_atr = 0.0; best = 0.0
    sl_price = 0.0; trail_on = False; bars = 0

    trade_log = []
    daily_pnl = {}

    def _close(price, reason):
        nonlocal cash, pos, ep, best, sl_price, trail_on, e_atr, bars
        if pos == 0: return
        sl_cost = slip_r * MAX_CT; comm = COMMISSION * MAX_CT
        pnl = ((price - ep) if pos == 1 else (ep - price)) * mult * MAX_CT - sl_cost - comm
        cash += margin * MAX_CT + pnl
        trade_log.append({"pnl": pnl, "reason": reason})
        d = df.index[0].date()
        daily_pnl.setdefault(d, 0.0); daily_pnl[d] += pnl
        pos = 0; ep = 0; best = 0; sl_price = 0; trail_on = False; bars = 0

    def _open(direction, price, cur_atr):
        nonlocal cash, pos, ep, e_atr, best, sl_price, trail_on, bars
        if pos != 0: return
        raw_sl = int(cur_atr * sl_mult)
        if is_win: raw_sl = max(raw_sl, SL_MIN_WIN)
        elif is_wdo: raw_sl = max(raw_sl, SL_MIN_WDO)
        raw_sl = ((raw_sl + 4) // 5) * 5
        cost = slip_r * MAX_CT + COMMISSION * MAX_CT
        if cash >= margin * MAX_CT + cost:
            cash -= margin * MAX_CT + cost
            pos = 1 if direction == "BUY" else -1
            ep = price; e_atr = cur_atr; best = price; trail_on = False
            sl_price = price - raw_sl if pos == 1 else price + raw_sl
            bars = 0

    for i, (date, row) in enumerate(df.iterrows()):
        price = float(row["close"]); high = float(row["high"]); low = float(row["low"])
        hour = int(row["hour"]); minute = int(row["minute"])
        cur_atr = float(atr.iloc[i]) if i > 0 and not pd.isna(atr.iloc[i]) else 0
        cur_vwap = float(vwap.iloc[i]) if not pd.isna(vwap.iloc[i]) else 0

        if pos == 0:
            if cur_atr > 0 and cur_vwap > 0:
                if price > cur_vwap * buy_thresh: _open("BUY", price, cur_atr)
                elif price < cur_vwap * sell_thresh: _open("SELL", price, cur_atr)
            continue

        bars += 1
        if pos == 1: best = max(best, high)
        else: best = min(best, low) if best > 0 else low
        profit_pts = best - ep if pos == 1 else ep - best

        if not trail_on and e_atr > 0 and profit_pts >= trail_act * e_atr:
            trail_on = True
        if trail_on and e_atr > 0:
            td = trail_dist * e_atr
            if pos == 1:
                ns = best - td
                if ns > sl_price: sl_price = ns
            else:
                ns = best + td
                if ns < sl_price: sl_price = ns

        if sl_price > 0:
            if pos == 1 and low <= sl_price: _close(sl_price, "SL"); continue
            elif pos == -1 and high >= sl_price: _close(sl_price, "SL"); continue
        if hour > CLOSE_HOUR or (hour == CLOSE_HOUR and minute >= CLOSE_MINUTE):
            _close(price, "1645"); continue

    if pos != 0: _close(float(df["close"].iloc[-1]), "FORCE")
    return _stats(trade_log, daily_pnl, df)


def backtest_bollinger(df, symbol, params):
    """Backtest Bollinger com parâmetros customizáveis."""
    spec = CONTRACT_SPECS[symbol]
    mult, margin, slip_r = spec["mult"], spec["margin"], spec["slip_r"]
    is_win = "WIN" in symbol
    is_wdo = "WDO" in symbol

    sl_mult = params["sl_atr_mult"]
    trail_act = params["trail_activate"]
    trail_dist = params["trail_distance"]
    bb_period = params["bb_period"]
    bb_std = params["bb_std"]
    rsi_buy = params["rsi_buy"]
    rsi_sell = params["rsi_sell"]
    rsi_period = params.get("rsi_period", 14)

    atr = calc_atr(df, ATR_PERIOD)
    bb_upper, bb_mid, bb_lower = calc_bollinger(df, bb_period, bb_std)
    rsi = calc_rsi(df["close"], rsi_period)

    cash = 100_000.0
    pos = 0; ep = 0.0; e_atr = 0.0; best = 0.0
    sl_price = 0.0; trail_on = False; bars = 0

    trade_log = []
    daily_pnl = {}

    def _close(price, reason):
        nonlocal cash, pos, ep, best, sl_price, trail_on, e_atr, bars
        if pos == 0: return
        sl_cost = slip_r * MAX_CT; comm = COMMISSION * MAX_CT
        pnl = ((price - ep) if pos == 1 else (ep - price)) * mult * MAX_CT - sl_cost - comm
        cash += margin * MAX_CT + pnl
        trade_log.append({"pnl": pnl, "reason": reason})
        d = df.index[0].date()
        daily_pnl.setdefault(d, 0.0); daily_pnl[d] += pnl
        pos = 0; ep = 0; best = 0; sl_price = 0; trail_on = False; bars = 0

    def _open(direction, price, cur_atr):
        nonlocal cash, pos, ep, e_atr, best, sl_price, trail_on, bars
        if pos != 0: return
        raw_sl = int(cur_atr * sl_mult)
        if is_win: raw_sl = max(raw_sl, SL_MIN_WIN)
        elif is_wdo: raw_sl = max(raw_sl, SL_MIN_WDO)
        raw_sl = ((raw_sl + 4) // 5) * 5
        cost = slip_r * MAX_CT + COMMISSION * MAX_CT
        if cash >= margin * MAX_CT + cost:
            cash -= margin * MAX_CT + cost
            pos = 1 if direction == "BUY" else -1
            ep = price; e_atr = cur_atr; best = price; trail_on = False
            sl_price = price - raw_sl if pos == 1 else price + raw_sl
            bars = 0

    for i, (date, row) in enumerate(df.iterrows()):
        price = float(row["close"]); high = float(row["high"]); low = float(row["low"])
        hour = int(row["hour"]); minute = int(row["minute"])
        cur_atr = float(atr.iloc[i]) if i > 0 and not pd.isna(atr.iloc[i]) else 0
        cur_bb_up = float(bb_upper.iloc[i]) if not pd.isna(bb_upper.iloc[i]) else 0
        cur_bb_mid = float(bb_mid.iloc[i]) if not pd.isna(bb_mid.iloc[i]) else 0
        cur_bb_low = float(bb_lower.iloc[i]) if not pd.isna(bb_lower.iloc[i]) else 0
        cur_rsi = float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else 50

        if pos == 0:
            if cur_atr > 0 and cur_bb_up > 0 and cur_bb_low > 0:
                if low <= cur_bb_low and cur_rsi < rsi_buy:
                    _open("BUY", price, cur_atr)
                elif high >= cur_bb_up and cur_rsi > rsi_sell:
                    _open("SELL", price, cur_atr)
            continue

        bars += 1
        if pos == 1: best = max(best, high)
        else: best = min(best, low) if best > 0 else low
        profit_pts = best - ep if pos == 1 else ep - best

        if not trail_on and e_atr > 0 and profit_pts >= trail_act * e_atr:
            trail_on = True
        if trail_on and e_atr > 0:
            td = trail_dist * e_atr
            if pos == 1:
                ns = best - td
                if ns > sl_price: sl_price = ns
            else:
                ns = best + td
                if ns < sl_price: sl_price = ns

        if sl_price > 0:
            if pos == 1 and low <= sl_price: _close(sl_price, "SL"); continue
            elif pos == -1 and high >= sl_price: _close(sl_price, "SL"); continue
        if hour > CLOSE_HOUR or (hour == CLOSE_HOUR and minute >= CLOSE_MINUTE):
            _close(price, "1645"); continue

    if pos != 0: _close(float(df["close"].iloc[-1]), "FORCE")
    return _stats(trade_log, daily_pnl, df)


def _stats(trade_log, daily_pnl, df):
    n_trades = len(trade_log)
    if n_trades == 0:
        return {"ok": True, "trades": 0, "wr": 0, "pnl": 0, "sharpe": 0, "pf": 0, "payoff": 0, "max_dd": 0, "avg_daily": 0, "n_days": 1}

    n_wins = sum(1 for t in trade_log if t["pnl"] > 0)
    total_pnl = sum(t["pnl"] for t in trade_log)
    wr = (n_wins / n_trades * 100) if n_trades else 0

    gross_win = sum(t["pnl"] for t in trade_log if t["pnl"] > 0)
    gross_loss = sum(abs(t["pnl"]) for t in trade_log if t["pnl"] <= 0)
    pf = gross_win / gross_loss if gross_loss > 0 else (999 if gross_win > 0 else 0)

    wins_p = [t["pnl"] for t in trade_log if t["pnl"] > 0]
    losses_p = [abs(t["pnl"]) for t in trade_log if t["pnl"] <= 0]
    avg_win = np.mean(wins_p) if wins_p else 0
    avg_loss = np.mean(losses_p) if losses_p else 1
    payoff = avg_win / avg_loss if avg_loss > 0 else 0

    n_days = df["date"].nunique()
    avg_daily = total_pnl / n_days if n_days else 0

    daily_vals = list(daily_pnl.values())
    sharpe = np.mean(daily_vals) / np.std(daily_vals) * np.sqrt(252) if len(daily_vals) > 1 and np.std(daily_vals) > 0 else 0

    return {
        "ok": True, "trades": n_trades, "wins": n_wins, "wr": wr,
        "pnl": total_pnl, "sharpe": sharpe, "pf": pf, "payoff": payoff,
        "max_dd": 0, "avg_daily": avg_daily, "n_days": n_days,
    }


def optimize_wdo(df_m5, df_m15):
    """Otimiza parâmetros VWAP para WDO."""
    print("\n" + "═" * 80)
    print("  🔧 OTIMIZAÇÃO WDO — VWAP")
    print("═" * 80)

    param_grid = {
        "vwap_buy":     [1.0005, 1.001, 1.003],
        "vwap_sell":    [0.9995, 0.999, 0.997],
        "sl_atr_mult":  [1.0, 1.5, 2.0],
        "trail_activate": [1.0, 1.5, 2.0],
        "trail_distance": [0.3, 0.5],
    }

    keys = list(param_grid.keys())
    combos = list(product(*param_grid.values()))
    print(f"  Testando {len(combos)} combinações...")

    results = []
    for i, vals in enumerate(combos):
        params = dict(zip(keys, vals))
        params["vwap_period"] = 20

        r_m5 = backtest_vwap(df_m5, "WDO$", params)
        r_m15 = backtest_vwap(df_m15, "WDO$", params)

        total_pnl = r_m5["pnl"] + r_m15["pnl"]
        total_trades = r_m5["trades"] + r_m15["trades"]
        total_wins = r_m5["wins"] + r_m15["wins"]
        overall_wr = (total_wins / total_trades * 100) if total_trades else 0

        # Score: PnL ponderado por consistência (Sharpe) e WR
        score = total_pnl
        if overall_wr < 40: score *= 0.5  # penaliza WR baixo
        if total_trades < 5: score *= 0.3  # penaliza poucos trades

        results.append({
            "params": params, "score": score,
            "pnl": total_pnl, "trades": total_trades, "wr": overall_wr,
            "m5_pnl": r_m5["pnl"], "m15_pnl": r_m15["pnl"],
            "m5_trades": r_m5["trades"], "m15_trades": r_m15["trades"],
            "m5_wr": r_m5["wr"], "m15_wr": r_m15["wr"],
            "sharpe": (r_m5["sharpe"] + r_m15["sharpe"]) / 2,
        })

    results.sort(key=lambda x: x["score"], reverse=True)

    print(f"\n  🏆 TOP 10 WDO VWAP:")
    print(f"  {'#':>2} {'Score':>8} {'PnL':>9} {'Trades':>6} {'WR':>5} {'M5 PnL':>9} {'M15 PnL':>9} | Parameters")
    print(f"  {'─'*90}")
    for i, r in enumerate(results[:10], 1):
        p = r["params"]
        print(f"  {i:>2} R${r['score']:>+7.0f} R${r['pnl']:>+7.0f} {r['trades']:>5}  {r['wr']:>4.0f}% "
              f"R${r['m5_pnl']:>+7.0f} R${r['m15_pnl']:>+7.0f} | "
              f"VWAP:{p['vwap_buy']}/{p['vwap_sell']} SL:{p['sl_atr_mult']}x "
              f"Trail:{p['trail_activate']}x/{p['trail_distance']}x")

    return results[0] if results else None


def optimize_win(df_m5, df_m15):
    """Otimiza parâmetros Bollinger para WIN."""
    print("\n" + "═" * 80)
    print("  🔧 OTIMIZAÇÃO WIN — Bollinger Bands")
    print("═" * 80)

    param_grid = {
        "bb_period":    [15, 20, 25],
        "bb_std":       [1.5, 2.0, 2.5],
        "rsi_buy":      [25, 30, 35],
        "rsi_sell":     [65, 70, 75],
        "sl_atr_mult":  [1.0, 1.5, 2.0],
        "trail_activate": [1.5, 2.0],
        "trail_distance": [0.3, 0.5],
        "rsi_period":   [14],
    }

    keys = list(param_grid.keys())
    combos = list(product(*param_grid.values()))
    print(f"  Testando {len(combos)} combinações...")

    results = []
    for i, vals in enumerate(combos):
        params = dict(zip(keys, vals))

        r_m5 = backtest_bollinger(df_m5, "WIN$", params)
        r_m15 = backtest_bollinger(df_m15, "WIN$", params)

        total_pnl = r_m5["pnl"] + r_m15["pnl"]
        total_trades = r_m5["trades"] + r_m15["trades"]
        total_wins = r_m5["wins"] + r_m15["wins"]
        overall_wr = (total_wins / total_trades * 100) if total_trades else 0

        score = total_pnl
        if overall_wr < 40: score *= 0.5
        if total_trades < 3: score *= 0.3

        results.append({
            "params": params, "score": score,
            "pnl": total_pnl, "trades": total_trades, "wr": overall_wr,
            "m5_pnl": r_m5["pnl"], "m15_pnl": r_m15["pnl"],
            "m5_trades": r_m5["trades"], "m15_trades": r_m15["trades"],
            "m5_wr": r_m5["wr"], "m15_wr": r_m15["wr"],
            "sharpe": (r_m5["sharpe"] + r_m15["sharpe"]) / 2,
        })

    results.sort(key=lambda x: x["score"], reverse=True)

    print(f"\n  🏆 TOP 10 WIN Bollinger:")
    print(f"  {'#':>2} {'Score':>8} {'PnL':>9} {'Trades':>6} {'WR':>5} {'M5 PnL':>9} {'M15 PnL':>9} | Parameters")
    print(f"  {'─'*100}")
    for i, r in enumerate(results[:10], 1):
        p = r["params"]
        print(f"  {i:>2} R${r['score']:>+7.0f} R${r['pnl']:>+7.0f} {r['trades']:>5}  {r['wr']:>4.0f}% "
              f"R${r['m5_pnl']:>+7.0f} R${r['m15_pnl']:>+7.0f} | "
              f"BB({p['bb_period']},{p['bb_std']}) RSI({p['rsi_buy']}/{p['rsi_sell']}) "
              f"SL:{p['sl_atr_mult']}x Trail:{p['trail_activate']}x/{p['trail_distance']}x")

    return results[0] if results else None


def run():
    print("\n" + "═" * 80)
    print("  🧪 OTIMIZADOR SPLIT — WDO: VWAP | WIN: Bollinger")
    print("  Buscando parâmetros ótimos para cada ativo")
    print("═" * 80)

    # Buscar dados
    print("\n📡 Buscando dados...")
    wdo_m5 = fetch("WDO$", "M5", 1000)
    wdo_m15 = fetch("WDO$", "M15", 500)
    win_m5 = fetch("WIN$", "M5", 1000)
    win_m15 = fetch("WIN$", "M15", 500)

    for name, df in [("WDO M5", wdo_m5), ("WDO M15", wdo_m15), ("WIN M5", win_m5), ("WIN M15", win_m15)]:
        if df.empty:
            print(f"  ❌ {name}: sem dados")
            return
        print(f"  ✅ {name}: {len(df)} barras, {df['date'].nunique()} dias")

    # Otimizar WDO
    best_wdo = optimize_wdo(wdo_m5, wdo_m15)

    # Otimizar WIN
    best_win = optimize_win(win_m5, win_m15)

    # Resumo final
    if best_wdo and best_win:
        print("\n\n" + "═" * 80)
        print("  🏆 CONFIGURAÇÃO ÓTIMA ENCONTRADA")
        print("═" * 80)

        wp = best_wdo["params"]
        wnp = best_win["params"]

        print(f"\n  📊 WDO — VWAP")
        print(f"  ├─ VWAP buy/sell: {wp['vwap_buy']}/{wp['vwap_sell']}")
        print(f"  ├─ SL: {wp['sl_atr_mult']}x ATR")
        print(f"  ├─ Trailing: {wp['trail_activate']}x/{wp['trail_distance']}x ATR")
        print(f"  └─ Resultado: R$ {best_wdo['pnl']:+.0f} | {best_wdo['trades']} trades | WR {best_wdo['wr']:.0f}%")
        print(f"     M5: R$ {best_wdo['m5_pnl']:+.0f} ({best_wdo['m5_trades']}t) | M15: R$ {best_wdo['m15_pnl']:+.0f} ({best_wdo['m15_trades']}t)")

        print(f"\n  📊 WIN — Bollinger")
        print(f"  ├─ BB({wnp['bb_period']}, {wnp['bb_std']})")
        print(f"  ├─ RSI({wnp['rsi_period']}): buy<{wnp['rsi_buy']} sell>{wnp['rsi_sell']}")
        print(f"  ├─ SL: {wnp['sl_atr_mult']}x ATR")
        print(f"  ├─ Trailing: {wnp['trail_activate']}x/{wnp['trail_distance']}x ATR")
        print(f"  └─ Resultado: R$ {best_win['pnl']:+.0f} | {best_win['trades']} trades | WR {best_win['wr']:.0f}%")
        print(f"     M5: R$ {best_win['m5_pnl']:+.0f} ({best_win['m5_trades']}t) | M15: R$ {best_win['m15_pnl']:+.0f} ({best_win['m15_trades']}t)")

        total_pnl = best_wdo["pnl"] + best_win["pnl"]
        print(f"\n  💰 PnL TOTAL: R$ {total_pnl:+.0f}")
        print(f"  📈 R$/dia estimado: R$ {total_pnl / max(best_wdo.get('n_days', 1), 1):+.0f}")

        # Gerar config
        print(f"\n\n  📝 CONFIG PARA vt_autotrader.py:")
        print(f"  {'─'*60}")
        print(f"""
    "strategy": {{
        "WDO": "VWAP",
        "WIN": "BOLLINGER",
    }},

    # WDO params (otimizado)
    "wdo": {{
        "vwap_period": 20,
        "vwap_buy_threshold": {wp['vwap_buy']},
        "vwap_sell_threshold": {wp['vwap_sell']},
        "sl_atr_mult": {wp['sl_atr_mult']},
        "trail_activate": {wp['trail_activate']},
        "trail_distance": {wp['trail_distance']},
        "cooldown_seconds": 300,
        "max_daily_trades": 20,
    }},

    # WIN params (otimizado)
    "win": {{
        "bb_period": {wnp['bb_period']},
        "bb_std": {wnp['bb_std']},
        "rsi_period": {wnp['rsi_period']},
        "rsi_buy": {wnp['rsi_buy']},
        "rsi_sell": {wnp['rsi_sell']},
        "sl_atr_mult": {wnp['sl_atr_mult']},
        "trail_activate": {wnp['trail_activate']},
        "trail_distance": {wnp['trail_distance']},
        "cooldown_seconds": 900,
        "max_daily_trades": 10,
    }},""")

    print("\n" + "═" * 80 + "\n")


if __name__ == "__main__":
    run()
