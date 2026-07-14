#!/usr/bin/env python3
"""W873 Pause Losing TFs (2026-07-07, noite) — pause dos 12 pares negativos.

Após run do AGI v4 (20:12-20:28) com specs W873 corretas, 12 de 16 pares
continuam negativos em 7d (soma -R$327). O AGI não achou alternativas que
passem no gate anti-overfit (walk-forward). Pausar via disabled_timeframes
(Lei 5: nunca aceitar negativo) mantém os 4 WIN lucrativos ativos.

Mantém símbolos (Lei 2: AGI nunca desabilita símbolo) — só pausa TFs.
Pause é reversível: AGI reativa quando achar estratégia lucrativa validada.

Uso: python3 scripts/w873_pause_losing_tfs_20260707.py [--dry-run]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.vt_config_loader import load_config, save_full_config

# 12 TFs negativos confirmados pelo AGI v4 (run 20:12-20:28, specs W873).
# Critério: PnL 7d <= 0 E (n_trades baixo OU PF < 1.2).
# BIT_M5 (-R$10, PF 0.94) incluído: marginal, não justifica risco.
LOSING_TFS = [
    "BIT_M5", "BIT_M15", "BIT_M30", "BIT_H1",
    "WSP_M5", "WSP_M15", "WSP_M30", "WSP_H1",
    "WDO_M5", "WDO_M15", "WDO_M30", "WDO_H1",
]


def main():
    dry = "--dry-run" in sys.argv
    cfg = load_config()

    atual = set(cfg.get("disabled_timeframes", []))
    novos = set(LOSING_TFS) - atual
    final = sorted(atual | set(LOSING_TFS))

    print("=== disabled_timeframes ATUAL ===")
    print(" ", sorted(atual) if atual else "(vazio)")
    print(f"\n=== {len(novos)} TF(s) a pausar ===")
    print(" ", sorted(novos))
    print(f"\n=== WIN (4) permanecem ATIVOS ===")
    print("  WIN_M5 (MOMENTUM_BREAKOUT)   PF=1.04  +R$363")
    print("  WIN_M15 (RSI_REVERSION)      PF=1.47  +R$4431")
    print("  WIN_M30 (PIVOT_POINTS)       PF=3.07  +R$1636")
    print("  WIN_H1 (RSI_REVERSION)       PF=1.72  +R$3252")
    print(f"\n=== após: {len(final)} TF(s) pausados, 4 WIN ativos ===")

    if dry:
        print("\n[DRY-RUN] nenhuma mudança aplicada.")
        return

    if not novos:
        print("\nNada a fazer — TFs já pausados.")
        return

    cfg["disabled_timeframes"] = final
    save_full_config(cfg, updated_by="w873_pause_losing_tfs")
    print(f"\n✓ {len(novos)} TF(s) pausados via disabled_timeframes (by=w873_pause_losing_tfs)")
    print("✓ Autotrader hot-reload (sem restart). Posições órfãs ainda gerenciadas.")


if __name__ == "__main__":
    main()
