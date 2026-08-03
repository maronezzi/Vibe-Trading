#!/usr/bin/env python3
"""
scripts/w881_apply_optimization_20260803.py
===========================================
Script de manutenção (Wave 881 — stand-in LLM, 03/08/2026).

APLICA 6 otimizações de estratégia/params validadas por sweep completo do
Stage 3 do AGI v4 (engine canônica) em 13 pares lucrativos. Todas as 6
passaram TODOS os gates (profitability + walk-forward) com WF >= 75% (3/4
ou 4/4 janelas), bem acima do gate de 65%.

Ganho agregado estimado em PnL 30d: +R$1.850 (soma das 6 melhorias).

MUDANÇAS:
  WIN_M5:  SMART_EMA     → BOLLINGER    (+R$745, WF 4/4)
  WSP_M15: ADX_TREND     → BOLLINGER    (+R$461, WF 3/4)
  WDO_M15: EMA_PULLBACK  → EMA_PULLBACK (+R$354, WF 3/4) — só params
  BIT_M30: BOLLINGER     → BOLLINGER    (+R$249, WF 3/4) — só params (6→37t)
  BIT_M15: RANGE_TRADING → BOLLINGER    (+R$24, WF 4/4)
  WDO_M5:  ADX_TREND     → BOLLINGER    (+R$17, WF 4/4)

DECISÃO: operador (Bruno) autorizou aplicação das 6 em 03/08/2026.

PRECAUÇÃO DE RISCO (mesclagem, não substituição):
  Os params_by_tf atuais contêm chaves de RISCO AO VIVO (max_consecutive_losses,
  halt_duration_minutes, profit_lock_r, etc.) que o BACKTEST IGNORA (filter
  _BACKTEST_ACTIVE_UNIVERSAL só usa sl_atr_mult + cooldown_seconds) mas o
  autotrader usa. Este script MESCLA os params validados sobre os existentes,
  preservando as chaves de risco ao vivo. Só sobrescreve: sl_atr_mult,
  cooldown_seconds, e os params de indicador específicos da nova estratégia.

Snapshot manual prévio em vt_config.json.snapshot_pre_w881_opt_<TS>.
save_full_config atômico → seguro durante runtime.

Uso:
    python3 scripts/w881_apply_optimization_20260803.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT))
sys.path.insert(0, str(_PROJECT / "core"))

from vt_config_loader import load_config, save_full_config


# ─── As 6 mudanças validadas (params completos do melhor candidato) ─────────
# Cada entrada: (par, estratégia_final, params_a_mesclar_sobre_o_existente)
CHANGES = [
    ("WIN_M5", "BOLLINGER", {
        "bb_period": 30, "bb_std": 3.0, "cooldown_seconds": 900, "sl_atr_mult": 2.0,
    }),
    ("WSP_M15", "BOLLINGER", {
        "bb_period": 10, "bb_std": 1.5, "cooldown_seconds": 60, "sl_atr_mult": 1.2,
    }),
    ("WDO_M15", "EMA_PULLBACK", {
        "cooldown_seconds": 60, "ema_fast": 5, "ema_slow": 35,
        "pullback_pct": 0.15, "sl_atr_mult": 3.0,
    }),
    ("BIT_M30", "BOLLINGER", {
        "bb_period": 10, "bb_std": 1.5, "cooldown_seconds": 60, "sl_atr_mult": 1.2,
    }),
    ("BIT_M15", "BOLLINGER", {
        "bb_period": 10, "bb_std": 1.5, "cooldown_seconds": 60, "sl_atr_mult": 1.2,
    }),
    ("WDO_M5", "BOLLINGER", {
        "bb_period": 18, "bb_std": 3.0, "cooldown_seconds": 180, "sl_atr_mult": 3.0,
    }),
]


def main() -> int:
    cfg = load_config(force=True)
    print(f"Config v{cfg.get('_version')} lido (by {cfg.get('_updated_by')}).")

    sbt = cfg.get("strategy_by_tf", {})
    pbt = cfg.get("params_by_tf", {})
    changes_applied = []

    for pair, new_strat, new_params in CHANGES:
        old_strat = sbt.get(pair, "?")
        old_params = dict(pbt.get(pair, {}))

        # Mescla: preserva chaves de risco ao vivo, sobrescreve só as validadas
        merged = dict(old_params)
        merged.update(new_params)

        sbt[pair] = new_strat
        pbt[pair] = merged

        # Reporta o que mudou
        strat_changed = old_strat != new_strat
        param_changes = {k: (old_params.get(k, "—"), v) for k, v in new_params.items()
                         if old_params.get(k) != v}
        if strat_changed:
            changes_applied.append(f"{pair}: {old_strat} → {new_strat} + params {param_changes}")
        else:
            changes_applied.append(f"{pair}: mantém {new_strat}, params ajustados {param_changes}")

    cfg["strategy_by_tf"] = sbt
    cfg["params_by_tf"] = pbt

    print(f"\nAplicando {len(changes_applied)} mudança(s):")
    for c in changes_applied:
        print(f"  • {c}")

    save_full_config(cfg, updated_by="bruno_wave_881_optimization")
    print(f"\nConfig salvo (nova versão: v{cfg['_version']}).")
    print(
        "\nDEPOIS: o autotrader (se rodando) recarrega config no próximo loop. "
        "As 6 estratégias/params otimizadas entram em produção. Monitorar "
        "primeiros trades de cada par alterado vs backtest esperado."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
