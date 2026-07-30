#!/usr/bin/env python3
"""Wave 14.6 — Meio-Dia Tuning 2026-07-15 (cron 12:00).

Briefing do dia (entradas < 12h00):
  WINQ26 M5/M15/M30/H1 → WR=0% em todos, -R$ 641 total
    M5  (SMART_EMA):        9 trades, -R$   51.53
    M15 (HTF_BIAS, BOLLINGER legado): 9 trades, -R$  589.60
    M30 (HTF_BIAS):         5 trades,  R$     0
    H1  (RSI_REVERSION):    2 trades,  R$     0
  BITN26 → 14 trades, WR 28.6%, **+R$ 1615** (POSITIVO, skip)
  WSP    → 0 trades (todos em disabled_timeframes — bloqueio não-paramétrico)
  WDO    → 0 trades (todos em disabled_timeframes — bloqueio não-paramétrico)
  IND    → disabled_symbols (decisão Bruno Wave 14)

Plano conservador (2 parâmetros WIN, NÃO troca estratégia, NÃO mexe
em sl_atr_mult, NÃO remove de disabled_timeframes):

  win.bb_std        2.0 → 2.2   (alarga bandas — menos toques falsos)
  win.rsi_oversold  15  → 25    (afroxa piso oversold p/ mais sinais BUY)

Sem mudanças em WSP/WDO/BIT/IND — ver briefing acima.
Rodar com autotrader PAUSADO (defesa em profundidade).
"""
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.vt_config_loader import load_config, save_full_config  # noqa: E402

CONFIG_PATH = Path(__file__).resolve().parent.parent / "vt_config.json"

CHANGES = [
    (("win", "bb_std"),       2.0, 2.2,
     "BOLLINGER — alarga bandas 2.0→2.2 (menos toques falsos)"),
    (("win", "rsi_oversold"), 15,  25,
     "BOLLINGER — afrouxa piso oversold 15→25 (mais sinais BUY)"),
]


def main(dry_run: bool = False) -> int:
    ts = time.strftime("%Y%m%d_%H%M%S")
    snap = CONFIG_PATH.with_name(f"vt_config.json.bak.pre_meio_dia_{ts}")
    shutil.copy2(CONFIG_PATH, snap)
    print(f"[SNAPSHOT] {snap}")

    cfg = load_config()
    print("\n=== Mudanças propostas ===")
    for path, old, new, why in CHANGES:
        cur = cfg
        for k in path[:-1]:
            cur = cur.get(k, {}) if isinstance(cur, dict) else {}
        cur = cur.get(path[-1])
        flag = "OK" if cur == old else f"MISMATCH(atual={cur})"
        print(f"  {'.'.join(path)}: {old} -> {new}  [{flag}]  {why}")

    if dry_run:
        print("\n[DRY-RUN] nenhuma escrita.")
        return 0

    # aplica
    for path, _old, new, _why in CHANGES:
        d = cfg
        for k in path[:-1]:
            d = d.setdefault(k, {})
        d[path[-1]] = new

    cfg["halt_new_trades"] = True  # pausa defensiva durante write
    cfg["_updated_by"] = "hermes_wave_14_6_meio_dia_20260715"
    ok = save_full_config(cfg, updated_by="hermes_wave_14_6_meio_dia_20260715")
    print(f"\n[WRITE] save_full_config ok={ok}")

    # sanity
    cfg2 = load_config(force=True)
    print(f"\n[VERIFY] _version={cfg2.get('_version')} _updated_by={cfg2.get('_updated_by')}")
    for path, _old, new, _why in CHANGES:
        got = cfg2
        for k in path:
            got = got.get(k, {}) if isinstance(got, dict) else {}
        flag = "OK" if got == new else f"FAIL(={got})"
        print(f"  {'.'.join(path)} = {got} (esperado {new}) [{flag}]")

    # libera
    cfg2["halt_new_trades"] = False
    cfg2["_updated_by"] = "hermes_wave_14_6_meio_dia_20260715_release"
    ok2 = save_full_config(cfg2, updated_by="hermes_wave_14_6_meio_dia_20260715_release")
    print(f"\n[RELEASE] halt_new_trades=False, ok={ok2}")

    # Telegram
    try:
        from core.vt_hermes_helper import hermes_send  # noqa: E402
        from core.vt_autotrader import TELEGRAM_TARGET  # noqa: E402
        lines = [
            "🛠️ Tuning Meio-Dia Wave 14.6 (2026-07-15)",
            f"_version={cfg2.get('_version')}",
            "",
            "Mudancas (2 WIN, sem trocar estrategia/sl_atr_mult):",
        ]
        for path, old, new, why in CHANGES:
            lines.append(f"- {'.'.join(path)}: {old}->{new}  ({why})")
        lines += [
            "",
            "Pulos:",
            "- BIT (PnL +R$ 1615 positivo, mesmo com WR 28.6%)",
            "- WSP/WDO (todos TFs em disabled_timeframes; ajuste param nao destrava)",
            "- IND (disabled_symbols — decisao Bruno Wave 14)",
            "",
            f"Snapshot: {snap.name}",
            f"halt_new_trades liberado: {not cfg2.get('halt_new_trades')}",
        ]
        ok3 = hermes_send(TELEGRAM_TARGET, "\n".join(lines))
        print(f"[NOTIFY] hermes_send ok={ok3}")
    except Exception as e:  # noqa: BLE001
        print(f"[NOTIFY] Falhou (nao-critico): {e}")

    return 0


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    sys.exit(main(dry_run=dry))