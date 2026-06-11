"""
Backtest SPLIT do dia 09/06/2026 — WDO: VWAP | WIN: Bollinger
M5 e M15 para ambos.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from datetime import date
from backtest_split_strategies import (
    fetch, backtest_vwap, backtest_bollinger, calc_atr, CONTRACT_SPECS
)

TARGET_DATE = date(2026, 6, 9)

def run():
    print("\n" + "═" * 80)
    print("  🧪 BACKTEST SPLIT — 09/06/2026")
    print("  " + "─" * 76)
    print("  WDO → VWAP(20) | WIN → Bollinger(20,2)+RSI(14)")
    print("  SL: 1.5x ATR | Trailing: 1.5x/0.5x ATR | 1 contrato")
    print("═" * 80)

    combos = [
        ("WDO$", "M5",  1000, "VWAP",      backtest_vwap),
        ("WDO$", "M15", 500,  "VWAP",      backtest_vwap),
        ("WIN$", "M5",  1000, "Bollinger",  backtest_bollinger),
        ("WIN$", "M15", 500,  "Bollinger",  backtest_bollinger),
    ]

    all_results = []

    for sym, tf, n_bars, strategy, bt_func in combos:
        spec = CONTRACT_SPECS[sym]
        print(f"\n📡 {sym} ({spec['name']}) {tf} — {strategy}...")

        df = fetch(sym, tf, n_bars)
        if df.empty:
            print("  ❌ Sem dados do MT5"); continue

        df_today = df[df["date"] == TARGET_DATE].copy()
        if df_today.empty:
            print(f"  ❌ Sem dados para {TARGET_DATE}")
            continue

        df_today["hour"] = df_today.index.hour
        df_today["minute"] = df_today.index.minute
        df_today["date"] = df_today.index.date

        p0 = float(df_today["close"].iloc[0])
        p1 = float(df_today["close"].iloc[-1])
        high_day = float(df_today["high"].max())
        low_day = float(df_today["low"].min())
        atr_avg = calc_atr(df_today, 14).mean()

        print(f"  ✅ {len(df_today)} barras | Abertura: {p0:.2f} → Fechamento: {p1:.2f} ({(p1/p0-1)*100:+.2f}%)")
        print(f"     High: {high_day:.2f} | Low: {low_day:.2f} | ATR: {atr_avg:.1f} pts")

        r = bt_func(df_today, sym)
        if r["ok"]:
            pnl = sum(t["pnl"] for t in r["trade_log"])
            print(f"\n  📊 {strategy} — {sym} {tf}")
            print(f"  {'─' * 55}")
            print(f"  Trades:    {r['trades']} (L:{r['long']} S:{r['short']})")
            print(f"  Win Rate:  {r['wr']:.1f}%")
            print(f"  PnL:       R$ {pnl:+.2f}")
            print(f"  PF:        {r['pf']:.2f}")
            print(f"  Payoff:    {r['payoff']:.2f}")

            print(f"\n  Saídas:")
            for reason, data in sorted(r["reasons"].items(), key=lambda x: x[1]["pnl"], reverse=True):
                pct = data["count"] / r["trades"] * 100 if r["trades"] else 0
                wr_r = data["wins"] / data["count"] * 100 if data["count"] else 0
                print(f"    {reason:<8}: {data['count']:>3} ({pct:4.1f}%) WR {wr_r:.0f}% PnL R${data['pnl']:+.0f}")

            if r["trade_log"]:
                print(f"\n  Trades:")
                for i, t in enumerate(r["trade_log"], 1):
                    icon = "✅" if t["pnl"] > 0 else "❌"
                    print(f"    {i:>2}. {t['type']:<5} {t['ep']:.2f} → {t['xp']:.2f} | "
                          f"R$ {t['pnl']:+.2f} | {t['reason']} | {t['bars']}b {icon}")

            all_results.append({
                "symbol": sym, "tf": tf, "strategy": strategy,
                "trades": r["trades"], "wins": r["wins"], "wr": r["wr"],
                "pnl": pnl, "pf": r["pf"], "payoff": r["payoff"],
            })

    if all_results:
        ranking = sorted(all_results, key=lambda x: x["pnl"], reverse=True)
        total_pnl = sum(r["pnl"] for r in all_results)
        total_trades = sum(r["trades"] for r in all_results)
        total_wins = sum(r["wins"] for r in all_results)
        overall_wr = (total_wins / total_trades * 100) if total_trades else 0

        print("\n\n" + "═" * 80)
        print("  📋 RANKING — 09/06/2026")
        print("═" * 80)
        print(f"\n  {'#':>2} {'Ativo':<7} {'TF':<4} {'Strategy':<10} {'Trades':>6} {'WR':>6} {'PnL':>10} {'PF':>6}")
        print(f"  {'─'*55}")
        for i, r in enumerate(ranking, 1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i:>2}"
            print(f"  {medal} {r['symbol']:<7} {r['tf']:<4} {r['strategy']:<10} {r['trades']:>5}  {r['wr']:>5.1f}% "
                  f"R${r['pnl']:>+8.2f} {r['pf']:>5.2f}")

        print(f"\n  {'TOTAL':<22} {total_trades:>5}  {overall_wr:>5.1f}% R${total_pnl:>+8.2f}")

        if total_pnl > 0:
            print(f"\n  ✅ Dia LUCRATIVO! R$ {total_pnl:+.2f}")
        else:
            print(f"\n  🔴 Dia com prejuízo: R$ {total_pnl:+.2f}")

    print("\n" + "═" * 80 + "\n")


if __name__ == "__main__":
    run()
