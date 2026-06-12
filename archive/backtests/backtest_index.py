"""Backtest de índice Brasil (BOVA11) + EUA (IVVB11) + comparativo.

Usa yfinance direto (zero Wine/MT5/rate limit). BOVA11 replica Ibovespa
99.8%, IVVB11 replica S&P500 em BRL com hedge cambial inverso.

Roda as 4 estratégias (SMA, RSI, Bollinger, Momentum) e compara
BOVA11 vs IVVB11 vs 50/50 portfolio.

Uso: PYTHONPATH=./agent ./agent/venv/bin/python backtest_index.py [period] [interval]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "agent"))

import numpy as np
import pandas as pd

from backtest.loaders.index_etf_loader import fetch_ohlcv, INDEX_ETFS


# === Estratégias (mesmas do backtest_strategies.py) ===

def sma_signals(df, fast=20, slow=50):
    sig = (df["close"].rolling(fast).mean() > df["close"].rolling(slow).mean()).astype(int)
    return sig.shift(1).fillna(0)


def rsi_signals(df, period=14, low=30, high=70):
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = -delta.where(delta < 0, 0).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[rsi < low] = 1
    sig[rsi > high] = 0
    return sig.shift(1).fillna(0).ffill().fillna(0)


def bollinger_signals(df, period=20, std=2.0):
    sma = df["close"].rolling(period).mean()
    sd = df["close"].rolling(period).std()
    lower, upper = sma - std * sd, sma + std * sd
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[df["close"] < lower] = 1
    sig[df["close"] > upper] = 0
    return sig.shift(1).fillna(0).ffill().fillna(0)


def momentum_signals(df, lookback=60, sma_filter=200):
    ret = df["close"].pct_change(lookback)
    if len(df) >= sma_filter:
        sma200 = df["close"].rolling(sma_filter).mean()
        above = df["close"] > sma200
    else:
        above = True
    return ((ret > 0) & above).astype(int).shift(1).fillna(0)


STRATEGIES = {
    "SMA(20,50)  ": sma_signals,
    "RSI(14)     ": rsi_signals,
    "Bollinger   ": bollinger_signals,
    "Momentum60  ": momentum_signals,
}


def backtest(df, signals, *, capital=100_000.0, slippage=0.001, commission=0.0003):
    if len(signals) != len(df):
        return {"ok": False, "reason": "tamanho diferente"}
    cash, shares, entry_price = capital, 0, 0.0
    equity_list, n_trades, n_wins = [], 0, 0

    for date, row in df.iterrows():
        price = float(row["close"])
        target_pos = int(signals.loc[date])
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

    bh_qty = int(capital / float(df["close"].iloc[0]) / (1 + commission))
    bh_final = bh_qty * float(df["close"].iloc[-1]) * (1 - commission)

    eq = pd.Series(equity_list, index=df.index[:len(equity_list)])
    total_return = (cash - capital) / capital * 100
    bh_return = (bh_final - capital) / capital * 100
    n_years = max((df.index[-1] - df.index[0]).days / 365.25, 0.01)
    cagr = ((cash / capital) ** (1 / n_years) - 1) * 100
    dd = (eq - eq.cummax()) / eq.cummax()
    max_dd = dd.min() * 100
    rets = eq.pct_change().fillna(0)
    sharpe = (rets.mean() / rets.std()) * np.sqrt(252) if rets.std() > 0 else 0.0

    return {
        "ok": True, "trades": n_trades, "wins": n_wins,
        "win_rate": (n_wins / n_trades * 100) if n_trades else 0,
        "total_return": total_return, "cagr": cagr, "sharpe": sharpe,
        "max_dd": max_dd, "bh_return": bh_return, "alpha": total_return - bh_return,
    }


def live_state(df):
    """Sinal ao vivo com base no último bar."""
    close = df["close"]
    state = {}

    # SMA
    if len(close) >= 50:
        sma20, sma50 = close.rolling(20).mean(), close.rolling(50).mean()
        diff_now = sma20.iloc[-1] - sma50.iloc[-1]
        diff_prev = sma20.iloc[-2] - sma50.iloc[-2]
        if diff_now > 0 and diff_prev <= 0:
            state["sma"] = ("CRUZOU AGORA ↑", "buy")
        elif diff_now > 0:
            state["sma"] = ("em alta", "hold")
        else:
            state["sma"] = ("em baixa", "sell")
    else:
        state["sma"] = ("dados <50", "neutral")

    # RSI
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = -delta.where(delta < 0, 0).rolling(14).mean()
    rsi = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))
    rsi_now = float(rsi.iloc[-1])
    if rsi_now < 30:
        state["rsi"] = (f"RSI {rsi_now:.0f} SOBREVENDIDO", "buy")
    elif rsi_now > 70:
        state["rsi"] = (f"RSI {rsi_now:.0f} SOBRECOMPRADO", "sell")
    else:
        state["rsi"] = (f"RSI {rsi_now:.0f}", "neutral")

    # Bollinger
    sma, sd = close.rolling(20).mean(), close.rolling(20).std()
    lower, upper = sma - 2*sd, sma + 2*sd
    pct_b = (close.iloc[-1] - lower.iloc[-1]) / (upper.iloc[-1] - lower.iloc[-1])
    if pct_b < 0:
        state["bb"] = (f"%B {pct_b:.2f} ABAIXO banda inf", "buy")
    elif pct_b > 1:
        state["bb"] = (f"%B {pct_b:.2f} ACIMA banda sup", "sell")
    else:
        state["bb"] = (f"%B {pct_b:.2f}", "neutral")

    # Momentum
    if len(close) >= 60:
        ret_60 = close.iloc[-1] / close.iloc[-60] - 1
        if len(close) >= 200:
            sma200 = close.rolling(200).mean().iloc[-1]
            above = close.iloc[-1] > sma200
        else:
            above = None
        if ret_60 > 0 and (above is None or above):
            tag = "buy" if above is None or above else "neutral"
            state["mom"] = (f"ret 60d +{ret_60*100:.1f}%" + ("" if above is None else " >SMA200"), tag)
        else:
            state["mom"] = (f"ret 60d {ret_60*100:.1f}%", "sell")
    else:
        state["mom"] = ("dados <60", "neutral")

    return state


# === Execução ===

INDEX_TICKERS = ["BOVA11", "IVVB11", "NASD11", "SMAL11"]


def run(period: str = "2y", interval: str = "1d"):
    print(f"\n🚀 BACKTEST ÍNDICES VIA ETF (yfinance) — {period}/{interval}")
    print(f"   Sem Wine, sem MT5, sem rate limit\n")

    # Backtest
    all_results = []
    live_results = []
    for code in INDEX_TICKERS:
        try:
            df = fetch_ohlcv(code, period=period, interval=interval)
            if df is None or df.empty:
                print(f"  {code:6s} ❌ sem dados")
                continue
            price_now = float(df["close"].iloc[-1])
            ret_total = (price_now / float(df["close"].iloc[0]) - 1) * 100

            print(f"  {code:6s} {INDEX_ETFS[code]['name']:30s}  "
                  f"{len(df):>4} pregões  ret {ret_total:+6.2f}%  R$ {price_now:>7.2f}")

            row = {"code": code, "name": INDEX_ETFS[code]["name"],
                   "tracks": INDEX_ETFS[code]["tracks"], "price": price_now, "ret_total": ret_total}
            for name, fn in STRATEGIES.items():
                sig = fn(df)
                r = backtest(df, sig)
                if r["ok"]:
                    key = name.strip()
                    row[f"{key}_ret"] = r["total_return"]
                    row[f"{key}_sharpe"] = r["sharpe"]
                    row[f"{key}_alpha"] = r["alpha"]
            all_results.append(row)

            state = live_state(df)
            buys = sum(1 for _, (_, t) in state.items() if t == "buy")
            sells = sum(1 for _, (_, t) in state.items() if t == "sell")
            live_results.append({
                "code": code, "name": INDEX_ETFS[code]["name"], "price": price_now,
                "ret_total": ret_total, "state": state, "buys": buys, "sells": sells,
                "neutral": 4 - buys - sells,
            })
        except Exception as e:
            print(f"  {code:6s} 💥 {e}")

    # Tabela de resultados
    if all_results:
        print("\n" + "=" * 90)
        print(f"📊  RESULTADO POR ESTRATÉGIA (período: {period})")
        print("=" * 90)
        print(f"\n{'TICKER':<8} {'NOME':<30} {'SMA':>10} {'RSI':>10} {'BB':>10} {'MOM':>10}   {'B&H':>8}")
        print("-" * 90)
        for r in all_results:
            print(f"{r['code']:<8} {r['name'][:30]:<30} "
                  f"{r.get('SMA(20,50)_ret', 0):>+9.2f}% "
                  f"{r.get('RSI(14)_ret', 0):>+9.2f}% "
                  f"{r.get('Bollinger_ret', 0):>+9.2f}% "
                  f"{r.get('Momentum60_ret', 0):>+9.2f}%   "
                  f"{r['ret_total']:>+7.2f}%")

        # Melhor estratégia
        print(f"\n🏆 MELHOR ESTRATÉGIA POR ATIVO:")
        print("-" * 90)
        for r in all_results:
            strats = {
                "SMA": r.get("SMA(20,50)_ret", -999), "RSI": r.get("RSI(14)_ret", -999),
                "BB": r.get("Bollinger_ret", -999), "MOM": r.get("Momentum60_ret", -999),
            }
            best = max(strats, key=lambda k: strats[k])
            print(f"   {r['code']:6s} → {best:<10s}  ret {strats[best]:>+6.2f}%   "
                  f"sharpe {r.get(f'{best}_sharpe', 0):>5.2f}")

    # Sinais ao vivo
    if live_results:
        print("\n" + "=" * 110)
        print("🔴🟢  SINAIS AO VIVO")
        print("=" * 110)
        print(f"\n{'TICKER':<8} {'PREÇO':>9} {'RET':>7}   {'SMA(20,50)':<24} {'RSI(14)':<22} {'Bollinger':<22} {'Momentum60':<22} SCORE")
        print("-" * 135)
        for r in live_results:
            s = r["state"]
            def fmt(key):
                txt, tag = s[key]
                if tag == "buy": return f"🟢 {txt:<22}"
                elif tag == "sell": return f"🔴 {txt:<22}"
                else: return f"⚪ {txt:<22}"
            score = f"{r['buys']}🟢/{r['sells']}🔴/{r['neutral']}⚪"
            print(f"{r['code']:<8} R${r['price']:>7.2f} {r['ret_total']:>+6.2f}%   "
                  f"{fmt('sma'):<24} {fmt('rsi'):<22} {fmt('bb'):<22} {fmt('mom'):<22} {score}")

        # Recomendações
        print("\n" + "=" * 90)
        print("🎯  RECOMENDAÇÃO — onde comprar índice HOJE")
        print("=" * 90)
        strong = [r for r in live_results if r["buys"] >= 2]
        moderate = [r for r in live_results if r["buys"] == 1 and r["sells"] == 0]
        avoid = [r for r in live_results if r["sells"] >= 1]

        print(f"\n🟢 COMPRA FORTE (2+ estratégias comprando):")
        if not strong: print("   (nenhum)")
        for r in strong:
            s = r["state"]
            print(f"   ✅ {r['code']:6s} {r['name'][:30]:<30}  R$ {r['price']:>7.2f}  "
                  f"({', '.join(k for k, (_, t) in s.items() if t == 'buy')})")

        print(f"\n🟡 COMPRA MODERADA (1 comprando, 0 vendendo):")
        if not moderate: print("   (nenhum)")
        for r in moderate:
            s = r["state"]
            buy_strat = next(k for k, (_, t) in s.items() if t == "buy")
            print(f"   • {r['code']:6s} {r['name'][:30]:<30}  R$ {r['price']:>7.2f}  ({buy_strat})")

        print(f"\n🔴 EVITAR (1+ estratégias vendendo):")
        if not avoid: print("   (nenhum — cenário raro!)")
        for r in avoid:
            s = r["state"]
            print(f"   ❌ {r['code']:6s} {r['name'][:30]:<30}  R$ {r['price']:>7.2f}  "
                  f"({', '.join(k for k, (_, t) in s.items() if t == 'sell')})")

    # Salvar
    if all_results:
        out = Path("/tmp") / f"index_backtest_{period}_{interval}.csv"
        pd.DataFrame(all_results).to_csv(out, index=False)
        print(f"\n💾 Salvo em: {out}")


if __name__ == "__main__":
    period = sys.argv[1] if len(sys.argv) > 1 else "2y"
    interval = sys.argv[2] if len(sys.argv) > 2 else "1d"
    run(period, interval)
