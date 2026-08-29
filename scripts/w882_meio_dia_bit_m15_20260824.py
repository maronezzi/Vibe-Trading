#!/usr/bin/env python3
"""VT Meio-Dia Tuning — sessão 13h seg 2026-08-24 (cron Inteligencia Trader).

Estratégia ATIVA no par problemático:
  BIT_M15 = DIVERGENCE_RSI (strategy_by_tf)

Evidência HOJE (2026-08-24, entrada < 13h):
  BITQ26 M15 DIVERGENCE_RSI → 8 trades, WR=12.5%, PnL=-R$23.60 ← ÚNICO PROBLEMA
    Dia de alta forte (BIT 403k → 412k). Todos os 7 SELLs perderam:
    "divergência" bearish falsa em tendência — preço fez higher high com
    pullback < 0.5% e RSI fez lower high por ruído.
  WSPU26 -R$9.37 (8t) / WDOU26 -R$75 (11t, 6 wins) → dentro do ruído, não mexe.
  WINZ26 -R$219 (2t) → SYMBOL STOP já ativo (limite -R$150), sem novas entradas hoje.
  IND desabilitado (decisão Bruno).

Diagnóstico:
  DIVERGENCE_RSI usa min_divergence default 0.005 (0.5% de movimento mínimo
  entre extremos). No BIT (ATR ~2500-3500 pts ≈ 0.7%), 0.5% é RUÍDO — qualquer
  pullback mínimo dispara "divergência". O parâmetro NÃO está definido em
  params_by_tf.BIT_M15 nem na seção bit → cai no default da estratégia.

Validação (forward backtest simulate_forward, 500 barras M15 reais via MT5,
mesmo motor do gate do AGI v4, spec BIT$ mult=0.01):
  current (min_divergence=0.005):  pnl=+R$99.81   n=20  wr=75%   dd=R$56.60
  A (min_divergence=0.01):         pnl=+R$189.65  n=9   wr=100%  dd=R$0.00  ← MELHOR
  B (min_divergence=0.015, lb=20): pnl=-R$37.60   n=5   wr=60%   dd=R$95.51
  C (min_divergence=0.01, lb=20):  pnl=-R$47.47   n=7   wr=71%   dd=R$95.51

Mudança (1 param, 1 ativo — conservadora; sem trocar estratégia, sem mexer
em sl_atr_mult):
  BIT_M15.min_divergence  (ausente/0.005) → 0.01
    Exige movimento mínimo de 1% entre extremos para confirmar divergência —
    filtra os sinais falsos de pullback em tendência. Backtest: +90% PnL,
    WR 75→100%, drawdown zerado, metade dos trades.

Escrita via save_full_config() (whitelist core/vt_config_loader.py) —
atômica, segura durante runtime (hot-reload ≤30s no autotrader).
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.vt_config_loader import load_config, save_full_config  # noqa: E402

CONFIG_PATH = Path(__file__).resolve().parent.parent / "vt_config.json"


def main(dry_run: bool) -> int:
    cfg = load_config(force=True)
    old_version = cfg.get("_version")

    pbt = cfg.setdefault("params_by_tf", {})
    bit_m15 = pbt.setdefault("BIT_M15", {})
    old_val = bit_m15.get("min_divergence", "(default 0.005)")

    assert "BIT_M15" in cfg.get("strategy_by_tf", {}), "BIT_M15 sumiu do strategy_by_tf?"
    assert cfg.get("strategy_by_tf", {}).get("BIT_M15") == "DIVERGENCE_RSI", (
        f"BIT_M15 não é mais DIVERGENCE_RSI ({cfg['strategy_by_tf'].get('BIT_M15')}) — abortar"
    )

    bit_m15["min_divergence"] = 0.01
    cfg["_updated_by"] = "meio_dia_13h_20260824"
    cfg["_notes"] = ("Wave meio-dia 13h 24/08: BIT_M15 DIVERGENCE_RSI min_divergence "
                     "0.005→0.01 — 8t WR=12.5% -R$23.60 manhã (divergência falsa em "
                     "tendência); forward backtest 500b: +R$99.81→+R$189.65, WR 75→100%, "
                     "dd R$56.60→0")

    print(f"[MEIO-DIA] v{old_version} | BIT_M15.min_divergence: {old_val} -> 0.01")
    print(f"[MEIO-DIA] _updated_by: meio_dia_13h_20260824")

    if dry_run:
        print("[DRY-RUN] Nenhuma escrita.")
        return 0

    backup = CONFIG_PATH.with_suffix(f".json.bak_meio_dia_20260824")
    shutil.copy2(CONFIG_PATH, backup)
    print(f"[BACKUP] {backup.name}")

    save_full_config(cfg, updated_by="meio_dia_13h_20260824")
    print("[OK] Config salvo (atômico). Autotrader hot-reload ≤30s.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    sys.exit(main(args.dry_run))
