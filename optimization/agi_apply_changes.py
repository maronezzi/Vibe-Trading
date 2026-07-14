#!/usr/bin/env python3
"""AGI Apply Changes — Wave Per-TF (Bruno)

Script cirúrgico que aplica mudanças de parâmetros/estratégia via save_params
(API canônica do loader, com lock file).

Uso:
    python3 optimization/agi_apply_changes.py --changes '[{...}]'

Whitelist: ver core/vt_config_loader.py:ALLOWED_WRITERS.
"""

import argparse
import json
import sys
from pathlib import Path

# Self-bootstrap sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.vt_config_loader import load_config, save_full_config

UPDATED_BY = "hermes_agi_apply_changes_wave_per_tf"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--changes", default="[]", help="JSON array de changes")
    ap.add_argument("--strategy-changes", default=None, help="JSON array [{tf, strategy, reason}] para strategy_by_tf")
    ap.add_argument("--max-daily-loss", type=int, default=None, help="Override max_daily_loss")
    ap.add_argument("--disable-symbols", default=None, help="JSON array")
    ap.add_argument("--disable-tfs", default=None, help="JSON array")
    ap.add_argument("--reenable-symbols", default=None, help="JSON array")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    changes = json.loads(args.changes)
    print(f"[agi_apply_changes] {len(changes)} mudanças a aplicar (dry_run={args.dry_run})")

    cfg = load_config(force=True)
    if args.dry_run:
        for ch in changes:
            print(f"  DRY: {ch.get('symbol')} → {ch.get('params')} ({ch.get('reason', '')[:60]})")
        if args.strategy_changes:
            sc = json.loads(args.strategy_changes)
            for ch in sc:
                print(f"  DRY-STRAT: {ch.get('tf')} → {ch.get('strategy')} ({ch.get('reason', '')[:60]})")
        return

    # Estratégias (Wave W874): atualiza strategy_by_tf
    if args.strategy_changes:
        sc = json.loads(args.strategy_changes)
        strat_map = cfg.setdefault("strategy_by_tf", {})
        for ch in sc:
            tf = ch.get("tf")
            strat = ch.get("strategy")
            reason = ch.get("reason", "")[:80]
            if not tf or not strat:
                print(f"  SKIP-STRAT (sem tf/strategy): {ch}")
                continue
            old = strat_map.get(tf, "?")
            strat_map[tf] = strat
            print(f"  ✅ STRAT {tf}: {old} → {strat} ({reason})")

    applied = 0
    # Coleta tudo para um único save_full_config (atômico, correto topologia)
    cfg_now = load_config(force=True)
    by_tf = cfg_now.setdefault("params_by_tf", {})

    # Estratégias já foram aplicadas em cfg (acima); garantir consistência
    if args.strategy_changes:
        cfg_now["strategy_by_tf"] = cfg.get("strategy_by_tf", cfg_now.get("strategy_by_tf", {}))

    for ch in changes:
        sym = ch.get("symbol")
        params = ch.get("params", {})
        reason = ch.get("reason", "")[:80]
        if not sym or not params:
            print(f"  SKIP (sem symbol/params): {ch}")
            continue
        # Detecta se é par SYMBOL_TF (tem '_' e o sufixo é M5/M15/M30/H1)
        parts = sym.split("_")
        if len(parts) == 2 and parts[1].upper() in ("M5", "M15", "M30", "H1"):
            # Par SYM_TF → escreve em cfg['params_by_tf'][SYM_TF.upper()]
            tf_key = f"{parts[0].upper()}_{parts[1].upper()}"
            existing = by_tf.get(tf_key, {})
            existing.update(params)
            by_tf[tf_key] = existing
            applied += 1
            print(f"  ✅ {tf_key} (params_by_tf) ← {params} ({reason})")
        else:
            # Símbolo root → escreve em cfg[symbol.lower()]
            root_key = sym.lower()
            existing = cfg_now.get(root_key, {})
            existing.update(params)
            cfg_now[root_key] = existing
            applied += 1
            print(f"  ✅ {root_key} (root) ← {params} ({reason})")

    # Salva tudo de uma vez (atômico)
    cfg_now["_version"] = cfg_now.get("_version", 0) + 1
    cfg_now["_updated_at"] = __import__("datetime").datetime.now().isoformat()
    cfg_now["_updated_by"] = UPDATED_BY
    save_full_config(cfg_now, updated_by=UPDATED_BY)

    # Optional: max_daily_loss, disable_*, reenable_*
    extras = {}
    if args.max_daily_loss is not None:
        extras["max_daily_loss"] = args.max_daily_loss
    if args.disable_symbols:
        extras["disabled_symbols"] = json.loads(args.disable_symbols)
    if args.disable_tfs:
        extras["disabled_timeframes"] = json.loads(args.disable_tfs)
    if args.reenable_symbols:
        # tira de disabled_symbols
        cfg_now = load_config(force=True)
        cur = set(cfg_now.get("disabled_symbols", []))
        for s in json.loads(args.reenable_symbols):
            cur.discard(s)
        extras["disabled_symbols"] = sorted(cur)

    if extras:
        cfg_now = load_config(force=True)
        cfg_now.update(extras)
        cfg_now["_version"] = cfg_now.get("_version", 0) + 1
        cfg_now["_updated_at"] = __import__("datetime").datetime.now().isoformat()
        cfg_now["_updated_by"] = UPDATED_BY
        save_full_config(cfg_now, updated_by=UPDATED_BY)
        print(f"  ✅ extras aplicados: {list(extras.keys())}")

    print(f"[agi_apply_changes] done — {applied}/{len(changes)} aplicadas")


if __name__ == "__main__":
    main()
