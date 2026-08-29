#!/usr/bin/env python3
"""
vt_resolve_symbols.py — Sincroniza vt_config.json com os contratos resolvidos em runtime.

Uso:
    python3 vt_resolve_symbols.py          # dry-run (mostra o que mudaria)
    python3 vt_resolve_symbols.py --apply  # aplica mudanças no vt_config.json

Lógica:
    Chama core.vt_calendar.resolve_symbol() para cada símbolo do config
    e atualiza resolved_symbols no vt_config.json. Persistência via
    save_full_config (whitelist canônica — lock + atomic write + lineage).

Notas:
    - vt_calendar vive em core/ (sys.path ajustado abaixo — espelha o padrão
      de monitoring/vt_pre_flight.py e tests/conftest.py).
    - write_paths: AGENTS.md proíbe bypass do save_full_config. Este script
      está em ALLOWED_WRITERS (core/vt_config_loader.py), então pode chamar
      save_full_config livremente.
"""
import argparse
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent

# Espelha o bootstrap de monitoring/vt_pre_flight.py — sem isso, o import
# `from core.vt_calendar import resolve_symbol` falha (Bug original:
# script tentava `from vt_calendar import resolve_symbol` que não existe
# no root — vt_calendar vive em core/).
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "core"))  # noqa: E402 — fix ModuleNotFoundError de core.vt_calendar
sys.path.insert(0, str(PROJECT_DIR / "mt5"))   # noqa: E402 — fix ModuleNotFoundError de mt5_orchestrator (deps transitivas)


def main():
    parser = argparse.ArgumentParser(description="Sync resolved_symbols in vt_config.json")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry-run)")
    args = parser.parse_args()

    # Import lazy — depois do sys.path fix
    from core.vt_calendar import resolve_symbol
    from core.vt_config_loader import load_config, save_full_config

    # Load config (loader canônico — valida schema mínimo + hot-reload)
    config = load_config()

    symbols = config.get("symbols", [])
    resolved = dict(config.get("resolved_symbols", {}))
    changes = []

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

    # Apply changes via save_full_config (whitelisted writer — passa o
    # _assert_authorized_writer do loader; usa lock + atomic write).
    for sym, old, new in changes:
        resolved[sym] = new

    config["resolved_symbols"] = resolved
    # save_full_config já incrementa _version, atualiza _updated_at e
    # seta _updated_by — não duplicamos aqui.
    save_full_config(config, updated_by="pre_pregao_resolve_symbols")

    print(f"\n✅ {len(changes)} símbolo(s) atualizado(s) no vt_config.json")


if __name__ == "__main__":
    main()
