#!/usr/bin/env python3
"""Wave 878 (2026-07-17, sex, 11h00-11h15) — Copilot sessão 11h tightening.

Briefing:
- WIN M15/M30 HTF_BIAS_LTF_ENTRY: hoje 13 trades WIN_M15 (2W/11L, PnL -R$8) +
  7 trades WIN_M30 (1W/6L, PnL -R$11). Cooldown 120-180s permite 5-6 entradas
  consecutivas em mercado lateral.
- BIT M15 EMA_PULLBACK: 6 trades, WR 33%, PnL -R$215. ATR=1551 + sl_atr_mult=3.0
  → SL sugerido ~4653pts reais. Validator local inflou para 155142pts em 2
  oportunidades (BIT M30/M15) causando emergency_close sem posição aberta.

Ações (3 params, 3 pares — conservative, NÃO troca estratégia, NÃO mexe em
sl_atr_mult, NÃO remove de disabled_timeframes):

| Par      | cooldown_seconds | max_consecutive_losses | halt_duration_minutes |
|----------|------------------|------------------------|-----------------------|
| WIN_M15  | 180 → 600        | 8 → 3                  | 30 → 45               |
| WIN_M30  | 120 → 600        | 8 → 3                  | 15 → 30               |
| BIT_M15  | 300 → 600        | 8 → 3                  | (sem mudança)         |

Regra Bruno: "NÃO mude estratégias — apenas ajuste parâmetros". Aplicar via
save_full_config com updated_by="copilot_11h_w878_overtrading_fix".

Rodar com autotrader PAUSADO (data/autotrader.paused presente).
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.vt_config_loader import save_full_config  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "vt_config.json"
UPDATED_BY = "copilot_11h_w878_overtrading_fix"

UPDATES = {
    "WIN_M15": {
        "cooldown_seconds": 600,
        "max_consecutive_losses": 3,
        "halt_duration_minutes": 45,
    },
    "WIN_M30": {
        "cooldown_seconds": 600,
        "max_consecutive_losses": 3,
        "halt_duration_minutes": 30,
    },
    "BIT_M15": {
        "cooldown_seconds": 600,
        "max_consecutive_losses": 3,
    },
}


def main() -> int:
    cfg = json.loads(CONFIG_PATH.read_text())

    print("=== ANTES ===")
    for tf_key, changes in UPDATES.items():
        p = cfg["params_by_tf"][tf_key]
        relevant = {k: p.get(k) for k in changes.keys()}
        print(f"  {tf_key}: {relevant}")

    for tf_key, changes in UPDATES.items():
        for param, val in changes.items():
            cfg["params_by_tf"][tf_key][param] = val

    print("\n=== DEPOIS (preview) ===")
    for tf_key, changes in UPDATES.items():
        p = cfg["params_by_tf"][tf_key]
        relevant = {k: p.get(k) for k in changes.keys()}
        print(f"  {tf_key}: {relevant}")

    save_full_config(cfg, updated_by=UPDATED_BY)

    # Recarregar para validar
    cfg2 = json.loads(CONFIG_PATH.read_text())
    print(f"\n[OK] v{cfg2['_version']} | updated_by={cfg2['_updated_by']} | at={cfg2['_updated_at']}")
    print("\n=== VERIFICAÇÃO PÓS-SAVE ===")
    for tf_key in UPDATES:
        p = cfg2["params_by_tf"][tf_key]
        relevant = {k: p.get(k) for k in UPDATES[tf_key].keys()}
        print(f"  {tf_key}: {relevant}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())