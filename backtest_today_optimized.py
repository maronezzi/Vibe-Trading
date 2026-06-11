"""
Backtest SPLIT OTIMIZADO — 09/06/2026
WDO: VWAP(20) SL 1.0x ATR Trail 1.5x/0.3x
WIN: BB(25,1.5) RSI(30/75) SL 1.5x ATR Trail 1.5x/0.3x
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from datetime import date
from backtest_split_strategies import (
    fetch, calc_atr, CONTRACT_SPECS, calc_vwap, calc_bollinger, calc_rsi,
    CLOSE_HOUR, CLOSE_MINUTE, COMMISSION, MAX_CT, SL_MIN_WIN, SL_MIN_WDO
)
import numpy as np, pandas as pd

TARGET_DATE = date(2026, 6, 9)


def _backtest_vwap_opt(df, symbol):
    """VWAP com params otimizados: SL 1.0x, Trail 1.5x/0.3x."""
    spec = CONTRACT_SPECS[symbol]
    mult, margin, slip_r = spec["mult"], spec["margin"], spec["slip_r"]
    is_win = "WIN" in symbol; is_wdo = "WDO" in symbol
    sl_mult, trail_act, trail_dist = 1.0, 1.5, 0.3

    atr = calc_atr(df, 14)
    vwap = calc_vwap(df, 20)
    cash = 100_000.0; pos = 0; ep = 0.0; e_atr = 0.0; best = 0.0
    sl_price = 0.0; trail_on = False; bars_in = 0
    trade_log = []; daily_pnl = {}

    def _close(price, reason):
        nonlocal cash, pos, ep, best, sl_price, trail_on, e_atr, bars_in
        if pos == 0: return
        sl_cost = slip_r * MAX_CT; comm = COMMISSION * MAX_CT
        pnl = ((price - ep) if pos == 1 else (ep - price)) * mult * MAX_CT - sl_cost - comm
        cash += margin * MAX_CT + pnl
        trade_log.append({"pnl": pnl, "reason": reason, "type": "LONG" if pos == 1 else "SHORT", "ep": ep, "xp": price, "bars": bars_in})
        daily_pnl.setdefault(0, 0.0); daily_pnl[0] += pnl
        pos = 0; ep = 0; best = 0; sl_price = 0; trail_on = False; bars_in = 0

    def _open(direction, price, cur_atr):
        nonlocal cash, pos, ep, e_atr, best, sl_price, trail_on, bars_in
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
            bars_in = 0

    for i, (date, row) in enumerate(df.iterrows()):
        price = float(row["close"]); high = float(row["high"]); low = float(row["low"])
        hour = int(row["hour"]); minute = int(row["minute"])
        cur_atr = float(atr.iloc[i]) if i > 0 and not pd.isna(atr.iloc[i]) else 0
        cur_vwap = float(vwap.iloc[i]) if not pd.isna(vwap.iloc[i]) else 0

        if pos == 0:
            if cur_atr > 0 and cur_vwap > 0:
                atr_pct = (cur_atr / price) if price > 0 else 0
                if atr_pct < 0.0015: buy_m, sell_m = 1.0005, 0.9995
                elif atr_pct < 0.003: buy_m, sell_m = 1.0015, 0.9985
                else: buy_m, sell_m = 1.003, 0.997
                if price > cur_vwap * buy_m: _open("BUY", price, cur_atr)
                elif price < cur_vwap * sell_m: _open("SELL", price, cur_atr)
            continue

        bars_in += 1
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
    return _stats(trade_log, daily_pnl)


def _backtest_bb_opt(df, symbol):
    """Bollinger com params otimizados: BB(25,1.5) RSI(30/75) SL 1.5x Trail 1.5x/0.3x."""
    spec = CONTRACT_SPECS[symbol]
    mult, margin, slip_r = spec["mult"], spec["margin"], spec["slip_r"]
    is_win = "WIN" in symbol; is_wdo = "WDO" in symbol
    sl_mult, trail_act, trail_dist = 1.5, 1.5, 0.3
    bb_period, bb_std_val = 25, 1.5
    rsi_buy, rsi_sell = 30, 75

    atr = calc_atr(df, 14)
    bb_upper, bb_mid, bb_lower = calc_bollinger(df, bb_period, bb_std_val)
    rsi = calc_rsi(df["close"], 14)

    cash = 100_000.0; pos = 0; ep = 0.0; e_atr = 0.0; best = 0.0
    sl_price = 0.0; trail_on = False; bars_in = 0
    trade_log = []; daily_pnl = {}

    def _close(price, reason):
        nonlocal cash, pos, ep, best, sl_price, trail_on, e_atr, bars_in
        if pos == 0: return
        sl_cost = slip_r * MAX_CT; comm = COMMISSION * MAX_CT
        pnl = ((price - ep) if pos == 1 else (ep - price)) * mult * MAX_CT - sl_cost - comm
        cash += margin * MAX_CT + pnl
        trade_log.append({"pnl": pnl, "reason": reason, "type": "LONG" if pos == 1 else "SHORT", "ep": ep, "xp": price, "bars": bars_in})
        daily_pnl.setdefault(0, 0.0); daily_pnl[0] += pnl
        pos = 0; ep = 0; best = 0; sl_price = 0; trail_on = False; bars_in = 0

    def _open(direction, price, cur_atr):
        nonlocal cash, pos, ep, e_atr, best, sl_price, trail_on, bars_in
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
            bars_in = 0

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
                if low <= cur_bb_low and cur_rsi < rsi_buy: _open("BUY", price, cur_atr)
                elif high >= cur_bb_up and cur_rsi > rsi_sell: _open("SELL", price, cur_atr)
            continue

        bars_in += 1
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
    return _stats(trade_log, daily_pnl)


def _stats(trade_log, daily_pnl):
    n = len(trade_log)
    if n == 0:
        return {"ok": True, "trades": 0, "wins": 0, "wr": 0, "pnl": 0, "pf": 0, "payoff": 0, "reasons": {}, "trade_log": []}
    n_wins = sum(1 for t in trade_log if t["pnl"] > 0)
    total_pnl = sum(t["pnl"] for t in trade_log)
    wr = n_wins / n * 100
    gw = sum(t["pnl"] for t in trade_log if t["pnl"] > 0)
    gl = sum(abs(t["pnl"]) for t in trade_log if t["pnl"] <= 0)
    pf = gw / gl if gl > 0 else 999
    wp = [t["pnl"] for t in trade_log if t["pnl"] > 0]
    lp = [abs(t["pnl"]) for t in trade_log if t["pnl"] <= 0]
    aw = np.mean(wp) if wp else 0
    al = np.mean(lp) if lp else 1
    payoff = aw / al if al > 0 else 0
    reasons = {}
    for t in trade_log:
        r = t["reason"]
        reasons.setdefault(r, {"count": 0, "pnl": 0, "wins": 0})
        reasons[r]["count"] += 1; reasons[r]["pnl"] += t["pnl"]
        if t["pnl"] > 0: reasons[r]["wins"] += 1
    return {"ok": True, "trades": n, "wins": n_wins, "wr": wr, "pnl": total_pnl,
            "pf": pf, "payoff": payoff, "reasons": reasons, "trade_log": trade_log}


def run():
    print("\n" + "═" * 80)
    print("  🧪 BACKTEST SPLIT OTIMIZADO — 09/06/2026")
    print("  " + "─" * 76)
    print("  WDO → VWAP(20) | SL 1.0x | Trail 1.5x/0.3x")
    print("  WIN → BB(25,1.5) RSI(30/75) | SL 1.5x | Trail 1.5x/0.3x")
    print("═" * 80)

    combos = [
        ("WDO$", "M5",  1000, "VWAP",      _backtest_vwap_opt),
        ("WDO$", "M15", 500,  "VWAP",      _backtest_vwap_opt),
        ("WIN$", "M5",  1000, "Bollinger",  _backtest_bb_opt),
        ("WIN$", "M15", 500,  "Bollinger",  _backtest_bb_opt),
    ]
    all_results = []

    for sym, tf, n_bars, strategy, bt_func in combos:
        spec = CONTRACT_SPECS[sym]
        print(f"\n📡 {sym} ({spec['name']}) {tf} — {strategy}...")
        df = fetch(sym, tf, n_bars)
        if df.empty:
            print("  ❌ Sem dados"); continue

        df_today = df[df["date"] == TARGET_DATE].copy()
        if df_today.empty:
            print(f"  ❌ Sem dados para {TARGET_DATE}"); continue

        df_today["hour"] = df_today.index.hour
        df_today["minute"] = df_today.index.minute
        df_today["date"] = df_today.index.date

        p0 = float(df_today["close"].iloc[0])
        p1 = float(df_today["close"].iloc[-1])
        atr_avg = calc_atr(df_today, 14).mean()

        print(f"  ✅ {len(df_today)} barras | {p0:.2f} → {p1:.2f} ({(p1/p0-1)*100:+.2f}%) | ATR: {atr_avg:.1f} pts")

        r = bt_func(df_today, sym)
        if r["ok"]:
            print(f"\n  📊 {strategy} — {sym} {tf}")
            print(f"  {'─' * 55}")
            print(f"  Trades: {r['trades']} | WR: {r['wr']:.1f}% | PnL: R$ {r['pnl']:+.2f} | PF: {r['pf']:.2f}")

            for reason, data in sorted(r["reasons"].items(), key=lambda x: x[1]["pnl"], reverse=True):
                pct = data["count"] / r["trades"] * 100 if r["trades"] else 0
                wr_r = data["wins"] / data["count"] * 100 if data["count"] else 0
                print(f"    {reason:<8}: {data['count']:>3} ({pct:4.1f}%) WR {wr_r:.0f}% PnL R${data['pnl']:+.0f}")

            if r["trade_log"]:
                print(f"\n  Trades:")
                for i, t in enumerate(r["trade_log"], 1):
                    icon = "✅" if t["pnl"] > 0 else "❌"
                    print(f"    {i:>2}. {t['type']:<5} {t['ep']:.2f} → {t['xp']:.2f} | R$ {t['pnl']:+.2f} | {t['reason']} | {t['bars']}b {icon}")

            all_results.append({"symbol": sym, "tf": tf, "strategy": strategy, "trades": r["trades"], "wr": r["wr"], "pnl": r["pnl"], "pf": r["pf"]})

    if all_results:
        ranking = sorted(all_results, key=lambda x: x["pnl"], reverse=True)
        total_pnl = sum(r["pnl"] for r in all_results)
        total_trades = sum(r["trades"] for r in all_results)
        total_wins = sum(r.get("wins", 0) for r in all_results)

        print("\n\n" + "═" * 80)
        print("  📋 RANKING — 09/06/2026 (params otimizados)")
        print("═" * 80)
        print(f"\n  {'#':>2} {'Ativo':<7} {'TF':<4} {'Strategy':<10} {'Trades':>6} {'WR':>6} {'PnL':>10} {'PF':>6}")
        print(f"  {'─'*55}")
        for i, r in enumerate(ranking, 1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i:>2}"
            print(f"  {medal} {r['symbol']:<7} {r['tf']:<4} {r['strategy']:<10} {r['trades']:>5}  {r['wr']:>5.1f}% R${r['pnl']:>+8.2f} {r['pf']:>5.2f}")

        overall_wr = (total_wins / total_trades * 100) if total_trades else 0
        print(f"\n  {'TOTAL':<22} {total_trades:>5}  {overall_wr:>5.1f}% R${total_pnl:>+8.2f}")

        # Comparação com configs anteriores
        print(f"\n  📈 Comparação com configs ANTERIORES (SL 1.5x, Trail 0.5x):")
        print(f"  {'─'*60}")
        old_results = {"WDO$ M5 VWAP": 0, "WDO$ M15 VWAP": 0, "WIN$ M5 Bollinger": -65.5, "WIN$ M15 Bollinger": 0}
        for r in all_results:
            key = f"{r['symbol']} {r['tf']} {r['strategy']}"
            old = old_results.get(key, 0)
            delta = r["pnl"] - old
            icon = "✅" if delta >= 0 else "❌"
            print(f"  {r['symbol']:<7} {r['tf']:<4} Antes: R${old:>+7.1f} → Agora: R${r['pnl']:>+7.1f} {icon}")

        if total_pnl > 0:
            print(f"\n  ✅ Dia LUCRATIVO com params otimizados!")
        else:
            print(f"\n  🔴 Prejuízo: R$ {total_pnl:+.2f}")

    print("\n" + "═" * 80 + "\n")


if __name__ == "__main__":
    run()
