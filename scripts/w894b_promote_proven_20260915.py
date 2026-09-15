#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wave 894B (Bruno 15/09/2026) — promove o comprovado, rebaixa o crônico.

Decisão humana com base em 90d de dados live+sombra (até 14/09):
- LIVE total do sistema: -R$764 (628 trades) — a sim do AGI (+R$30k/30d)
  não transfere para o real.
- HTF_BIAS_LTF_ENTRY: +R$665 em 83 trades live no WIN_M15 — melhor par
  live da história — removida pelo AGI em 11/08; o substituto
  (AGI4_WIN_121815) perdeu -R$503 desde então. VOLTA ao WIN_M15 com os
  params históricos (snapshot de 10/08: bak.pre_no_changes_fix_20260810).
- EMA_SLOPE_MOMENTUM: +R$96 em 156 trades de SOMBRA no WDO_M5
  (14/08→04/09, forward walker) e zero trades live — nunca recebeu a vez.
  Assume o WDO_M5 com os params do config v1344 (02/09, dentro da janela
  sombreada). NOTA: reativa um par live-killed em 14/09 — sobressaída
  CONSCIENTE da quarentena §13 (a evidência é de sombra em mercado real,
  não sim de grid; se sangrar, o kill-switch recalibrado pega em n=4).
- BIT_M5/BIT_H1 (ADX_TREND): desativados. ADX_TREND é crônico
  (-R$410 live/90d, família) e a sombra também o dá negativo
  (-R$46/27t). WSP_H1 (EMA_PULLBACK, -R$408/90d) já está disabled no
  config da VPS desde 14/09 — mantido.

One-shot. Rodar com autotrader PAUSADO (fora do pregão).
Escrita via save_full_config (ALLOWED_WRITERS) — atômica e versionada.
"""
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

UPDATED_BY = "bruno_human_decision_wave894b_promote_proven"

# Params históricos (recuperados de snapshots — ver docstring)
PARAMS_WIN_M15_HTF_BIAS = {
    "cooldown_seconds": 1800,
    "halt_duration_minutes": 45,
    "max_consecutive_losses": 3,
    "profit_lock_r": 0.7,
    "sl_atr_mult": 2.0,
    "adx_min": 22,
    "trail_activate": 0.8,
    "trail_distance": 0.4,
}
PARAMS_WDO_M5_EMA_SLOPE = {
    "cooldown_seconds": 60,
    "sl_atr_mult": 0.8,
    "adx_period": 14,
    "ema_period": 32,
    "slope_lookback": 4,
    "slope_threshold": 0.3,
    "adx_min": 25,
}


def main() -> int:
    # Guarda: não rodar com o daemon de pé (escrita de config em runtime)
    try:
        r = subprocess.run(["pgrep", "-f", "core/vt_autotrader.py"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            print("🚫 autotrader rodando — rode fora do pregão/daemon parado")
            return 2
    except FileNotFoundError:
        pass  # pgrep ausente — segue (ambientes de teste)

    from core.vt_config_loader import load_config, save_full_config

    cfg = load_config(force=True)
    print(f"config base: _version={cfg.get('_version')} "
          f"_updated_by={cfg.get('_updated_by')}")

    before = {
        "WIN_M15": (cfg.get("strategy_by_tf") or {}).get("WIN_M15"),
        "WDO_M5": (cfg.get("strategy_by_tf") or {}).get("WDO_M5"),
        "disabled": list(cfg.get("disabled_timeframes") or []),
    }

    # 1) WIN_M15 ← HTF_BIAS_LTF_ENTRY (volta do vencedor comprovado)
    cfg.setdefault("strategy_by_tf", {})["WIN_M15"] = "HTF_BIAS_LTF_ENTRY"
    cfg.setdefault("params_by_tf", {})["WIN_M15"] = dict(PARAMS_WIN_M15_HTF_BIAS)

    # 2) WDO_M5 ← EMA_SLOPE_MOMENTUM (sombra +R$96/156t) + reativa o par
    cfg.setdefault("strategy_by_tf", {})["WDO_M5"] = "EMA_SLOPE_MOMENTUM"
    cfg.setdefault("params_by_tf", {})["WDO_M5"] = dict(PARAMS_WDO_M5_EMA_SLOPE)
    disabled = [d for d in (cfg.get("disabled_timeframes") or []) if d != "WDO_M5"]

    # 3) BIT_M5/BIT_H1 fora (ADX_TREND crônico; sombra negativa)
    for pair in ("BIT_M5", "BIT_H1"):
        if pair not in disabled:
            disabled.append(pair)
    cfg["disabled_timeframes"] = disabled
    dti = cfg.setdefault("day_trade_intent", {})
    dti.update({"WDO_M5": True, "BIT_M5": False, "BIT_H1": False})

    # 4) Nota de linhagem
    notas = cfg.get("_notes") or ""
    cfg["_notes"] = (notas + " | " if notas else "") + (
        "Wave 894B 15/09: WIN_M15→HTF_BIAS_LTF_ENTRY (+R$665/83t live; params "
        "snapshot 10/08); WDO_M5→EMA_SLOPE_MOMENTUM (+R$96/156t sombra; params "
        "v1344; reativa par live-killed 14/09 por decisão humana com evidência "
        "de sombra — quarentena §13 sobressaída consciente); BIT_M5/BIT_H1 "
        "desativados (ADX_TREND −R$410/90d live, sombra negativa)"
    )

    print("WIN_M15:", before["WIN_M15"], "→ HTF_BIAS_LTF_ENTRY")
    print("WDO_M5 :", before["WDO_M5"], "→ EMA_SLOPE_MOMENTUM (reativa par)")
    print("disabled_timeframes:", before["disabled"], "→", cfg["disabled_timeframes"])

    save_full_config(cfg, updated_by=UPDATED_BY)
    print(f"✅ gravado por {UPDATED_BY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
