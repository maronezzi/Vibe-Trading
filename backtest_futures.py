"""Backtest de futuros B3 (WIN, WDO) com MT5.

Diferenças vs backtest de ações:
  - Alavancagem: 1 contrato WIN = R$ 0.20/ponto (~R$ 2/pontuação mínima)
  - Margem por contrato: ~R$ 1.500 (WIN), ~R$ 200 (WDO)
  - Pode short (gain na queda)
  - Stop loss / take profit por pontos
  - Horário: 09:00-17:55 (WIN/WDO diurno), 17:30-02:30 (after)

Por padrão, opera só comprado (long-only) com 1 contrato fixo.
"""

import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent / "agent"))

import numpy as np
import pandas as pd

from backtest.loaders.mt5_loader import fetch_ohlcv


# Configurações por símbolo
FUTURES_CONFIG = {
    "WIN": {
        "name": "Mini Ibovespa",
        "tick_value": 0.20,        # R$ por ponto
        "tick_size": 5,            # 1 ponto = 5 ticks
        "min_lot": 1,              # contratos mínimos
        "margin_per_contract": 1500.0,  # R$ margem B3
        "currency": "BRL",
    },
    "WDO": {
        "name": "Mini Dólar",
        "tick_value": 10.0,        # US$ por 0.5 ponto
        "tick_size": 0.5,
        "min_lot": 1,
        "margin_per_contract": 200.0,
        "currency": "BRL",
    },
    "IND": {
        "name": "Ibovespa Cheio",
        "tick_value": 1.0,
        "tick_size": 5,
        "min_lot": 1,
        "margin_per_contract": 7500.0,
        "currency": "BRL",
    },
    "DOL": {
        "name": "Dólar Cheio",
        "tick_value": 50.0,
        "tick_size": 0.5,
        "min_lot": 1,
        "margin_per_contract": 1000.0,
        "currency": "BRL",
    },
}


def sma_crossover_signals(df: pd.DataFrame, fast: int = 20, slow: int = 50) -> pd.Series:
    """Sinal: 1 = comprado, 0 = fora, -1 = vendido (short)."""
    sig = pd.Series(0, index=df.index, dtype=int)
    fast_ma = df["close"].rolling(fast).mean()
    slow_ma = df["close"].rolling(slow).mean()
    sig = (fast_ma > slow_ma).astype(int)
    return sig.shift(1).fillna(0).rename("sma")


def rsi_signals(df: pd.DataFrame, period: int = 14, low: int = 30, high: int = 70) -> pd.Series:
    """Long-short com RSI: <30 compra, >70 vende (short)."""
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = -delta.where(delta < 0, 0).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[rsi < low] = 1
    sig[rsi > high] = -1
    return sig.shift(1).fillna(0).rename("rsi")


