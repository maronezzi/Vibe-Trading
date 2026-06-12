"""Backtest multi-ativo: top 10 B3 + brapi.dev + SMA crossover.

Roda SMA(20,50) em paralelo nas 10 maiores ações B3 por market cap,
compara com buy & hold e ranqueia por Sharpe / alpha.

Uso: ./agent/venv/bin/python backtest_multi.py [range] [top_n]
  ex:  ./agent/venv/bin/python backtest_multi.py 1y 10
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "agent"))

import numpy as np
import pandas as pd

from backtest.loaders.b3_loader import fetch_ohlcv, fetch_quote


# Top 10 B3 (blue chips líquidas, ~R$ 1.5 tri somados) — jun/2026
TOP_B3 = [
    "PETR4",  # Petrobras PN
    "VALE3",  # Vale ON
    "ITUB4",  # Itaú PN
    "BBDC4",  # Bradesco PN
    "BBAS3",  # Banco do Brasil ON
    "B3SA3",  # B3 ON
    "ABEV3",  # Ambev ON
    "WEGE3",  # WEG ON
    "RENT3",  # Localiza ON
    "SUZB3",  # Suzano ON
]


def backtest_sma(df: pd.DataFrame, *, fast: int = 20, slow: int = 50,
                 capital: float = 100_000.0,
                 slippage: float = 0.001, commission: float = 0.0003) -> dict:
    """SMA crossover long-only. Retorna métricas + equity final."""
    if len(df) < slow + 5:
        return {"ok": False, "reason": f"só {len(df)} pregões (precisa ≥{slow + 5})"}

    df = df.copy()
    df[f"sma{fast}"] = df["close"].rolling(fast).mean()
    df[f"sma{slow}"] = df["close"].rolling(slow).mean()
    df["signal"] = 0
    df.loc[df[f"sma{fast}"] > df[f"sma{slow}"], "signal"] = 1
    df["position"] = df["signal"].shift(1).fillna(0)

    cash, shares, entry_price = capital, 0, 0.0
    equity_list = []
    n_trades = 0
    n_wins = 0

    for date, row in df.iterrows():
        price = float(row["close"])
        target_pos = int(row["position"])
        equity_list.append(cash + shares * price)

        if target_pos == 1 and shares == 0:
            buy_price = price * (1 + slippage)
            cps = buy_price * (1 + commission)
            qty = int(cash / cps)
            if qty > 0:
                cash -= qty * cps
                shares = qty
                entry_price = buy_price

        elif target_pos == 0 and shares > 0:
            sell_price = price * (1 - slippage)
            proceeds = sell_price * shares * (1 - commission)
            cash += proceeds
            pnl = proceeds - shares * entry_price * (1 + commission)
            n_trades += 1
            if pnl > 0:
                n_wins += 1
            shares = 0
            entry_price = 0.0

    if shares > 0:
        last = float(df["close"].iloc[-1])
        proceeds = last * shares * (1 - slippage) * (1 - commission)
        cash += proceeds
        n_trades += 1
        if proceeds > shares * entry_price:
            n_wins += 1

    # Buy & Hold
    bh_qty = int(capital / float(df["close"].iloc[0]) / (1 + commission))
    bh_final = bh_qty * float(df["close"].iloc[-1]) * (1 - commission)
    bh_return = (bh_final - capital) / capital * 100

    # Métricas
    eq = pd.Series(equity_list, index=df.index[:len(equity_list)])
    total_return = (cash - capital) / capital * 100
    n_years = max((df.index[-1] - df.index[0]).days / 365.25, 0.01)
    cagr = ((cash / capital) ** (1 / n_years) - 1) * 100
    dd = (eq - eq.cummax()) / eq.cummax()
    max_dd = dd.min() * 100
    rets = eq.pct_change().fillna(0)
    sharpe = (rets.mean() / rets.std()) * np.sqrt(252) if rets.std() > 0 else 0.0

    return {
        "ok": True,
        "trades": n_trades,
        "wins": n_wins,
        "win_rate": (n_wins / n_trades * 100) if n_trades else 0,
        "total_return": total_return,
        "cagr": cagr,
        "sharpe": sharpe,
        "max_dd": max_dd,
        "bh_return": bh_return,
        "alpha": total_return - bh_return,
        "final_equity": cash,
        "n_days": len(df),
    }


def run_portfolio(codes: list[str], range_: str = "1y", pause: float = 0.3) -> pd.DataFrame:
    """Roda backtest em vários tickers via brapi.dev."""
    rows = []
    print(f"\n🚀 Backtest multi-ativo — {len(codes)} tickers — range {range_}")
    print(f"   Fonte: brapi.dev  •  Estratégia: SMA(20,50) long-only\n")

    for i, code in enumerate(codes, 1):
        try:
            df = fetch_ohlcv(code, range_=range_)
            if df is None or df.empty:
                print(f"  [{i:>2}/{len(codes)}] {code:6s} ❌ sem dados (brapi + yf falharam)")
                continue

            quote = fetch_quote(code) or {}
            r = backtest_sma(df)
            src = df.attrs.get("source", "?")  # brapi ou yfinance

            if not r["ok"]:
                print(f"  [{i:>2}/{len(codes)}] {code:6s} ⚠️  {r['reason']}  (fonte: {src})")
                continue

            name = quote.get("longName") or "?"
            print(f"  [{i:>2}/{len(codes)}] {code:6s}  "
                  f"ret {r['total_return']:>+6.2f}%  "
                  f"sharpe {r['sharpe']:>5.2f}  "
                  f"alpha {r['alpha']:>+6.2f}%  "
                  f"trades {r['trades']:>2d}  "
                  f"wr {r['win_rate']:>3.0f}%  "
                  f"[{src}]  "
                  f"({name})")

            rows.append({
                "code": code,
                "name": name,
                "source": src,
                "price": quote.get("regularMarketPrice"),
                "pe": quote.get("priceEarnings"),
                "mcap_bi": quote.get("marketCap", 0) / 1e9 if quote.get("marketCap") else None,
                "trades": r["trades"],
                "win_rate": r["win_rate"],
                "total_return": r["total_return"],
                "cagr": r["cagr"],
                "sharpe": r["sharpe"],
                "max_dd": r["max_dd"],
                "bh_return": r["bh_return"],
                "alpha": r["alpha"],
                "n_days": r["n_days"],
            })
            time.sleep(pause)  # gentileza com brapi (se cair de novo, yf cobre)

        except Exception as e:
            print(f"  [{i:>2}/{len(codes)}] {code:6s} 💥 {e}")
            continue

    return pd.DataFrame(rows)


def print_ranking(df: pd.DataFrame) -> None:
    """Imprime ranking por Sharpe, alpha e drawdown."""
    if df.empty:
        print("\n❌ Nenhum ticker retornou dados.")
        return

    print("\n" + "=" * 90)
    print("📊  RANKING — TOP 10 B3 — SMA(20,50) vs Buy & Hold")
    print("=" * 90)

    print(f"\n🏆 RANKING POR SHARPE (risco/retorno ajustado):")
    print("-" * 90)
    top_sharpe = df.sort_values("sharpe", ascending=False)
    for i, r in top_sharpe.iterrows():
        emoji = "🥇" if i == top_sharpe.index[0] else "🥈" if i == top_sharpe.index[1] else "🥉" if i == top_sharpe.index[2] else "  "
        print(f"  {emoji} {r['code']:6s}  Sharpe {r['sharpe']:>5.2f}  "
              f"ret {r['total_return']:>+6.2f}%  "
              f"alpha {r['alpha']:>+6.2f}%  "
              f"DD {r['max_dd']:>+6.2f}%")

    print(f"\n💰 RANKING POR ALPHA (bateu buy & hold?):")
    print("-" * 90)
    top_alpha = df.sort_values("alpha", ascending=False)
    for _, r in top_alpha.iterrows():
        emoji = "✅" if r["alpha"] > 0 else "❌"
        print(f"  {emoji} {r['code']:6s}  alpha {r['alpha']:>+6.2f}%  "
              f"(estratégia {r['total_return']:>+6.2f}%  vs  B&H {r['bh_return']:>+6.2f}%)")

    print(f"\n📉 RANKING POR DRAWDOWN (menor = melhor controle de risco):")
    print("-" * 90)
    top_dd = df.sort_values("max_dd", ascending=True)  # menos negativo primeiro
    for _, r in top_dd.iterrows():
        print(f"     {r['code']:6s}  max DD {r['max_dd']:>+6.2f}%  "
              f"sharpe {r['sharpe']:>5.2f}  ret {r['total_return']:>+6.2f}%")

    # === Sumário estatístico ===
    print(f"\n📈 SUMÁRIO ESTATÍSTICO:")
    print("-" * 90)
    n = len(df)
    n_alpha = (df["alpha"] > 0).sum()
    n_pos = (df["total_return"] > 0).sum()
    n_bh_pos = (df["bh_return"] > 0).sum()
    print(f"   Tickers testados:    {n}")
    print(f"   Estratégia positiva: {n_pos}/{n}  ({n_pos/n*100:.0f}%)")
    print(f"   Buy & Hold positivo: {n_bh_pos}/{n}  ({n_bh_pos/n*100:.0f}%)")
    print(f"   Alpha > 0:           {n_alpha}/{n}  ({n_alpha/n*100:.0f}%)")
    print(f"   Retorno médio (estratégia):  {df['total_return'].mean():>+6.2f}%")
    print(f"   Retorno médio (B&H):         {df['bh_return'].mean():>+6.2f}%")
    print(f"   Sharpe médio:                {df['sharpe'].mean():>5.2f}")
    print(f"   Max DD médio:                {df['max_dd'].mean():>+6.2f}%")
    print(f"   Win rate médio:              {df['win_rate'].mean():>5.1f}%")


if __name__ == "__main__":
    range_ = sys.argv[1] if len(sys.argv) > 1 else "1y"
    n_top = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    codes = TOP_B3[:n_top]

    df = run_portfolio(codes, range_=range_)
    print_ranking(df)

    out = Path("/tmp") / f"b3_multi_{range_}.csv"
    df.to_csv(out, index=False)
    print(f"\n💾 Resultados salvos em: {out}")
