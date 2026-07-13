#!/usr/bin/env python3
"""W877 Trader-IA 11h — Strategy swap cirúrgico baseado em backtest 30d.

Wave: hermes_wave_11h_w13_jul_strategy_swap (2026-07-13, seg, 11h00-11h10).

Contexto (validado 11:00 via backtest/backtest_v944.py + mt5_fetch):
  Edge por par (PnL R$ / n / WR% em 30d — base 7d/15d/30d/60d):

  WIN_M15:
    HTF_BIAS_LTF_ENTRY (atual) 30d:  n=132 pnl=+6324 wr=33.3%  | 60d: +6338
    STRONG_TREND         (cand)  30d:  n=128 pnl=+9088 wr=33.6%  | 60d: +10514
    Improvement: +R$2764/mês (60d: +R$4176/mês) — n e WR praticamente idênticos.
    Estabilidade 7d/15d/30d/60d consistente: STRONG_TREND >= HTF em todas as janelas.

  BIT_M30:
    PIVOT_POINTS  (atual) 30d:  n=22 pnl=+67   wr=45.5%  | 60d: +74
    RSI_REVERSION (cand)  30d:  n=66 pnl=+204  wr=40.9%  | 60d: +218
    Improvement: +R$137/mês (60d: +R$144/mês) — n triplicou (22→66), WR consistente
    dentro da banda (40-46%).

NÃO troca:
  - WIN_H1: HTF (60d +6062) vs RSI_REVERSION (60d +7049, +16%) — melhoria marginal,
    preferi conservadora (regra: Bruno não aceita swap marginal <30% improvement).
  - BIT_M5: HTF (30d +190) vs STRONG_TREND (30d +298, +R$108 em 145 trades) —
    R$108/mês em 145 trades não compensa risco de swap. Mantém HTF.
  - WIN_M30, BIT_M5/M15/H1, WSP_*, WDO_*: cada par já em sua melhor estratégia
    pelo backtest 30d.

Uso: autotrader VIVO (hot-reload em ≤30s). Mudanças são aplicadas via
save_full_config (atômico: tmp + os.replace).
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.vt_config_loader import load_config, save_full_config

# Trocas validadas via backtest 30d/60d (Wave 877, 2026-07-13 11h)
STRATEGY_SWAPS = {
    "WIN_M15": "STRONG_TREND",   # HTF_BIAS_LTF_ENTRY → STRONG_TREND (+R$2764/mês 30d)
    "BIT_M30": "RSI_REVERSION",  # PIVOT_POINTS       → RSI_REVERSION (n triplica, +R$137/mês)
}


def main() -> int:
    dry = "--dry-run" in sys.argv
    cfg = load_config(force=True)

    print("=== Wave W877 — Trader-IA 11h (2026-07-13) ===")
    print(f"Config inicial: v{cfg.get('_version')} by={cfg.get('_updated_by')}")
    print()

    by_tf = cfg.setdefault("strategy_by_tf", {})
    print(f"=== STRATEGY SWAPS ({len(STRATEGY_SWAPS)} pares) ===")
    for tf_key, new_strat in sorted(STRATEGY_SWAPS.items()):
        old_strat = by_tf.get(tf_key, "(herda de strategy[symbol])")
        print(f"  {tf_key}: {old_strat} → {new_strat}")

    # Sanity: STRONG_TREND e RSI_REVERSION são estratégias válidas (existem em strategies/)
    from core.vt_strategy_loader import load_strategies
    available = set(load_strategies(force=True).keys())
    for new_strat in set(STRATEGY_SWAPS.values()):
        if new_strat not in available:
            print(f"❌ ERRO: {new_strat} não existe em strategies/ — abort")
            return 1
    print("\nInvariants OK (todas as estratégias alvo existem em strategies/)")

    if dry:
        print("\n[DRY-RUN] nenhuma mudança aplicada.")
        return 0

    for tf_key, new_strat in STRATEGY_SWAPS.items():
        by_tf[tf_key] = new_strat
    cfg["strategy_by_tf"] = by_tf

    save_full_config(cfg, updated_by="hermes_wave_11h_w13_jul_strategy_swap")
    cfg2 = load_config(force=True)

    print()
    print(f"=== Salvo v{cfg2.get('_version')} by={cfg2.get('_updated_by')} ===")
    print("strategy_by_tf final (alterados):")
    for k in sorted(STRATEGY_SWAPS):
        print(f"  {k} → {cfg2['strategy_by_tf'].get(k)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