def run_futures_backtest(
    symbol: str,
    *,
    timeframe: str = "M5",
    n_bars: int = 5000,
    strategy: str = "sma",
    fast: int = 20,
    slow: int = 50,
    capital: float = 50_000.0,
    max_contracts: int = 1,
    stop_loss_pts: Optional[float] = None,
    take_profit_pts: Optional[float] = None,
    long_only: bool = True,
    long_short: bool = False,
) -> dict:
    """Roda backtest de futuro B3.

    Args:
        symbol: WIN | WDO | IND | DOL.
        timeframe: M1, M5, M15, M30, H1, D1.
        n_bars: Quantidade de barras.
        strategy: 'sma' | 'rsi'.
        capital: Caixa inicial.
        max_contracts: Máximo de contratos simultâneos.
        stop_loss_pts: Stop loss em pontos (None = desabilitado).
        take_profit_pts: Take profit em pontos (None = desabilitado).
        long_only: Se True, só comprado.
        long_short: Se True, permite short.
    """
    cfg = FUTURES_CONFIG.get(symbol.upper())
    if cfg is None:
        raise ValueError(f"Símbolo {symbol} não configurado. Use {list(FUTURES_CONFIG)}")

    print(f"📊 Baixando {symbol} ({cfg['name']}) — {timeframe} via MT5...")
    df = fetch_ohlcv(symbol, timeframe=timeframe, n_bars=n_bars)
    if df is None or df.empty:
        raise RuntimeError(f"MT5 não retornou dados pra {symbol}")
    print(f"   → {len(df)} barras: {df.index[0]} → {df.index[-1]}")

    # Sinais
    if strategy == "sma":
        sig = sma_crossover_signals(df, fast=fast, slow=slow)
    elif strategy == "rsi":
        sig = rsi_signals(df)
    else:
        raise ValueError(f"Estratégia {strategy!r} não suportada")

    if long_only:
        sig = sig.clip(lower=0)

    # Simulação com 1 contrato (default)
    position = 0
    entry_price = 0.0
    entry_date = df.index[0]  # inicializa pra evitar unbound warning
    cash = capital
    pnl_per_point = cfg["tick_value"] / cfg["tick_size"]  # R$/ponto
    trades = []
    equity_list = []

    for ts, row in df.iterrows():
        price = float(row["close"])
        # Marca equity pelo mark-to-market do contrato
        # 1 ponto = tick_value/tick_size em R$
        pnl_per_point = cfg["tick_value"] / cfg["tick_size"]  # R$/ponto
        unrealized = position * (price - entry_price) * pnl_per_point
        equity_today = cash + unrealized
        equity_list.append(equity_today)

        target_pos = int(sig.loc[ts])

        # Stop / take
        if position != 0:
            pts = (price - entry_price) * (1 if position > 0 else -1)
            if stop_loss_pts and pts <= -stop_loss_pts:
                target_pos = 0  # stopou
            elif take_profit_pts and pts >= take_profit_pts:
                target_pos = 0  # atingiu target

        # Entrada/saída
        if target_pos != position:
            # Fecha posição atual
            if position != 0:
                pnl = position * (price - entry_price) * pnl_per_point
                cash += pnl
                trades.append({
                    "entry_date": entry_date, "exit_date": ts,
                    "side": "LONG" if position > 0 else "SHORT",
                    "entry_price": entry_price, "exit_price": price,
                    "pnl": pnl, "pts": (price - entry_price) * (1 if position > 0 else -1),
                })
                position = 0

            # Abre nova (se target != 0)
            if target_pos != 0:
                if long_short or target_pos > 0:
                    position = target_pos
                    entry_price = price
                    entry_date = ts

    # Fecha posição final
    if position != 0:
        last = float(df["close"].iloc[-1])
        pnl = position * (last - entry_price) * pnl_per_point
        cash += pnl
        trades.append({
            "entry_date": entry_date, "exit_date": df.index[-1],
            "side": "LONG" if position > 0 else "SHORT",
            "entry_price": entry_price, "exit_price": last,
            "pnl": pnl, "pts": (last - entry_price) * (1 if position > 0 else -1),
        })

    # Relatório
    print("\n" + "=" * 70)
    print(f"📈  BACKTEST {symbol} ({cfg['name']}) — {strategy.upper()} — TF {timeframe}")
    print("=" * 70)

    eq = pd.Series(equity_list, index=df.index[:len(equity_list)])
    total_pnl = cash - capital
    total_return = total_pnl / capital * 100
    n_years = max((df.index[-1] - df.index[0]).days / 365.25, 0.01)
    cagr = ((cash / capital) ** (1 / n_years) - 1) * 100 if cash > 0 else -100

    dd = (eq - eq.cummax())
    max_dd_pts = dd.min()
    max_dd = max_dd_pts / capital * 100
    rets = eq.pct_change().fillna(0)
    sharpe = (rets.mean() / rets.std()) * np.sqrt(252 * 24 * 12) if rets.std() > 0 else 0  # anualiza intraday

    print(f"\n💰 Capital inicial:    R$ {capital:>12,.2f}")
    print(f"💵 Capital final:      R$ {cash:>12,.2f}")
    print(f"📊 PnL total:          R$ {total_pnl:>+12,.2f}  ({total_return:+.2f}%)")
    print(f"📅 Período:            {n_years:.2f} anos")
    print(f"📉 Max drawdown:       R$ {max_dd_pts:>+10,.2f}  ({max_dd:+.2f}%)")
    print(f"⚡ Sharpe anualizado:  {sharpe:>6.2f}")
    print(f"📦 Contratos:          {max_contracts}  (1 contrato = R$ {cfg['margin_per_contract']:.0f} margem)")
    print(f"🎯 Stop/Target:        {stop_loss_pts or 'off'} / {take_profit_pts or 'off'} pts")

    n_trades = len(trades)
    win_rate = 0.0
    profit_factor = 0.0
    if n_trades > 0:
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        win_rate = len(wins) / n_trades * 100
        avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0
        avg_loss = np.mean([t["pnl"] for t in losses]) if losses else 0
        profit_factor = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))) if losses else float("inf")

        print(f"\n🔄 Trades: {n_trades}")
        print(f"   Win rate:    {win_rate:.0f}% ({len(wins)}/{n_trades})")
        print(f"   Avg win:     R$ {avg_win:>+10,.2f}")
        print(f"   Avg loss:    R$ {avg_loss:>+10,.2f}")
        print(f"   Profit fact: {profit_factor:>5.2f}")

        # Mostra últimos 5 trades
        print(f"\n📋 Últimos 5 trades:")
        for t in trades[-5:]:
            emoji = "✅" if t["pnl"] > 0 else "❌"
            print(f"   {emoji} {t['entry_date'].date()} → {t['exit_date'].date()}  "
                  f"{t['side']:5s}  {t['entry_price']:>8.2f} → {t['exit_price']:>8.2f}  "
                  f"PnL R$ {t['pnl']:>+8,.2f} ({t['pts']:+.0f} pts)")

    out = Path("/tmp") / f"{symbol.lower()}_{timeframe}_{strategy}.csv"
    eq.to_csv(out, header=["equity"])
    print(f"\n💾 Equity curve salva em: {out}")

    return {
        "symbol": symbol,
        "strategy": strategy,
        "total_return": total_return,
        "pnl": total_pnl,
        "sharpe": sharpe,
        "max_dd": max_dd,
        "n_trades": n_trades,
        "win_rate": win_rate if n_trades else 0,
        "profit_factor": profit_factor if n_trades else 0,
    }


if __name__ == "__main__":
    import sys
    symbol = sys.argv[1] if len(sys.argv) > 1 else "WIN"
    tf = sys.argv[2] if len(sys.argv) > 2 else "M5"
    strategy = sys.argv[3] if len(sys.argv) > 3 else "sma"
    n = int(sys.argv[4]) if len(sys.argv) > 4 else 5000

    run_futures_backtest(symbol, timeframe=tf, strategy=strategy, n_bars=n)
