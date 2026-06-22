"""
Backtest do HALT DOL: testa combinações de max_consecutive_losses (por TF)
e halt_duration_minutes para encontrar melhor configuração.

Uso:
  ./agent/venv/bin/python scripts/backtest_halt_dol.py
"""
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from itertools import product

PROJECT = Path("/home/bruno/Projects/Vibe-Trading")
DB_PATH = PROJECT / "vt_trades.db"
CONFIG_PATH = PROJECT / "vt_config.json"


def get_root(symbol):
    if 'WIN' in symbol: return 'WIN'
    if 'WDO' in symbol: return 'WDO'
    if 'BIT' in symbol: return 'BIT'
    if 'DOL' in symbol: return 'DOL'
    if 'IND' in symbol: return 'IND'
    if 'WSP' in symbol: return 'WSP'
    return symbol[:3]


def get_dol_trades(days=30):
    """Retorna trades DOL ordenados por tempo."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute("""
        SELECT symbol, timeframe, direction, net_pnl, entry_time, exit_time, multiplier
        FROM trades
        WHERE entry_time >= ? AND symbol LIKE 'DOL%'
        ORDER BY entry_time
    """, (cutoff,)).fetchall()
    return rows


def simulate_halt(trades, halt_threshold_by_tf, halt_duration_min, multiplier_correction=50.0):
    """
    Simula estratégia: se uma posição atingir 'halt_threshold' losses consecutivas
    no mesmo (symbol_root, tf), pausa por 'halt_duration_min' minutos.

    Returns:
      dict com pnl_real (com multiplier correto), max_drawdown, halted_count
    """
    # Multiplier correto do DOL (50x)
    halted_until = {}  # (root, tf) → datetime
    consecutive = {}   # (root, tf) → count
    pnl = 0.0
    pnl_history = []
    halted_count = 0
    trade_count = 0
    win_count = 0
    loss_count = 0

    for t in trades:
        root = get_root(t['symbol'])
        tf = t['timeframe']
        key = (root, tf)

        # Verifica HALT
        if key in halted_until:
            if datetime.fromisoformat(t['entry_time']) < halted_until[key]:
                # Bloqueado
                halted_count += 1
                continue
            else:
                del halted_until[key]
                consecutive[key] = 0

        # PnL real (multiplier correto)
        # Multiplier no DB é o que o bot gravou (errado). Corrigir pra DOL = 50x
        recorded_mult = t['multiplier'] or 1.0
        if root == 'DOL':
            # Recalcular PnL real com multiplier 50
            real_pnl = t['net_pnl'] * (multiplier_correction / recorded_mult)
        else:
            real_pnl = t['net_pnl']

        trade_count += 1
        if real_pnl > 0:
            win_count += 1
            consecutive[key] = 0
            halted_until.pop(key, None)
        else:
            loss_count += 1
            consecutive[key] = consecutive.get(key, 0) + 1

        # Checar threshold
        threshold = halt_threshold_by_tf.get(f"{root}_{tf}", 3)
        if consecutive[key] >= threshold:
            halt_until = datetime.fromisoformat(t['entry_time']) + timedelta(minutes=halt_duration_min)
            halted_until[key] = halt_until
            # Reset streak (pausa)
            consecutive[key] = 0

        pnl += real_pnl
        pnl_history.append(pnl)

    # Max drawdown
    peak = 0
    max_dd = 0
    for p in pnl_history:
        if p > peak:
            peak = p
        dd = peak - p
        if dd > max_dd:
            max_dd = dd

    return {
        'pnl_real': pnl,
        'max_drawdown': max_dd,
        'halted_count': halted_count,
        'trade_count': trade_count,
        'wins': win_count,
        'losses': loss_count,
        'win_rate': win_count / trade_count * 100 if trade_count > 0 else 0,
    }


def main():
    trades = get_dol_trades(days=30)
    print(f"Trades DOL analisados: {len(trades)} (últimos 30 dias)")
    if len(trades) == 0:
        print("Sem trades DOL!")
        return

    # PnL sem HALT (baseline)
    baseline = simulate_halt(trades, {}, 0)
    print(f"\n=== BASELINE (sem HALT) ===")
    print(f"  Trades: {baseline['trade_count']}")
    print(f"  WR: {baseline['win_rate']:.1f}%")
    print(f"  PnL real: R$ {baseline['pnl_real']:+.2f}")
    print(f"  Max DD: R$ {baseline['max_drawdown']:.2f}")

    # Grid search
    # halt_threshold por TF DOL
    halt_thresholds = {
        'DOL_M5':  [2, 3, 4],
        'DOL_M15': [2, 3, 4],
        'DOL_M30': [2, 3, 4, 5],
        'DOL_H1':  [3, 4, 5],
    }
    halt_durations = [30, 60, 90, 120]

    results = []
    for m5, m15, m30, h1 in product(*halt_thresholds.values()):
        for dur in halt_durations:
            cfg = {'DOL_M5': m5, 'DOL_M15': m15, 'DOL_M30': m30, 'DOL_H1': h1}
            res = simulate_halt(trades, cfg, dur)
            # Score: PnL real - penalidade por drawdown
            score = res['pnl_real'] - res['max_drawdown'] * 0.5
            results.append({
                'config': cfg,
                'halt_duration': dur,
                **res,
                'score': score,
            })

    # Top 5
    results.sort(key=lambda x: x['score'], reverse=True)

    print(f"\n=== TOP 5 CONFIGURAÇÕES (de {len(results)} testadas) ===")
    for i, r in enumerate(results[:5], 1):
        print(f"\n#{i} Score: R$ {r['score']:+.2f}")
        print(f"  Config: {r['config']}")
        print(f"  HALT duration: {r['halt_duration']}min")
        print(f"  PnL real: R$ {r['pnl_real']:+.2f} (vs baseline R$ {baseline['pnl_real']:+.2f})")
        print(f"  Max DD: R$ {r['max_drawdown']:.2f}")
        print(f"  Trades bloqueados: {r['halted_count']} / {r['trade_count']}")

    # Comparação com config atual
    current_config = {
        'DOL_M5': 3, 'DOL_M15': 3, 'DOL_M30': 4, 'DOL_H1': 5,
    }
    current = simulate_halt(trades, current_config, 60)
    print(f"\n=== CONFIG ATUAL ===")
    print(f"  {current_config}, halt_duration=60min")
    print(f"  PnL real: R$ {current['pnl_real']:+.2f}")
    print(f"  Max DD: R$ {current['max_drawdown']:.2f}")
    print(f"  Trades bloqueados: {current['halted_count']} / {current['trade_count']}")

    # Recomendação
    best = results[0]
    print(f"\n=== RECOMENDAÇÃO ===")
    if best['pnl_real'] > current['pnl_real'] and best['max_drawdown'] < current['max_drawdown']:
        print(f"  Aplicar: {best['config']} + halt_duration={best['halt_duration']}min")
        print(f"  Melhoria esperada: R$ {best['pnl_real'] - current['pnl_real']:+.2f}")
        print(f"  Redução DD: R$ {current['max_drawdown'] - best['max_drawdown']:.2f}")
    else:
        print(f"  Manter config atual (top #1 não melhora ambos PnL e DD)")


if __name__ == "__main__":
    main()