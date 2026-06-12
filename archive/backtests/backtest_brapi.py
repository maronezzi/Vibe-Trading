"""Backtest VALE3 usando brapi.dev como fonte (B3 nativo, mais rápido).

Migração: yfinance(.SA) → brapi.dev
  - 5-10x mais rápido (0.3s vs 2-3s por request)
  - Sem rate limit agressivo
  - Dados B3-nativos
  - Dados atualizados (jun/2026)

Estratégia: SMA(20) vs SMA(50) — long-only, capital 100% alocado
"""

import sys
from pathlib import Path

# Adicionar o agent/ ao path pra importar o loader
sys.path.insert(0, str(Path(__file__).parent / "agent"))

import numpy as np
import pandas as pd

from backtest.loaders.brapi_loader import fetch_ohlcv, fetch_quote


def run_sma_crossover_backtest(
    code: str,
    *,
    range_: str = "1y",
    fast: int = 20,
    slow: int = 50,
    capital: float = 100_000.0,
    slippage: float = 0.001,
    commission: float = 0.0003,
) -> dict:
    """Roda backtest SMA crossover em ação B3 via brapi.dev."""
    print(f"📊 Baixando {code} ({range_}) via brapi.dev...")
    df = fetch_ohlcv(code, range_=range_)
    if df is None or df.empty:
        raise RuntimeError(f"brapi.dev não retornou dados pra {code}")
    print(f"   → {len(df)} pregões: {df.index[0].date()} → {df.index[-1].date()}")

    # Indicadores
    df[f"sma{fast}"] = df["close"].rolling(fast).mean()
    df[f"sma{slow}"] = df["close"].rolling(slow).mean()
    df["signal"] = 0
    df.loc[df[f"sma{fast}"] > df[f"sma{slow}"], "signal"] = 1
    df["position"] = df["signal"].shift(1).fillna(0)  # atua D+1

    # Simulação
    cash = capital
    shares = 0
    entry_price = 0.0
    trades = []
    equity_curve = []

    for date, row in df.iterrows():
        price = float(row["close"])
        target_pos = int(row["position"])
        equity_today = cash + shares * price
        equity_curve.append({
            "date": date, "equity": equity_today, "close": price,
            "position": shares > 0, "sma_fast": row[f"sma{fast}"],
            "sma_slow": row[f"sma{slow}"],
        })

        # Compra
        if target_pos == 1 and shares == 0:
            buy_price = price * (1 + slippage)
            cost_per_share = buy_price * (1 + commission)
            qty = int(cash / cost_per_share)
            if qty > 0:
                cash -= qty * cost_per_share
                shares = qty
                entry_price = buy_price
                trades.append({"date": date, "side": "BUY", "qty": qty, "price": buy_price})

        # Venda
        elif target_pos == 0 and shares > 0:
            sell_price = price * (1 - slippage)
            proceeds = sell_price * shares * (1 - commission)
            cash += proceeds
            pnl = proceeds - shares * entry_price * (1 + commission)
            trades.append({
                "date": date, "side": "SELL", "qty": shares,
                "price": sell_price, "pnl": pnl,
            })
            shares = 0
            entry_price = 0.0

    # Fecha posição final
    if shares > 0:
        last_price = float(df["close"].iloc[-1])
        sell_price = last_price * (1 - slippage)
        proceeds = sell_price * shares * (1 - commission)
        cash += proceeds
        pnl = proceeds - shares * entry_price * (1 + commission)
        trades.append({
            "date": df.index[-1], "side": "SELL_FINAL", "qty": shares,
            "price": sell_price, "pnl": pnl,
        })

    # === Relatório ===
    print("\n" + "=" * 60)
    q = fetch_quote(code)
    if q:
        print(f"📈  BACKTEST {code} ({q.get('longName') or '?'}) — SMA({fast}) vs SMA({slow})")
    else:
        print(f"📈  BACKTEST {code} — SMA({fast}) vs SMA({slow})")
    print(f"     Fonte: brapi.dev  •  Período: {df.index[0].date()} → {df.index[-1].date()}")
    print("=" * 60)

    ec = pd.DataFrame(equity_curve).set_index("date")
    total_return = (cash - capital) / capital * 100
    n_years = max((df.index[-1] - df.index[0]).days / 365.25, 0.01)
    cagr = ((cash / capital) ** (1 / n_years) - 1) * 100

    # Buy & Hold
    bh_qty = int(capital / float(df["close"].iloc[0]) / (1 + commission))
    bh_final = bh_qty * float(df["close"].iloc[-1]) * (1 - commission)
    bh_return = (bh_final - capital) / capital * 100

    # Drawdown
    ec["peak"] = ec["equity"].cummax()
    ec["dd"] = (ec["equity"] - ec["peak"]) / ec["peak"]
    max_dd = ec["dd"].min() * 100

    # Sharpe
    ec["ret"] = ec["equity"].pct_change().fillna(0)
    sharpe = (ec["ret"].mean() / ec["ret"].std()) * np.sqrt(252) if ec["ret"].std() > 0 else 0

    print(f"\n💰 Capital inicial:   R$ {capital:>12,.2f}")
    print(f"💵 Capital final:     R$ {cash:>12,.2f}")
    print(f"📊 Retorno total:     {total_return:>+6.2f}%")
    print(f"📅 CAGR:              {cagr:>+6.2f}%   ({n_years:.2f} anos)")
    print(f"📉 Max drawdown:      {max_dd:>+6.2f}%")
    print(f"⚡ Sharpe anualizado: {sharpe:>6.2f}")

    if q:
        print(f"\n📋 Fundamental (brapi.dev):")
        print(f"   Preço:        R$ {q.get('regularMarketPrice')}")
        print(f"   Variação:     {q.get('regularMarketChangePercent'):+.2f}%")
        print(f"   P/L:          {q.get('priceEarnings')}")
        print(f"   LPA:          R$ {q.get('earningsPerShare')}")
        print(f"   Market cap:   R$ {q.get('marketCap')/1e9:.1f} bi")
        print(f"   52w range:    R$ {q.get('fiftyTwoWeekLow')} - R$ {q.get('fiftyTwoWeekHigh')}")

    print(f"\n🎯 Buy & Hold benchmark:")
    print(f"   Retorno:           {bh_return:>+6.2f}%")
    alpha = total_return - bh_return
    emoji = "✅" if alpha > 0 else "❌"
    print(f"   Alpha (vs B&H):    {alpha:>+6.2f}%  {emoji}")

    n_round_trips = len([t for t in trades if t["side"] in ("BUY", "SELL")])
    n_trades = len([t for t in trades if t["side"] == "SELL"])
    wins = len([t for t in trades if t.get("pnl", 0) > 0 and t["side"] == "SELL"])
    print(f"\n🔄 Trades: {n_round_trips} round-trips ({n_trades} vendas)")
    if n_trades > 0:
        win_rate = wins / n_trades * 100
        print(f"   Win rate: {win_rate:.0f}% ({wins}/{n_trades})")
    for t in trades:
        side = t["side"]
        qty = t["qty"]
        px = t["price"]
        if "pnl" in t:
            emoji = "✅" if t["pnl"] > 0 else "❌"
            print(f"   {t['date'].date()}  {side:12s}  {qty:>5d}  R${px:>7.2f}  {emoji} PnL R$ {t['pnl']:>+9,.2f}")
        else:
            print(f"   {t['date'].date()}  {side:12s}  {qty:>5d}  R${px:>7.2f}")

    out = Path("/tmp") / f"{code.lower()}_brapi_backtest.csv"
    ec[["equity", "close", "sma_fast", "sma_slow", "position"]].to_csv(out)
    print(f"\n💾 Equity curve salva em: {out}")

    return {
        "code": code, "total_return": total_return, "cagr": cagr,
        "sharpe": sharpe, "max_dd": max_dd, "n_trades": n_trades,
        "bh_return": bh_return, "alpha": alpha, "final_equity": cash,
    }


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "VALE3"
    rng = sys.argv[2] if len(sys.argv) > 2 else "1y"
    run_sma_crossover_backtest(code, range_=rng)
