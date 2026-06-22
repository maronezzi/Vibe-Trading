"""
Migração do state: chaves antigas (símbolo resolvido) → chaves novas (root_tf).
Calcula streak real por (root, tf) baseado no SQLite e atualiza state.consecutive_losses.
"""
import json
import sqlite3
from datetime import datetime, timedelta
from collections import defaultdict

STATE_PATH = "/tmp/vt_autotrader_state.json"
DB_PATH = "/home/bruno/Projects/Vibe-Trading/vt_trades.db"


def get_root(symbol):
    if 'WIN' in symbol: return 'WIN'
    if 'WDO' in symbol: return 'WDO'
    if 'BIT' in symbol: return 'BIT'
    if 'DOL' in symbol: return 'DOL'
    if 'IND' in symbol: return 'IND'
    if 'WSP' in symbol: return 'WSP'
    return symbol[:3]


def main():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        SELECT symbol, timeframe, net_pnl, entry_time
        FROM trades
        WHERE date(entry_time)='2026-06-18'
        ORDER BY entry_time
    """)

    pairs_trades = defaultdict(list)
    for symbol, tf, pnl, et in c.fetchall():
        root = get_root(symbol)
        pairs_trades[(root, tf)].append((et, pnl))

    # Calcula streak atual por par (root, tf)
    new_streaks = {}
    for (root, tf), trades in pairs_trades.items():
        trades_sorted = sorted(trades, key=lambda x: x[0], reverse=True)
        streak = 0
        for _, pnl in trades_sorted:
            if pnl < 0:
                streak += 1
            else:
                break
        key = f"{root}_{tf}"
        new_streaks[key] = streak

    # Carrega state
    with open(STATE_PATH) as f:
        state = json.load(f)

    # Backup do state atual
    backup_path = STATE_PATH + f".bak_migration_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    with open(backup_path, 'w') as f:
        json.dump(state, f, indent=2)
    print(f"Backup salvo em {backup_path}")

    # Remove chaves antigas (com símbolo resolvido) e adiciona novas
    old_losses = state.get("consecutive_losses", {})
    new_losses = {}

    # Preserva chaves já em formato root_tf
    for k, v in old_losses.items():
        if '_' in k and k.split('_')[-1] in ('M5', 'M15', 'M30', 'H1'):
            new_losses[k] = v
        # Chave com símbolo resolvido (ex: DOLN26) — descarta (será recalculado)

    # Aplica streaks calculados do SQLite (fonte da verdade)
    for k, streak in new_streaks.items():
        if streak > 0:
            new_losses[k] = streak

    state["consecutive_losses"] = new_losses

    # Calcular HALT para pares que excederam threshold
    with open("/home/bruno/Projects/Vibe-Trading/vt_config.json") as f:
        cfg = json.load(f)

    by_tf = cfg.get("max_consecutive_losses_by_tf", {})
    halt_min = cfg.get("halt_duration_minutes", 60)

    new_halts = state.get("halt_until", {})
    # Limpa halts expirados (mais de halt_min atrás)
    now = datetime.now()
    for k in list(new_halts.keys()):
        try:
            ht = datetime.fromisoformat(new_halts[k])
            if ht < now:
                del new_halts[k]
        except Exception:
            del new_halts[k]

    # Aplica halt para streaks >= threshold
    halt_count = 0
    for k, streak in new_losses.items():
        threshold = by_tf.get(k, cfg.get("max_consecutive_losses", 3))
        if streak >= threshold and k not in new_halts:
            new_halts[k] = (now + timedelta(minutes=halt_min)).isoformat()
            halt_count += 1
            print(f"🛑 HALT aplicado: {k} ({streak} losses ≥ {threshold})")

    state["halt_until"] = new_halts

    # Salva
    with open(STATE_PATH, 'w') as f:
        json.dump(state, f, indent=2)

    print()
    print("=== RESULTADO ===")
    print(f"Streaks migrados: {new_losses}")
    print(f"HALT ativos: {list(new_halts.keys())}")
    print(f"Novos HALT aplicados: {halt_count}")
    print()
    print("✅ State migrado com sucesso.")


if __name__ == "__main__":
    main()