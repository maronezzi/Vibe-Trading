#!/usr/bin/env python3
"""
vt_resolve_symbols.py — Sincroniza vt_config.json com os contratos resolvidos em runtime.

Uso:
    python3 vt_resolve_symbols.py          # dry-run (mostra o que mudaria)
    python3 vt_resolve_symbols.py --apply  # aplica mudanças no vt_config.json

Lógica:
    Chama vt_calendar.resolve_symbol() para cada símbolo do config
    e atualiza resolved_symbols no vt_config.json.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
CONFIG_FILE = PROJECT_DIR / "vt_config.json"


def main():
    parser = argparse.ArgumentParser(description="Sync resolved_symbols in vt_config.json")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry-run)")
    args = parser.parse_args()

    # Load config
    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)

    symbols = config.get("symbols", [])
    resolved = config.get("resolved_symbols", {})
    changes = []

    # Import resolve_symbol
    sys.path.insert(0, str(PROJECT_DIR))
    from vt_calendar import resolve_symbol

    for sym in symbols:
        new_contract = resolve_symbol(sym)
        old_contract = resolved.get(sym, "")
        if new_contract != old_contract:
            changes.append((sym, old_contract, new_contract))
            print(f"  📝 {sym}: {old_contract or '(nenhum)'} → {new_contract}")
        else:
            print(f"  ✅ {sym}: {old_contract} (sem mudança)")

    if not changes:
        print("\n✅ Config já sincronizado — nada a fazer.")
        return

    if not args.apply:
        print(f"\n🔍 Dry-run: {len(changes)} mudança(s) encontrada(s). Use --apply para aplicar.")
        return

    # Apply changes
    for sym, old, new in changes:
        resolved[sym] = new

    config["resolved_symbols"] = resolved
    config["_version"] = config.get("_version", 0) + 1
    config["_updated_at"] = datetime.now().isoformat()
    config["_updated_by"] = "vt_resolve_symbols.py"

    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    print(f"\n✅ {len(changes)} símbolo(s) atualizado(s) no vt_config.json")


if __name__ == "__main__":
    main()
