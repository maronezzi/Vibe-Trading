"""
Validação forward dos parâmetros aplicados pelo AGI 17h em 18/06.
Re-roda os trades de hoje (18/06) com:
  (a) config ANTIGA (backup 17/06)
  (b) config NOVA (atual pós-AGI)
Compara PnL real (multiplier DOL=50).
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from copy import deepcopy

PROJECT = Path("/home/bruno/Projects/Vibe-Trading")
DB = PROJECT / "vt_trades.db"
OLD = Path("/tmp/vt_config_pre_check.json")  # 17/06 backup
NEW = PROJECT / "vt_config.json"  # atual pós-AGI


def get_root(symbol):
    for r in ['WIN', 'WDO', 'BIT', 'DOL', 'IND', 'WSP']:
        if r in symbol:
            return r
    return symbol[:3]


def load_config(path):
    with open(path) as f:
        return json.load(f)


def simulate_trade_pnl(trade, cfg, mult_correct):
    """Recalcula PnL real com multiplier correto."""
    root = get_root(trade['symbol'])
    recorded_mult = trade['multiplier'] or 1.0
    real_mult = mult_correct.get(root, 1.0)
    # Correção
    return trade['net_pnl'] * (real_mult / recorded_mult)


def apply_strategy_filters(trade, cfg):
    """
    Aplica filtros da config e retorna True se a trade teria sido PERMITIDA.
    Simplificação: checa disabled_symbols e disabled_timeframes.
    """
    root = get_root(trade['symbol'])
    tf = trade['timeframe']

    if root in cfg.get('disabled_symbols', []):
        return False

    tf_key = f"{root}_{tf}"
    if tf_key in cfg.get('disabled_timeframes', []):
        return False

    return True


def evaluate_config(cfg, trades, mult_correct):
    """Avalia performance de uma config contra um set de trades."""
    pnl_total = 0
    n_allowed = 0
    n_blocked = 0
    wins = 0
    losses = 0

    for t in trades:
        if not apply_strategy_filters(t, cfg):
            n_blocked += 1
            continue
        real_pnl = simulate_trade_pnl(t, cfg, mult_correct)
        pnl_total += real_pnl
        n_allowed += 1
        if real_pnl > 0:
            wins += 1
        else:
            losses += 1

    wr = wins / n_allowed * 100 if n_allowed > 0 else 0
    return {
        'pnl_real': pnl_total,
        'n_allowed': n_allowed,
        'n_blocked': n_blocked,
        'wins': wins,
        'losses': losses,
        'win_rate': wr,
    }


def main():
    print("=" * 70)
    print("VALIDAÇÃO FORWARD — Parâmetros AGI 18/06")
    print("=" * 70)
    print()

    # Carregar configs
    cfg_old = load_config(OLD)
    cfg_new = load_config(NEW)

    print(f"Config ANTIGA: {OLD.name} (17/06 — pré AGI)")
    print(f"Config NOVA:  {NEW.name} (atual — pós AGI)")
    print()

    # Multiplier correto
    mult_correct = {'WIN': 0.2, 'WDO': 10.0, 'BIT': 1.0, 'DOL': 50.0, 'IND': 1.0, 'WSP': 1.0}

    # Períodos de avaliação
    periods = [
        ("Últimos 30 dias", 30),
        ("Últimos 14 dias", 14),
        ("Últimos 7 dias", 7),
        ("Hoje (18/06)", 0),  # só 18/06
    ]

    conn = sqlite3.connect(str(DB))

    for label, days in periods:
        if days == 0:
            cutoff = "2026-06-18"
        else:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        trades = conn.execute("""
            SELECT symbol, timeframe, net_pnl, multiplier, entry_time
            FROM trades
            WHERE entry_time >= ?
            ORDER BY entry_time
        """, (cutoff,)).fetchall()

        # Converter para dicts
        trades = [
            {'symbol': t[0], 'timeframe': t[1], 'net_pnl': t[2],
             'multiplier': t[3], 'entry_time': t[4]}
            for t in trades
        ]

        old_result = evaluate_config(cfg_old, trades, mult_correct)
        new_result = evaluate_config(cfg_new, trades, mult_correct)

        delta_pnl = new_result['pnl_real'] - old_result['pnl_real']
        delta_blocked = new_result['n_blocked'] - old_result['n_blocked']

        print(f"--- {label} ({len(trades)} trades, cutoff {cutoff}) ---")
        print(f"  ANTIGA: PnL R$ {old_result['pnl_real']:+.2f} | {old_result['n_allowed']}t ({old_result['n_blocked']} blocked) | WR {old_result['win_rate']:.1f}%")
        print(f"  NOVA:   PnL R$ {new_result['pnl_real']:+.2f} | {new_result['n_allowed']}t ({new_result['n_blocked']} blocked) | WR {new_result['win_rate']:.1f}%")
        print(f"  Δ PnL: R$ {delta_pnl:+.2f} | Δ Blocked: {delta_blocked:+d}")
        print()

    # Detalhe por símbolo (últimos 7d)
    print("=" * 70)
    print("DETALHE POR SÍMBOLO (últimos 7 dias)")
    print("=" * 70)
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    trades = conn.execute("""
        SELECT symbol, timeframe, net_pnl, multiplier
        FROM trades
        WHERE entry_time >= ?
        ORDER BY entry_time
    """, (cutoff,)).fetchall()
    trades = [
        {'symbol': t[0], 'timeframe': t[1], 'net_pnl': t[2], 'multiplier': t[3]}
        for t in trades
    ]

    by_sym = {}
    for t in trades:
        root = get_root(t['symbol'])
        if root not in by_sym:
            by_sym[root] = []
        by_sym[root].append(t)

    print(f"\n{'Símbolo':<8} {'N':>4} {'PnL_real (NEW)':>16} {'Blocked':>8}")
    print("-" * 40)
    for sym in ['WIN', 'WDO', 'BIT', 'DOL', 'IND', 'WSP']:
        if sym not in by_sym:
            continue
        sym_trades = by_sym[sym]
        res_new = evaluate_config(cfg_new, sym_trades, mult_correct)
        delta_blocked = res_new['n_blocked']
        print(f"{sym:<8} {res_new['n_allowed']:>4} R$ {res_new['pnl_real']:>+14.2f} {delta_blocked:>8}")


if __name__ == "__main__":
    main()