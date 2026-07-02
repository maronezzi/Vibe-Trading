#!/usr/bin/env python3
"""
scripts/reenable_scope_and_live_vol.py
======================================
Script de manutenção (Fase 5 — handoff Bruno 02/07/2026).

Reativa 3 TFs bloqueados (violação Lei 2) + ajusta volume WIN para live gradual:
  1. Remove BIT_M5 e BIT_M30 de disabled_timeframes
  2. Adiciona H1 ao timeframes_by_symbol[WIN]
  3. Baixa volume_by_symbol[WIN] de 2 para 1 (live gradual: 1 contrato/símbolo)

DEVE rodar com autotrador PAUSADO (Write/Read Separation). Whitelisted em
ALLOWED_WRITERS. Usa save_full_config (lock + backup + atomic write).

Uso (apenas com autotrader parado):
    python3 scripts/reenable_scope_and_live_vol.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT))
sys.path.insert(0, str(_PROJECT / "core"))

from vt_config_loader import load_config, save_full_config


def main() -> int:
    cfg = load_config(force=True)
    print(f"Config v{cfg.get('_version')} lido.")

    changes = []

    # 1. Reativa BIT_M5 e BIT_M30 (Lei 2)
    disabled_tf = list(cfg.get("disabled_timeframes", []))
    for tf in ("BIT_M5", "BIT_M30"):
        if tf in disabled_tf:
            disabled_tf.remove(tf)
            changes.append(f"removido {tf} de disabled_timeframes")
    cfg["disabled_timeframes"] = disabled_tf

    # 2. Adiciona H1 ao WIN (Lei 2)
    tf_by_sym = dict(cfg.get("timeframes_by_symbol", {}))
    win_tfs = list(tf_by_sym.get("WIN", []))
    if "H1" not in win_tfs:
        win_tfs = sorted(set(win_tfs) | {"H1"}, key=["M5", "M15", "M30", "H1"].index)
        tf_by_sym["WIN"] = win_tfs
        cfg["timeframes_by_symbol"] = tf_by_sym
        changes.append(f"adicionado H1 a timeframes_by_symbol[WIN] → {win_tfs}")

    # 3. WIN volume 2 → 1 (live gradual)
    vol_by_sym = dict(cfg.get("volume_by_symbol", {}))
    if vol_by_sym.get("WIN") == 2:
        vol_by_sym["WIN"] = 1
        cfg["volume_by_symbol"] = vol_by_sym
        changes.append("WIN volume 2 → 1 (live gradual)")

    if not changes:
        print("Nenhuma mudança necessária — config já está no estado alvo.")
        return 0

    print(f"\nAplicando {len(changes)} mudança(s):")
    for c in changes:
        print(f"  • {c}")

    save_full_config(cfg, updated_by="reenable_scope_and_live_vol")
    print(f"\nConfig salvo (nova versão: v{cfg['_version']}).")
    print("DEPOIS: reinicie o autotrader (state rebuild do MT5).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
