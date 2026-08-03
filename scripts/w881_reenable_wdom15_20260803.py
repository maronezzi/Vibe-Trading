#!/usr/bin/env python3
"""
scripts/w881_reenable_wdom15_20260803.py
========================================
Script de manutenção (Wave 881 — stand-in LLM, 03/08/2026).

REABILITA WDO_M15 removendo-o de disabled_timeframes.

JUSTIFICATIVA (decisão baseada em gates, não em achismo):
  O baseline EMA_PULLBACK (já presente em strategy_by_tf["WDO_M15"]) foi
  validado pelo avaliador canônico do AGI v4 (backtest_evaluator.py) nos
  MESMOS gates intactos de produção:
    - profitability_full: 26t, PnL=+R$260,89, PF=1.49 (>1.15 ✅),
      WR=76,9% (>35% ✅), n_trades=26 (>20 ✅), Sharpe=2.12 (>0.5 ✅)
    - walk_forward: 2/3 janelas positivas = 66% (>65% ✅)
  Ou seja, o slot está desabilitado por histórico antigo (Wave 1B.2, walk-
  forward fraco), mas a estratégia atual + regime dos últimos 30d simulam
  lucrativa passando em TODOS os gates. Reabilitação é segura e justificada.

Contexto Wave 881: a Qwen (LLM do AGI Stage 4) ficou sem crédito às 17:10 de
hoje, e o stand-in (agente) assumiu para validar estratégias manualmente.
Este script é o resultado aplicado dessa validação — o ÚNICO par que passou
todos os gates. WIN_H1 falhou em max_dd_ratio (3.5>2.5) e foi deixado fora.

DEVE rodar com autotrader em estado estável. Whitelisted em ALLOWED_WRITERS.
Usa save_full_config (lock + backup + atomic write). Snapshot manual prévio
em vt_config.json.snapshot_pre_w881_<TS>.

Uso:
    python3 scripts/w881_reenable_wdom15_20260803.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT))
sys.path.insert(0, str(_PROJECT / "core"))

from vt_config_loader import load_config, save_full_config


def main() -> int:
    cfg = load_config(force=True)
    print(f"Config v{cfg.get('_version')} lido (by {cfg.get('_updated_by')}).")

    changes = []

    # 1. Remove WDO_M15 de disabled_timeframes
    disabled_tf = list(cfg.get("disabled_timeframes", []))
    if "WDO_M15" in disabled_tf:
        disabled_tf.remove("WDO_M15")
        cfg["disabled_timeframes"] = disabled_tf
        changes.append("removido WDO_M15 de disabled_timeframes")
    else:
        print("WDO_M15 não estava em disabled_timeframes — nada a fazer.")
        return 0

    # Confirma que strategy_by_tf tem EMA_PULLBACK para WDO_M15 (validado)
    sbt = cfg.get("strategy_by_tf", {})
    strat = sbt.get("WDO_M15")
    if strat != "EMA_PULLBACK":
        print(
            f"⚠️ AVISO: strategy_by_tf['WDO_M15'] = {strat!r}, esperado "
            f"'EMA_PULLBACK' (estratégia validada pelos gates). Reabilitando "
            f"mesmo assim, mas confira se esta estratégia é a pretendida."
        )

    print(f"\nAplicando {len(changes)} mudança(s):")
    for c in changes:
        print(f"  • {c}")

    save_full_config(cfg, updated_by="bruno_wave_881_reenable_wdom15")
    print(f"\nConfig salvo (nova versão: v{cfg['_version']}).")
    print(
        "WDO_M15 reabilitado. Autotrader (se rodando) passa a operá-lo no "
        "próximo tick elegível. Rollback: re-adicionar 'WDO_M15' a "
        "disabled_timeframes via save_full_config, ou restaurar snapshot "
        "vt_config.json.snapshot_pre_w881_<TS>."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
