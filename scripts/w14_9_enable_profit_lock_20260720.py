#!/usr/bin/env python3
"""W14.9 (2026-07-20, seg, Bruno) — Adiciona chaves do Profit Lock no config.

Profit Lock Adaptativo (Wave 880.H): quando o PnL diário (realizado + flutuante)
atinge um target adaptativo, fecha tudo e bloqueia novas até o dia seguinte.
Defesa contra "o mercado comer o lucro do dia" (caso 20/07).

Este script SÓ adiciona as chaves ao vt_config.json com profit_lock_enabled=FALSE.
O Bruno decide quando ligar (mudar pra true), após validar.

Defaults (configuráveis depois):
  profit_lock_enabled      : false  (OFF até Bruno ligar)
  profit_lock_min_target   : 250.0  (R$ mínimo garantido)
  profit_lock_target_mult  : 1.0    (multiplica média histórica)
  profit_lock_lookback_days: 7      (janela pra média)

Uso (autotrador PAUSADO — data/autotrader.paused presente):
    python3 scripts/w14_9_enable_profit_lock_20260720.py

Para ATIVAR depois:
    edit vt_config.json → profit_lock_enabled: true  (ou use --enable)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.vt_config_loader import load_config, save_full_config  # noqa: E402

# Chaves a adicionar (só se ausentes — não sobrescreve se já existirem).
NEW_KEYS = {
    "profit_lock_enabled": False,
    "profit_lock_min_target": 250.0,
    "profit_lock_target_mult": 1.0,
    "profit_lock_lookback_days": 7,
}


def main():
    # Parser simples (mesmo padrão dos outros scripts w*).
    enable_mode = "--enable" in sys.argv

    pause_file = Path(__file__).resolve().parent.parent / "data" / "autotrader.paused"
    if not pause_file.exists():
        print("⚠️  AVISO: data/autotrader.paused AUSENTE.")
        print("   Regra Bruno: rodar scripts de config com autotrador pausado.")
        resp = input("   Continuar mesmo assim? [digite SIM]: ").strip()
        if resp != "SIM":
            print("Abortado.")
            sys.exit(1)

    cfg = load_config()

    print("=== ANTES ===")
    for k in NEW_KEYS:
        print(f"  {k}: {cfg.get(k, '<ausente>')}")

    # Adiciona só chaves ausentes (preserva config manual do Bruno).
    added = 0
    for k, default_v in NEW_KEYS.items():
        if k not in cfg:
            cfg[k] = default_v
            added += 1

    if enable_mode:
        cfg["profit_lock_enabled"] = True
        print("\n>>> --enable: profit_lock_enabled forçado para TRUE")

    print(f"\n=== DEPOIS ({added} chave(s) nova(s), profit_lock_enabled={cfg['profit_lock_enabled']}) ===")
    for k in NEW_KEYS:
        print(f"  {k}: {cfg[k]}")

    save_full_config(cfg, updated_by="w14_9_enable_profit_lock_20260720")
    print("\n✓ vt_config.json salvo (by=w14_9_enable_profit_lock_20260720)")
    print("  Para ATIVAR: edite profit_lock_enabled → true (ou rode com --enable)")
    print("  Reação em ≤ check_interval (30s). Sem reiniciar.")


if __name__ == "__main__":
    main()
