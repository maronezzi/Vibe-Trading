#!/usr/bin/env python3
"""
Vibe-Trading Prediction MVP — Runner principal.

Busca dados do MT5, treina o Direction Classifier, roda walk-forward backtest.
Módulo 100% isolado — NÃO modifica scripts existentes.

Uso:
    cd ~/Projects/Vibe-Trading
    ./prediction/.venv/bin/python prediction/run.py                    # WDO$ M5
    ./prediction/.venv/bin/python prediction/run.py --symbol WIN$     # WIN$ M5
    ./prediction/.venv/bin/python prediction/run.py --all             # WDO + WIN
    ./prediction/.venv/bin/python prediction/run.py --all --bars 5000 # Mais dados
"""

import argparse
import json
import sys
import os
from datetime import datetime

# Garantir que prediction/ está no path
PRED_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(PRED_DIR))

from prediction.data.mt5_fetcher import fetch_ohlcv
from prediction.models.direction_classifier import DirectionClassifier
from prediction.backtest import backtest_walk_forward, print_backtest_results


def run_pipeline(symbol: str, timeframe: str, n_bars: int, min_confidence: float):
    """Pipeline completo: fetch → train → walk-forward backtest."""
    
    print(f"\n{'#'*60}")
    print(f"# Vibe-Trading Prediction MVP")
    print(f"# {symbol} | {timeframe} | {n_bars} barras")
    print(f"# {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#'*60}")
    
    # 1. Buscar dados
    print(f"\n[1/3] Buscando dados do MT5...")
    df = fetch_ohlcv(symbol, timeframe, n_bars)
    if df is None or len(df) < 500:
        print(f"❌ Dados insuficientes para {symbol} (mínimo 500 barras)")
        return None
    
    # 2. Treinar modelo
    print(f"\n[2/3] Treinando Direction Classifier...")
    clf = DirectionClassifier(
        symbol=symbol,
        timeframe=timeframe,
        min_confidence=min_confidence,
    )
    metrics = clf.train(df, verbose=True)
    
    # Salvar modelo
    tag = datetime.now().strftime("%Y%m%d_%H%M")
    model_path = clf.save(tag)
    
    # 3. Walk-Forward Backtest
    print(f"\n[3/3] Rodando walk-forward backtest...")
    results = backtest_walk_forward(
        df, symbol=symbol, timeframe=timeframe,
        min_confidence=min_confidence, horizon=1,
    )
    print_backtest_results(results)
    
    # Salvar resultados
    results_dir = os.path.join(PRED_DIR, "results")
    os.makedirs(results_dir, exist_ok=True)
    results_file = os.path.join(results_dir, f"{symbol}_{timeframe}_{tag}.json")
    with open(results_file, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n📁 Resultados salvos: {results_file}")
    
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "metrics": metrics,
        "backtest": results,
        "model_path": model_path,
    }


def main():
    parser = argparse.ArgumentParser(description="Vibe-Trading Prediction MVP")
    parser.add_argument("--symbol", default="WDO$", help="Símbolo (WDO$ ou WIN$)")
    parser.add_argument("--timeframe", default="M5", help="Timeframe (M5, M15, H1)")
    parser.add_argument("--bars", type=int, default=3000, help="Número de barras")
    parser.add_argument("--confidence", type=float, default=0.52, help="Confiança mínima")
    parser.add_argument("--all", action="store_true", help="Rodar em WDO e WIN")
    args = parser.parse_args()
    
    symbols = ["WDO$", "WIN$"] if args.all else [args.symbol]
    
    all_results = {}
    for sym in symbols:
        result = run_pipeline(sym, args.timeframe, args.bars, args.confidence)
        if result:
            all_results[sym] = result
    
    # Resumo final
    print(f"\n{'='*60}")
    print(f"📋 RESUMO FINAL — Walk-Forward Backtest")
    print(f"{'='*60}")
    for key, res in all_results.items():
        bt = res["backtest"]
        if "error" in bt:
            print(f"  {res['symbol']} {res['timeframe']}: ❌ {bt['error']}")
        else:
            t = bt["trades"]
            p = bt["pnl"]
            r = bt["risk"]
            emoji = "🟢" if r["return_pct"] > 0 else "🔴"
            print(f"  {emoji} {res['symbol']} {res['timeframe']}: "
                  f"{t['total']} trades | WR {t['win_rate']:.0f}% | "
                  f"Return {r['return_pct']:+.1f}% | "
                  f"Sharpe {r['sharpe_ratio']:.2f} | "
                  f"MaxDD {r['max_drawdown_pct']:.1f}%")
    print(f"{'='*60}")
    print(f"\n💡 Próximos passos:")
    print(f"   1. Adicionar lag features (preço N candles atrás)")
    print(f"   2. Adicionar correlação entre WDO e WIN")
    print(f"   3. Subir pra M5 com mais dados")
    print(f"   4. Integrar sentiment score (FinBERT)")
    print(f"   5. Ensemble com N-BEATS")


if __name__ == "__main__":
    main()
