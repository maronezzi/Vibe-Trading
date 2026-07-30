#!/usr/bin/env python3
"""VT Meio-Dia Tuning — script canônico (whitelist `vt_config_loader.py`).

Bruno 2026-07-01 (Regra de Ouro): apenas este nome de script está na whitelist
de writers de vt_config.json (vide `core/vt_config_loader.py:ALLOWED_WRITERS`).
Para tuning de meio-dia, invoque ESTE arquivo — não crie variantes.

Uso:
    /usr/bin/python3 scripts/vt_meio_dia_tuning.py [--dry-run]

Wave 15 — 2026-07-22 (meio-dia)
Estratégias ATIVAS hoje (strategy_by_tf do config v1100):
  WIN_M5  = SMART_EMA          | WIN_M15/M30 = HTF_BIAS_LTF_ENTRY | WIN_H1 = RSI_REVERSION
  BIT_M5  = MACD_MOMENTUM      | BIT_M15    = EMA_PULLBACK      | BIT_M30 = RSI_REVERSION | BIT_H1 = ADX_TREND
  WSP_M5  = RSI_REVERSION      | WSP_M15    = ADX_TREND         | WSP_M30 = SUPERTREND    | WSP_H1 = EMA_PULLBACK
  WDO_M5/M15/M30 = ADX_TREND  | WDO_H1     = RSI_REVERSION

Evidência manhã HOJE (entrada < 12h00):
  WINQ26 M30 HTF_BIAS_LTF_ENTRY → 3 trades, WR=67%, PnL=+49.0  (OK, não mexe)
  WINQ26 M5  SMART_EMA          → 2 trades, WR=50%, PnL=+26.8  (OK, não mexe)
  WINQ26 H1  RSI_REVERSION      → 3 trades, WR=0%,  PnL=-1.2   ← PROBLEMA (2 GHOST + 1 SL; rsi_period=5 muito ruidoso no H1)
  BITN26 M15 EMA_PULLBACK       → 2 trades, WR=100%, PnL=+18.2 (OK, não mexe)
  BITN26 M30 RSI_REVERSION      → 6 trades, WR=0%,  PnL=-7.6   ← PROBLEMA (6 BUYs perdedores; rsi_period=5 + oversold=30 gera sinais fracos em queda)
  WSP/WDO                       → 0 trades (sem evidência — não mexe)

Mudanças (1 WIN + 1 BIT, conservadoras; sem mexer em estratégia/sl_atr_mult):
  WIN_H1.rsi_period            5 → 7   (RSI_REVERSION — suaviza RSI no H1, reduz sinais falsos de overbought)
  WIN_H1.rsi_overbought       80 → 85  (RSI_REVERSION — exige overbought mais extremo para SELL)
  BIT_M30.rsi_period           5 → 7   (RSI_REVERSION — suaviza RSI, menos ruído em M30)
  BIT_M30.rsi_oversold        30 → 25  (RSI_REVERSION — exige oversold mais extremo para BUY, filtra faca-caindo)
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
    ("WIN_H1", "rsi_period",      5, 7,
     "RSI_REVERSION — rsi_period 5→7 suaviza RSI no H1, reduz sinais falsos de overbought (WR=0% manhã)"),
    ("WIN_H1", "rsi_overbought", 80, 85,
     "RSI_REVERSION — overbought 80→85 exige extremo mais forte para SELL (2 GHOST + 1 SL de manhã)"),
    ("BIT_M30", "rsi_period",     5, 7,
     "RSI_REVERSION — rsi_period 5→7 suaviza RSI em M30, menos ruído (6 trades WR=0% manhã)"),
    ("BIT_M30", "rsi_oversold",  30, 25,
     "RSI_REVERSION — oversold 30→25 exige extremo mais forte para BUY, filtra faca-caindo"),
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
    cfg["_updated_by"] = "hermes_meio_dia_20260722_w15"

    ok = save_full_config(cfg, updated_by="hermes_meio_dia_20260722_w15")
    print(f"\n[WRITE] save_full_config ok={ok}")

    # 2) Sanity relido
    cfg2 = load_config(force=True)
    print(f"[VERIFY] _version={cfg2.get('_version')} _updated_by={cfg2.get('_updated_by')}")
    for tf_key, param, _old, new, _why in CHANGES:
        got = cfg2.get("params_by_tf", {}).get(tf_key, {}).get(param)
        print(f"  relido {tf_key}.{param} = {got} (esperado {new})")

    # 3) Liberar autotrader
    cfg2["halt_new_trades"] = False
    cfg2["_updated_by"] = "hermes_meio_dia_20260722_w15_release"
    ok2 = save_full_config(cfg2, updated_by="hermes_meio_dia_20260722_w15_release")
    print(f"\n[RELEASE] halt_new_trades=False, ok={ok2}")

    # 4) Notificar via Telegram (reusa helper do autotrader)
    try:
        from core.vt_hermes_helper import hermes_send  # noqa: E402
        from core.vt_autotrader import TELEGRAM_TARGET  # noqa: E402
        msg_lines = [
            "🛠️ Tuning Meio-Dia 2026-07-22 (Wave 15)",
            f"_version={cfg2.get('_version')}",
            "",
            "Mudancas (1 WIN + 1 BIT, sem trocar estrategia/sl_atr_mult):",
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