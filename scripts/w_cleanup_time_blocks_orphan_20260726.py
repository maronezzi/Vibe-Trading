#!/usr/bin/env python3
"""
Wave Cleanup (2026-07-26, dom): remove time_blocks órfão do WINQ26 VWAP.

Contexto
--------
CONFIG["time_blocks"]["WINQ26"] = [{start:9, end:17, strategy:"VWAP", ...}]
foi criado na Wave 8.4 (2026-06-26) quando WIN usava VWAP. Desde então o AGI
trocou as estratégias do WIN (M5=SMART_EMA, M15/M30=HTF_BIAS_LTF_ENTRY,
H1=RSI_REVERSION) — nenhum TF usa VWAP.

Verificação no código (core/vt_autotrader.py:1093-1101): _is_blocked_time()
só aplica o block quando active_strategy == block.strategy. Como nenhum TF
do WIN tem VWAP ativo, o block é INÓCUO (zero efeito comportamental).

Esta limpeza remove APENAS a entrada WINQ26. Mantém BITM26 STRONG_TREND
09h-11h (ainda válido — BIT_M5 usa RANGE_TRADING mas o block é por symbol
root sem strategy filter, então ainda protege).

Uso (com autotrader PAUSADO — data/autotrader.paused presente):
    python3 scripts/w_cleanup_time_blocks_orphan_20260726.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.vt_config_loader import load_config, save_full_config  # noqa: E402

PAUSE_FILE = Path(__file__).parent.parent / "data" / "autotrader.paused"


def main():
    # Gate de segurança: só roda com autotrader pausado
    if not PAUSE_FILE.exists():
        print("❌ ABORT: data/autotrader.paused não existe.")
        print("   Rode: touch data/autotrader.paused  antes deste script.")
        sys.exit(1)

    cfg = load_config(force=True)
    tb = cfg.get("time_blocks", {}) or {}

    print("=== ANTES ===")
    print(f"time_blocks keys: {list(tb.keys())}")
    for k, v in tb.items():
        print(f"  {k}: {v}")

    if "WINQ26" not in tb:
        print("\n✅ WINQ26 já não existe em time_blocks — nada a fazer.")
        return

    # Snapshot do que vamos remover (auditoria)
    removed = tb.pop("WINQ26")
    print(f"\n🗑️  Removido WINQ26: {removed}")

    cfg["time_blocks"] = tb

    print("\n=== DEPOIS ===")
    print(f"time_blocks keys: {list(cfg['time_blocks'].keys())}")
    for k, v in cfg["time_blocks"].items():
        print(f"  {k}: {v}")

    # Sanity: BITM26 deve continuar
    if "BITM26" not in cfg["time_blocks"]:
        print("\n❌ ABORT: BITM26 sumiu — não vou salvar.")
        sys.exit(2)

    # Persiste via API canônica (lock + atomic write + version bump)
    ok = save_full_config(cfg, updated_by="w_cleanup_time_blocks_orphan_20260726")
    if ok:
        print(f"\n✅ vt_config.json atualizado (v{cfg.get('_version')})")
    else:
        print("\n❌ save_full_config retornou False")
        sys.exit(3)


if __name__ == "__main__":
    main()
