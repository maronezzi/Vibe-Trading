"""Backtest simples de VALE3.SA usando SMA crossover via yfinance + Vibe-Trading.

Estratégia: SMA(20) vs SMA(50)
- Compra quando SMA20 cruza SMA50 pra cima (golden cross)
- Vende quando SMA20 cruza SMA50 pra baixo (death cross)
- Sem alavancagem, sem short
- Capital inicial: R$100.000
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

# Carregar dados via yfinance
print("📊 Baixando VALE3.SA (2024-01-01 → 2025-06-07)...")
df = yf.download("VALE3.SA", start="2024-01-01", end="2025-06-07", progress=False)
print(f"   → {len(df)} pregões carregados")

# Ajustar MultiIndex columns (yfinance >= 0.2.31)
if isinstance(df.columns, pd.MultiIndex):
    df.columns = df.columns.get_level_values(0)
df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
df.columns = ["open", "high", "low", "close", "volume"]
df.index.name = "trade_date"

# Indicadores
df["sma20"] = df["close"].rolling(20).mean()
df["sma50"] = df["close"].rolling(50).mean()
df["signal"] = 0
df.loc[df["sma20"] > df["sma50"], "signal"] = 1   # comprado
df["position"] = df["signal"].shift(1).fillna(0) # atua no dia seguinte

# Simular equity
CAPITAL = 100_000.0
SLIPPAGE = 0.001  # 0.1% (B3 típico)
COMMISSION = 0.0003  # corretagem

cash = CAPITAL
shares = 0
entry_price = 0.0
trades = []
equity_curve = []

for date, row in df.iterrows():
    price = float(row["close"])
    target_pos = int(row["position"])

    # Calcular equity do dia
    equity_today = cash + shares * price
    equity_curve.append({"date": date, "equity": equity_today, "close": price, "position": shares > 0})

    # Sinal: alvo=1, hoje=0 → comprar
    if target_pos == 1 and shares == 0:
        buy_price = price * (1 + SLIPPAGE)
        cost = buy_price * (1 + COMMISSION)
        # usa 100% do capital
        qty = int(cash / cost)
        if qty > 0:
            cash -= qty * cost
            shares = qty
            entry_price = buy_price
            trades.append({"date": date, "side": "BUY", "qty": qty, "price": buy_price})

    # Sinal: alvo=0, hoje=1 → vender
    elif target_pos == 0 and shares > 0:
        sell_price = price * (1 - SLIPPAGE)
        proceeds = sell_price * shares * (1 - COMMISSION)
        cash += proceeds
        pnl = proceeds - shares * entry_price * (1 + COMMISSION)
        trades.append({"date": date, "side": "SELL", "qty": shares, "price": sell_price, "pnl": pnl})
        shares = 0
        entry_price = 0.0

# Fechar posição no final
if shares > 0:
    last_price = float(df["close"].iloc[-1])
    sell_price = last_price * (1 - SLIPPAGE)
    proceeds = sell_price * shares * (1 - COMMISSION)
    cash += proceeds
    pnl = proceeds - shares * entry_price * (1 + COMMISSION)
    trades.append({"date": df.index[-1], "side": "SELL_FINAL", "qty": shares, "price": sell_price, "pnl": pnl})

# === Relatório ===
print("\n" + "="*60)
print("📈  BACKTEST VALE3.SA — SMA(20) vs SMA(50)")
print("="*60)

ec = pd.DataFrame(equity_curve).set_index("date")
total_return = (cash - CAPITAL) / CAPITAL * 100
n_years = (df.index[-1] - df.index[0]).days / 365.25
cagr = ((cash / CAPITAL) ** (1 / n_years) - 1) * 100 if n_years > 0 else 0

# Buy & Hold benchmark
bh_qty = int(CAPITAL / float(df["close"].iloc[0]) / (1 + COMMISSION))
bh_final = bh_qty * float(df["close"].iloc[-1]) * (1 - COMMISSION)
bh_return = (bh_final - CAPITAL) / CAPITAL * 100

# Drawdown
ec["peak"] = ec["equity"].cummax()
ec["dd"] = (ec["equity"] - ec["peak"]) / ec["peak"]
max_dd = ec["dd"].min() * 100

# Sharpe anualizado (retornos diários)
ec["ret"] = ec["equity"].pct_change().fillna(0)
sharpe = (ec["ret"].mean() / ec["ret"].std()) * np.sqrt(252) if ec["ret"].std() > 0 else 0

print(f"\n💰 Capital inicial:   R$ {CAPITAL:>12,.2f}")
print(f"💵 Capital final:     R$ {cash:>12,.2f}")
print(f"📊 Retorno total:     {total_return:>+6.2f}%")
print(f"📅 CAGR:              {cagr:>+6.2f}%   ({n_years:.2f} anos)")
print(f"📉 Max drawdown:      {max_dd:>+6.2f}%")
print(f"⚡ Sharpe anualizado: {sharpe:>6.2f}")

print(f"\n🎯 Buy & Hold benchmark:")
print(f"   Retorno:           {bh_return:>+6.2f}%")

print(f"\n🔄 Trades executados: {len([t for t in trades if t['side'] in ('BUY','SELL')])}")
for t in trades:
    side = t["side"]
    qty = t["qty"]
    px = t["price"]
    if "pnl" in t:
        emoji = "✅" if t["pnl"] > 0 else "❌"
        print(f"   {t['date'].date()}  {side:12s}  {qty:>5d}  R${px:>7.2f}  {emoji} PnL R$ {t['pnl']:>+9,.2f}")
    else:
        print(f"   {t['date'].date()}  {side:12s}  {qty:>5d}  R${px:>7.2f}")

# Salvar equity curve
out = Path("/tmp/vale3_backtest.csv")
ec[["equity", "close"]].to_csv(out)
print(f"\n💾 Equity curve salva em: {out}")
