"""
Backtest do Direction Classifier — Walk-Forward.

Usa walk-forward validation: treina no passado, prevê o futuro, rola.
Módulo 100% isolado.
"""

import numpy as np
import pandas as pd

from prediction.utils.features import prepare_dataset, get_feature_columns


def backtest_walk_forward(
    df: pd.DataFrame,
    symbol: str = "WDO$",
    timeframe: str = "M5",
    horizon: int = 1,
    min_confidence: float = 0.55,
    initial_capital: float = 100000,
    contract_multiplier: float = 0.20,  # WDO: R$0.20/ponto
    train_ratio: float = 0.30,  # % do histórico pra treinar cada fold
    retrain_every: int = 100,  # re-treinar a cada N candles
    n_folds: int = 5,
) -> dict:
    """
    Walk-Forward Backtest.
    
    Divide os dados em N folds. Para cada fold:
    1. Treina no histórico disponível (rolling window)
    2. Prediz no período seguinte
    3. Coleta métricas
    
    Isso simula o cenário real: treinar com dados passados, prever o futuro.
    """
    # Preparar dataset completo com features
    dataset = prepare_dataset(df, horizon=horizon)
    feature_cols = get_feature_columns()
    
    total = len(dataset)
    if total < 500:
        return {"error": f"Dados insuficientes: {total} (mínimo 500)"}
    
    # Dividir em folds temporais
    fold_size = total // (n_folds + 2)  # +2 pra ter train buffer
    
    all_trades = []
    equity = initial_capital
    equity_curve = [initial_capital]
    
    print(f"\n{'='*60}")
    print(f"WALK-FORWARD BACKTEST: {symbol} {timeframe}")
    print(f"{'='*60}")
    print(f"Total candles: {total} | Folds: {n_folds} | Fold size: {fold_size}")
    print(f"Min confidence: {min_confidence:.0%}")
    
    for fold in range(n_folds):
        # Definir janela de treino e teste
        train_end = fold_size * (fold + 2)  # começa com 2 folds de treino
        test_start = train_end
        test_end = min(test_start + fold_size, total)
        
        if test_start >= total:
            break
        
        train_data = dataset.iloc[:train_end]
        test_data = dataset.iloc[test_start:test_end]
        
        if len(test_data) < 10:
            continue
        
        # Treinar modelo neste fold
        from prediction.models.direction_classifier import DirectionClassifier
        clf = DirectionClassifier(symbol=symbol, timeframe=timeframe, min_confidence=min_confidence)
        
        X_train = train_data[feature_cols].astype(float)
        y_train = train_data["target"].astype(int)
        
        import lightgbm as lgb
        clf.model = lgb.LGBMClassifier(
            n_estimators=300,
            learning_rate=0.05,
            max_depth=5,
            num_leaves=31,
            min_child_samples=30,
            subsample=0.8,
            colsample_bytree=0.8,
            reg_alpha=0.1,
            reg_lambda=0.1,
            random_state=42,
            verbose=-1,
        )
        clf.model.fit(X_train, y_train)
        
        # Predizer no período de teste
        fold_trades = []
        for i in range(len(test_data)):
            row = test_data.iloc[i]
            X_row = row[feature_cols].to_frame().T.astype(float)
            
            try:
                signal = clf.predict(X_row)
            except Exception:
                equity_curve.append(equity)
                continue
            
            if not signal["actionable"]:
                equity_curve.append(equity)
                continue
            
            # Simular trade
            entry_price = row["close"]
            is_long = signal["direction"] == "UP"
            
            if i + horizon < len(test_data):
                exit_row = test_data.iloc[i + horizon]
                exit_price = exit_row["close"]
                
                if is_long:
                    pnl_points = exit_price - entry_price
                else:
                    pnl_points = entry_price - exit_price
                
                pnl_brl = pnl_points * contract_multiplier
                equity += pnl_brl
                
                fold_trades.append({
                    "fold": fold + 1,
                    "datetime": str(row["datetime"]),
                    "direction": "LONG" if is_long else "SHORT",
                    "entry": round(entry_price, 2),
                    "exit": round(exit_price, 2),
                    "pnl_points": round(pnl_points, 2),
                    "pnl_brl": round(pnl_brl, 2),
                    "confidence": signal["confidence"],
                })
            
            equity_curve.append(equity)
        
        all_trades.extend(fold_trades)
        
        # Métricas do fold
        if fold_trades:
            fold_pnl = sum(t["pnl_brl"] for t in fold_trades)
            fold_wr = sum(1 for t in fold_trades if t["pnl_brl"] > 0) / len(fold_trades) * 100
            print(f"  Fold {fold+1}: {len(fold_trades)} trades | WR {fold_wr:.0f}% | P&L R${fold_pnl:,.0f}")
    
    if not all_trades:
        return {"error": "Nenhum trade executado em nenhum fold"}
    
    # === Métricas consolidadas ===
    trades_df = pd.DataFrame(all_trades)
    total_trades = len(trades_df)
    winners = trades_df[trades_df["pnl_brl"] > 0]
    losers = trades_df[trades_df["pnl_brl"] <= 0]
    
    win_rate = len(winners) / total_trades * 100
    total_pnl = trades_df["pnl_brl"].sum()
    avg_pnl = trades_df["pnl_brl"].mean()
    avg_win = winners["pnl_brl"].mean() if len(winners) > 0 else 0
    avg_loss = losers["pnl_brl"].mean() if len(losers) > 0 else 0
    
    # Max Drawdown
    peak = initial_capital
    max_dd = 0
    max_dd_abs = 0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd
            max_dd_abs = peak - eq
    
    # Sharpe Ratio
    returns = trades_df["pnl_brl"] / initial_capital
    sharpe = (returns.mean() / (returns.std() + 1e-10)) * np.sqrt(252) if len(returns) > 1 else 0
    
    # Profit Factor
    gross_profit = winners["pnl_brl"].sum() if len(winners) > 0 else 0
    gross_loss = abs(losers["pnl_brl"].sum()) if len(losers) > 0 else 1
    profit_factor = gross_profit / gross_loss
    
    # Expectancy
    expectancy = (win_rate/100 * avg_win) + ((1 - win_rate/100) * avg_loss)
    
    # Max consecutive losses
    max_consec_loss = 0
    current_consec = 0
    for _, trade in trades_df.iterrows():
        if trade["pnl_brl"] <= 0:
            current_consec += 1
            max_consec_loss = max(max_consec_loss, current_consec)
        else:
            current_consec = 0
    
    results = {
        "summary": {
            "symbol": symbol,
            "timeframe": timeframe,
            "period": f"{dataset['datetime'].iloc[0]} → {dataset['datetime'].iloc[-1]}",
            "total_candles": total,
            "n_folds": n_folds,
        },
        "trades": {
            "total": total_trades,
            "winners": len(winners),
            "losers": len(losers),
            "win_rate": round(win_rate, 2),
            "avg_pnl_per_trade": round(avg_pnl, 2),
        },
        "pnl": {
            "total_brl": round(total_pnl, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2),
            "expectancy_per_trade": round(expectancy, 2),
        },
        "risk": {
            "initial_capital": initial_capital,
            "final_capital": round(equity, 2),
            "return_pct": round((equity - initial_capital) / initial_capital * 100, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "max_drawdown_brl": round(max_dd_abs, 2),
            "sharpe_ratio": round(sharpe, 2),
            "max_consecutive_losses": max_consec_loss,
        },
        "confidence_analysis": {
            "avg_confidence": round(trades_df["confidence"].mean(), 4),
            "min_confidence_threshold": min_confidence,
        },
        "trades_sample": trades_df.tail(10).to_dict("records"),
    }
    
    return results


def print_backtest_results(results: dict):
    """Imprime resultados formatados."""
    if "error" in results:
        print(f"\n❌ ERRO: {results['error']}")
        return
    
    print(f"\n{'='*60}")
    print(f"📊 WALK-FORWARD BACKTEST RESULTS")
    print(f"{'='*60}")
    
    s = results["summary"]
    print(f"Symbol: {s['symbol']} | TF: {s['timeframe']}")
    print(f"Período: {s['period']}")
    print(f"Candles: {s['total_candles']} | Folds: {s['n_folds']}")
    
    t = results["trades"]
    print(f"\n📈 TRADING")
    print(f"  Total trades: {t['total']}")
    print(f"  Winners: {t['winners']} | Losers: {t['losers']}")
    print(f"  Win Rate: {t['win_rate']:.1f}%")
    print(f"  Média P&L/trade: R$ {t['avg_pnl_per_trade']:,.2f}")
    
    p = results["pnl"]
    print(f"\n💰 P&L")
    print(f"  Total: R$ {p['total_brl']:,.2f}")
    print(f"  Média win: R$ {p['avg_win']:,.2f}")
    print(f"  Média loss: R$ {p['avg_loss']:,.2f}")
    print(f"  Profit Factor: {p['profit_factor']:.2f}")
    print(f"  Expectancy/trade: R$ {p['expectancy_per_trade']:,.2f}")
    
    r = results["risk"]
    print(f"\n⚠️  RISCO")
    print(f"  Capital inicial: R$ {r['initial_capital']:,.2f}")
    print(f"  Capital final: R$ {r['final_capital']:,.2f}")
    print(f"  Return: {r['return_pct']:+.2f}%")
    print(f"  Max Drawdown: {r['max_drawdown_pct']:.1f}% (R$ {r['max_drawdown_brl']:,.2f})")
    print(f"  Sharpe Ratio: {r['sharpe_ratio']:.2f}")
    print(f"  Max consecutive losses: {r['max_consecutive_losses']}")
    
    c = results["confidence_analysis"]
    print(f"\n🎯 CONFIDENCE")
    print(f"  Média: {c['avg_confidence']:.2%}")
    print(f"  Threshold: {c['min_confidence_threshold']:.2%}")
    
    # Últimos trades
    if results.get("trades_sample"):
        print(f"\n📋 ÚLTIMOS 5 TRADES")
        for tr in results["trades_sample"][-5:]:
            emoji = "🟢" if tr["pnl_brl"] > 0 else "🔴"
            print(f"  {emoji} {tr['datetime'][:16]} | {tr['direction']:5s} | "
                  f"entry={tr['entry']:.0f} exit={tr['exit']:.0f} | "
                  f"P&L R${tr['pnl_brl']:+.2f} | conf={tr['confidence']:.2%}")
    
    print(f"\n{'='*60}")


if __name__ == "__main__":
    from prediction.data.mt5_fetcher import fetch_ohlcv
    
    for sym in ["WDO$", "WIN$"]:
        df = fetch_ohlcv(sym, "H1", 3000)
        if df is not None:
            results = backtest_walk_forward(df, symbol=sym, timeframe="H1", min_confidence=0.52)
            print_backtest_results(results)
