#!/usr/bin/env python3
"""VT Meio-Dia Tuning — script canônico (whitelist `vt_config_loader.py`).

Bruno 2026-07-01 (Regra de Ouro): apenas este nome de script está na whitelist
de writers de vt_config.json (vide `core/vt_config_loader.py:ALLOWED_WRITERS`).
Para tuning de meio-dia, invoque ESTE arquivo — não crie variantes.

Uso:
    /usr/bin/python3 scripts/vt_meio_dia_tuning.py [--dry-run]

Wave 11h — 2026-07-13 (meio-dia)
Roteiro briefing → blocos reais em params_by_tf[<sym>_<tf>]:
  BOLLINGER (WIN)     → WIN_M5/M15/M30/H1 (mix Wave 11h)
  VWAP (BIT)          → BIT_M5/M15/M30/H1 (mix Wave 11h)
  EMA_PULLBACK (DOL)  → WDO_M5/M15/M30/H1 (mix Wave 11h)
  MACD_MOMENTUM (WSP) → WSP_M5/M15/M30/H1 (mix Wave 11h)

Evidência manhã (entrada < 12h00):
  WINQ26 M15 STRONG_TREND   → 4 trades, WR=0%,  PnL=-50,2 (ADX=20 muito permissivo)
  WINQ26 M5  EMA_CROSSOVER  → 1 trade,  PnL=-67  (cooldown 60s permite reentradas)
  WINQ26 M30 HTF_BIAS       → 1 trade,  GHOST/RECONCILE (não causado por params)
  BITN26 M5  HTF_BIAS       → 3 trades, WR=0%,  PnL=-4,8 (rsi_oversold=20 cutuca)
  BITN26 M30 RSI_REVERSION  → 1 trade,  PnL=0   (cooldown curto)
  WDO/WSP                   → 0 trades (sem evidência — não mexe)

Mudanças (2 por ativo, conservadoras; sem mexer em estratégia/sl_atr_mult):
  WIN_M15.adx_threshold      20 → 25  (STRONG_TREND, filtrar chop)
  WIN_M5.cooldown_seconds    60 → 90  (EMA_CROSSOVER, anti reentradas)
  BIT_M5.rsi_oversold        20 → 25  (HTF_BIAS, evitar reversão prematura)
  BIT_M30.cooldown_seconds   60 → 120 (RSI_REVERSION, espaçar entradas)
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.vt_config_loader import load_config, save_full_config  # noqa: E402

CONFIG_PATH = Path(__file__).resolve().parent.parent / "vt_config.json"

CHANGES = [
    ("WIN_M15", "adx_threshold",    20, 25,
     "STRONG_TREND — ADX 20→25 filtra chop e ruído (principal causa do WR=0%)"),
    ("WIN_M5",  "cooldown_seconds", 60, 90,
     "EMA_CROSSOVER — cooldown 60→90 corta reentradas rápidas"),
    ("BIT_M5",  "rsi_oversold",     20, 25,
     "HTF_BIAS — rsi_oversold 20→25 evita reversão prematura"),
    ("BIT_M30", "cooldown_seconds", 60, 120,
     "RSI_REVERSION — cooldown 60→120 espaçar entradas"),
]

PER_ASSET = {}
for tf_key, *_ in CHANGES:
    sym = tf_key.split("_", 1)[0]
    PER_ASSET[sym] = PER_ASSET.get(sym, 0) + 1
assert all(n <= 2 for n in PER_ASSET.values()), f"Violação <=2/ativo: {PER_ASSET}"


def main(dry_run: bool) -> int:
    ts = time.strftime("%Y%m%d_%H%M%S")
    snap = CONFIG_PATH.with_name(f"vt_config.json.bak.pre_meio_dia_{ts}")
    shutil.copy2(CONFIG_PATH, snap)
    print(f"[SNAPSHOT] {snap}")

    cfg = load_config()
    pbtf = cfg.get("params_by_tf") or {}

    print("\n=== Mudanças propostas ===")
    for tf_key, param, old, new, why in CHANGES:
        cur = pbtf.get(tf_key, {}).get(param)
        flag = "OK" if cur == old else f"MISMATCH(atual={cur})"
        print(f"  {tf_key}.{param}: {old} -> {new}  [{flag}]  {why}")

    if dry_run:
        print("\n[DRY-RUN] nenhuma escrita realizada.")
        return 0

    # 1) Aplicar em memoria
    for tf_key, param, _old, new, _why in CHANGES:
        pbtf.setdefault(tf_key, {})[param] = new

    cfg["params_by_tf"] = pbtf
    cfg["halt_new_trades"] = True  # pausa enquanto escreve (defesa em profundidade)
    cfg["_updated_by"] = "hermes_wave_11h_meio_dia_20260713"

    ok = save_full_config(cfg, updated_by="hermes_wave_11h_meio_dia_20260713")
    print(f"\n[WRITE] save_full_config ok={ok}")

    # 2) Sanity relido
    cfg2 = load_config(force=True)
    print(f"[VERIFY] _version={cfg2.get('_version')} _updated_by={cfg2.get('_updated_by')}")
    for tf_key, param, _old, new, _why in CHANGES:
        got = cfg2.get("params_by_tf", {}).get(tf_key, {}).get(param)
        print(f"  relido {tf_key}.{param} = {got} (esperado {new})")

    # 3) Liberar autotrader
    cfg2["halt_new_trades"] = False
    cfg2["_updated_by"] = "hermes_wave_11h_meio_dia_20260713_release"
    ok2 = save_full_config(cfg2, updated_by="hermes_wave_11h_meio_dia_20260713_release")
    print(f"\n[RELEASE] halt_new_trades=False, ok={ok2}")

    # 4) Notificar via Telegram (reusa helper do autotrader)
    try:
        from core.vt_hermes_helper import hermes_send  # noqa: E402
        from core.vt_autotrader import TELEGRAM_TARGET  # noqa: E402
        msg_lines = [
            "🛠️ Tuning Meio-Dia Wave 11h (2026-07-13)",
            f"_version={cfg2.get('_version')}",
            "",
            "Mudancas (2 WIN + 2 BIT, sem trocar estrategia/sl_atr_mult):",
        ]
        for tf_key, param, old, new, why in CHANGES:
            msg_lines.append(f"- {tf_key}.{param}: {old}->{new}  ({why})")
        msg_lines.append("")
        msg_lines.append(f"Snapshot: {snap.name}")
        msg_lines.append(f"halt_new_trades liberado: {not cfg2.get('halt_new_trades')}")
        ok3 = hermes_send(TELEGRAM_TARGET, "\n".join(msg_lines))
        print(f"[NOTIFY] hermes_send ok={ok3}")
    except Exception as e:  # noqa: BLE001
        print(f"[NOTIFY] Falhou (nao-critico): {e}")

    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    sys.exit(main(dry_run=args.dry_run))