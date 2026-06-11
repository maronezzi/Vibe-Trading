"""
Backtest do dia 09/06/2026 — replica vt_autotrader.py com filtros do Trader IA.
Filtra barras APENAS do dia de hoje.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime, date
from backtest_autotrader_v6 import fetch, backtest, calc_atr, CONTRACT_SPECS
import numpy as np

TARGET_DATE = date(2026, 6, 9)  # Hoje

def run():
    print("\n" + "═" * 80)
    print("  📊 BACKTEST DO DIA 09/06/2026 — WDO & WIN (M5, M15)")
    print("  " + "─" * 76)
    print("  Replicando vt_autotrader.py + filtros Trader IA")
    print("  VWAP(20) | SL 1.0x ATR | Trailing 1.5x/0.5x ATR | 1 contrato")
    print("═" * 80)

    combos = [
        ("WDO$", "M5", 1000),
        ("WDO$", "M15", 500),
        ("WIN$", "M5", 1000),
        ("WIN$", "M15", 500),
    ]

    all_results = []

    for sym, tf, n_bars in combos:
        spec = CONTRACT_SPECS[sym]
        print(f"\n📡 {sym} ({spec['name']}) {tf} — buscando {n_bars} barras...")

        df = fetch(sym, tf, n_bars)
        if df.empty:
            print("  ❌ Sem dados do MT5")
            continue

        # Filtrar apenas o dia de hoje
        df_today = df[df["date"] == TARGET_DATE].copy()
        if df_today.empty:
            print(f"  ❌ Sem dados para {TARGET_DATE}")
            print(f"     Dados disponíveis: {sorted(df['date'].unique())}")
            continue

        # Recalcular hour/minute/date (garantir consistência)
        df_today["hour"] = df_today.index.hour
        df_today["minute"] = df_today.index.minute
        df_today["date"] = df_today.index.date

        p0 = float(df_today["close"].iloc[0])
        p1 = float(df_today["close"].iloc[-1])
        high_day = float(df_today["high"].max())
        low_day = float(df_today["low"].min())
        atr_series = calc_atr(df_today, 14)
        atr_avg = atr_series.mean()

        print(f"  ✅ {len(df_today)} barras em {TARGET_DATE}")
        print(f"     Abertura: {p0:.2f} | Fechamento: {p1:.2f} | Variação: {(p1/p0-1)*100:+.2f}%")
        print(f"     High: {high_day:.2f} | Low: {low_day:.2f} | Range: {high_day-low_day:.2f}")
        print(f"     ATR(14) médio: {atr_avg:.1f} pts")

        # Rodar backtest com df de hoje
        r = backtest(df_today, sym)

        if r["ok"]:
            print(f"\n  📊 RESULTADOS — {sym} {tf}")
            print(f"  {'─' * 60}")
            print(f"  Trades:    {r['trades']} (Long: {r['long']}, Short: {r['short']})")
            print(f"  Win Rate:  {r['wr']:.1f}%")
            print(f"  PnL Total: R$ {sum(t['pnl'] for t in r['trade_log']):+.2f}")
            print(f"  Avg Win:   R$ {r['avg_win']:+.1f}")
            print(f"  Avg Loss:  R$ {r['avg_loss']:+.1f}")
            print(f"  Payoff:    {r['payoff']:.2f}")
            print(f"  PF:        {r['pf']:.2f}")

            print(f"\n  Motivos de saída:")
            for reason, data in sorted(r["reasons"].items(), key=lambda x: x[1]["pnl"], reverse=True):
                pct = data["count"] / r["trades"] * 100 if r["trades"] else 0
                wr_r = data["wins"] / data["count"] * 100 if data["count"] else 0
                print(f"    {reason:<6}: {data['count']:>3} ({pct:4.1f}%) WR {wr_r:.0f}% PnL R${data['pnl']:+.0f}")

            print(f"\n  Detalhes trade a trade:")
            for i, t in enumerate(r["trade_log"], 1):
                pnl_icon = "✅" if t["pnl"] > 0 else "❌"
                print(f"    {i:>2}. {t['type']:<5} Entry {t['ep']:.2f} → Exit {t['xp']:.2f} | "
                      f"PnL R$ {t['pnl']:+.2f} | {t['reason']} | {t['bars']} barras {pnl_icon}")

            all_results.append({
                "symbol": sym, "tf": tf,
                "trades": r["trades"], "wins": r["wins"], "wr": r["wr"],
                "pnl": sum(t["pnl"] for t in r["trade_log"]),
                "avg_win": r["avg_win"], "avg_loss": r["avg_loss"],
                "payoff": r["payoff"], "pf": r["pf"],
                "long": r["long"], "short": r["short"],
                "reasons": r["reasons"],
                "trade_log": r["trade_log"],
            })

    # RESUMO GERAL
    if all_results:
        print("\n\n" + "═" * 80)
        print("  📋 RESUMO GERAL — 09/06/2026")
        print("═" * 80)

        total_trades = sum(r["trades"] for r in all_results)
        total_wins = sum(r["wins"] for r in all_results)
        total_pnl = sum(r["pnl"] for r in all_results)
        overall_wr = (total_wins / total_trades * 100) if total_trades else 0

        print(f"\n  {'Ativo':<7} {'TF':<4} {'Trades':>6} {'WR':>6} {'PnL':>10} {'Payoff':>7} {'PF':>6}")
        print(f"  {'─'*50}")
        for r in all_results:
            print(f"  {r['symbol']:<7} {r['tf']:<4} {r['trades']:>5}  {r['wr']:>5.1f}% "
                  f"R${r['pnl']:>+8.2f}  {r['payoff']:>6.2f} {r['pf']:>5.2f}")

        print(f"\n  {'TOTAL':<12} {total_trades:>5}  {overall_wr:>5.1f}% R${total_pnl:>+8.2f}")

        # Ranking
        ranking = sorted(all_results, key=lambda x: x["pnl"], reverse=True)
        print(f"\n  🏆 Ranking por PnL:")
        for i, r in enumerate(ranking, 1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "  "
            print(f"  {medal} {r['symbol']} {r['tf']} — R$ {r['pnl']:+.2f}")

        # Análise qualitativa
        print(f"\n  💡 Análise:")
        best = ranking[0]
        worst = ranking[-1]
        if total_pnl > 0:
            print(f"    ✅ Dia LUCRATIVO! R$ {total_pnl:+.2f}")
            print(f"    🏆 Melhor: {best['symbol']} {best['tf']} (R$ {best['pnl']:+.2f})")
        else:
            print(f"    🔴 Dia com prejuízo: R$ {total_pnl:+.2f}")
            print(f"    🏆 Melhor: {best['symbol']} {best['tf']} (R$ {best['pnl']:+.2f})")
            print(f"    ⚠️ Pior: {worst['symbol']} {worst['tf']} (R$ {worst['pnl']:+.2f})")

        # Filtro de tendência
        longs = sum(r["long"] for r in all_results)
        shorts = sum(r["short"] for r in all_results)
        print(f"\n  📊 Direcionalidade: {longs} longs vs {shorts} shorts")

        if overall_wr < 35:
            print(f"  ⚠️ Win rate baixo ({overall_wr:.1f}%) — filtros adicionais recomendados")
        elif overall_wr > 50:
            print(f"  ✅ Win rate saudável ({overall_wr:.1f}%)")

    print("\n" + "═" * 80 + "\n")


if __name__ == "__main__":
    run()
