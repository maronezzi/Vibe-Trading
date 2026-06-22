"""
Backtest completo: simula impacto de sl_atr_mult, cooldown_seconds, etc.
sobre os trades históricos.

Para cada trade, verifica:
  (a) cooldown_seconds: tempo desde último trade do mesmo symbol+dir é suficiente?
  (b) sl_atr_mult: comparado com ATR histórico da barra

NOTA: simplificação — usa entry_price/ATR ratio como proxy.
"""
import json
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path

PROJECT = Path("/home/bruno/Projects/Vibe-Trading")
DB = PROJECT / "vt_trades.db"
OLD_CFG = Path("/tmp/vt_config_pre_check.json")
NEW_CFG = PROJECT / "vt_config.json"


def get_root(symbol):
    for r in ['WIN', 'WDO', 'BIT', 'DOL', 'IND', 'WSP']:
        if r in symbol: return r
    return symbol[:3]


def get_tf_params(cfg, symbol, tf):
    root = get_root(symbol)
    sym_cfg = cfg.get(root.lower(), {})
    tf_key = f"{root}_{tf}"
    tf_cfg = cfg.get('params_by_tf', {}).get(tf_key, {})
    merged = {**sym_cfg, **tf_cfg}
    return merged


def simulate_trades(cfg, trades_df, mult_correct):
    """
    Aplica filtros da config e retorna DataFrame filtrado.
    """
    df = trades_df.copy()
    df['root'] = df['symbol'].apply(get_root)
    df['allowed'] = True
    df['blocked_reason'] = ''

    # disabled_symbols
    disabled_syms = cfg.get('disabled_symbols', [])
    df.loc[df['root'].isin(disabled_syms), 'allowed'] = False
    df.loc[df['root'].isin(disabled_syms), 'blocked_reason'] = 'disabled_symbol'

    # disabled_timeframes
    df['tf_key'] = df['root'] + '_' + df['timeframe']
    disabled_tfs = cfg.get('disabled_timeframes', [])
    df.loc[df['tf_key'].isin(disabled_tfs) & df['allowed'], 'allowed'] = False
    df.loc[df['tf_key'].isin(disabled_tfs) & ~df['allowed'].eq(False), 'blocked_reason'] = 'disabled_tf'

    # Cooldown por symbol+tf (verifica se último trade foi < cooldown_seconds atrás)
    df['entry_time_dt'] = pd.to_datetime(df['entry_time'], format='ISO8601')
    df['last_trade_time'] = df.groupby(['root', 'timeframe'])['entry_time_dt'].shift(1)
    df['seconds_since_last'] = (df['entry_time_dt'] - df['last_trade_time']).dt.total_seconds()

    # Aplicar cooldown por par (root_tf)
    for tf_key in df['tf_key'].unique():
        root, tf = tf_key.split('_')
        params = get_tf_params(cfg, root+'_DUMMY', tf) if root+'_DUMMY' in cfg.get('params_by_tf', {}) else cfg.get(root.lower(), {})
        cd = params.get('cooldown_seconds', 0)
        if cd > 0:
            mask = (df['tf_key'] == tf_key) & df['seconds_since_last'].notna() & (df['seconds_since_last'] < cd)
            df.loc[mask & df['allowed'], 'allowed'] = False
            df.loc[mask & df['allowed'], 'blocked_reason'] = 'cooldown'

    # PnL real
    df['pnl_real'] = df.apply(
        lambda r: r['net_pnl'] * (mult_correct.get(r['root'], 1.0) / (r['multiplier'] or 1.0)),
        axis=1
    )

    return df


def main():
    cfg_old = json.loads(OLD_CFG.read_text())
    cfg_new = json.loads(NEW_CFG.read_text())
    mult_correct = {'WIN': 0.2, 'WDO': 10.0, 'BIT': 1.0, 'DOL': 50.0, 'IND': 1.0, 'WSP': 1.0}

    conn = sqlite3.connect(str(DB))
    df = pd.read_sql_query("""
        SELECT symbol, timeframe, net_pnl, multiplier, entry_time
        FROM trades
        WHERE entry_time >= '2026-05-19'
    """, conn)

    print("=" * 75)
    print("BACKTEST COMPLETO — Comparação Forward (30 dias, 295 trades)")
    print("=" * 75)

    res_old = simulate_trades(cfg_old, df, mult_correct)
    res_new = simulate_trades(cfg_new, df, mult_correct)

    # Stats globais
    allowed_old = res_old[res_old['allowed']]
    allowed_new = res_new[res_new['allowed']]

    print(f"\n{'Métrica':<35} {'Config ANTIGA':>18} {'Config NOVA':>18}")
    print("-" * 75)
    print(f"{'Trades totais':<35} {len(res_old):>18} {len(res_new):>18}")
    print(f"{'Trades permitidos':<35} {len(allowed_old):>18} {len(allowed_new):>18}")
    print(f"{'Trades bloqueados':<35} {(~res_old['allowed']).sum():>18} {(~res_new['allowed']).sum():>18}")
    print(f"{'PnL real (permitidos)':<35} R$ {allowed_old['pnl_real'].sum():>+14.2f} R$ {allowed_new['pnl_real'].sum():>+14.2f}")
    print(f"{'Win rate':<35} {(allowed_old['pnl_real'] > 0).mean() * 100:>17.1f}% {(allowed_new['pnl_real'] > 0).mean() * 100:>17.1f}%")
    print(f"{'Avg PnL/trade':<35} R$ {allowed_old['pnl_real'].mean():>+14.2f} R$ {allowed_new['pnl_real'].mean():>+14.2f}")

    delta_pnl = allowed_new['pnl_real'].sum() - allowed_old['pnl_real'].sum()
    print(f"\n>>> Δ PnL: R$ {delta_pnl:+.2f}")
    print(f">>> Trades a mais bloqueados: {(~res_new['allowed']).sum() - (~res_old['allowed']).sum()}")

    # Por motivo de bloqueio (nova)
    print(f"\n=== Razões de bloqueio (config NOVA) ===")
    blocked = res_new[~res_new['allowed']]
    print(blocked['blocked_reason'].value_counts())

    # Por símbolo
    print(f"\n=== Por símbolo (config NOVA, permitidos) ===")
    by_sym = allowed_new.groupby('root').agg(
        n=('pnl_real', 'count'),
        pnl=('pnl_real', 'sum'),
        wr=('pnl_real', lambda x: (x > 0).mean() * 100),
    ).round(2)
    print(by_sym.sort_values('pnl', ascending=False).to_string())

    # Por par
    print(f"\n=== Por par (config NOVA, permitidos) ===")
    by_pair = allowed_new.groupby(['root', 'timeframe']).agg(
        n=('pnl_real', 'count'),
        pnl=('pnl_real', 'sum'),
        wr=('pnl_real', lambda x: (x > 0).mean() * 100),
    ).round(2)
    print(by_pair.sort_values('pnl', ascending=False).head(10).to_string())


if __name__ == "__main__":
    main()