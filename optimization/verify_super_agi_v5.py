#!/usr/bin/env python3
"""
verify_super_agi_v5.py — Compara candidatos do SUPER-AGI v5 contra a
estratégia ATUAL do config nas MESMAS barras MT5 (Regra 1 honesta).

Para cada par com candidato:
  1. Roda a ESTRATÉGIA ATUAL do config nas mesmas 30d
  2. Roda o CANDIDATO (best do report.json) nas mesmas 30d
  3. Compara e mostra delta

O output é uma tabela dry-run: SUGERE mudanças, NÃO aplica.

Usage:
    /usr/bin/python3 optimization/verify_super_agi_v5.py \
        --report /tmp/super_agi_v5_prod/report.json \
        --config vt_config.json \
        [--apply]   # Só com user explicit opt-in
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

log = logging.getLogger("verify_super_agi_v5")


def _evaluate(params: dict, df, sym: str, tf: str, strategy: str) -> dict:
    """Roda backtest_combo e retorna métricas em R$."""
    from backtest import backtest_v944 as bt
    bt.load_strategies()
    trades = bt.backtest_combo(df, sym, tf, strategy, params)
    if not trades:
        return {"pnl": 0.0, "n_trades": 0, "wr": 0.0, "max_dd": 0.0}
    pnls = [float(t.get("pnl", 0)) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = len(pnls)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    wr = len(wins) / n * 100 if n else 0.0
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        dd = equity - peak
        if dd < max_dd:
            max_dd = dd
    return {
        "pnl": round(sum(pnls), 2),
        "n_trades": n,
        "wr": round(wr, 2),
        "pf": round(min(pf, 99.99), 3),
        "max_dd": round(max_dd, 2),
    }


def _fetch_bars(sym: str, tf: str, n_bars: int = 2500):
    from backtest import backtest_v944 as bt
    resolved_path = bt.fetch(sym, tf, n_bars)
    if not resolved_path:
        return None
    return bt.load_csv(resolved_path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True, help="report.json do super_agi_v5")
    parser.add_argument("--config", default="vt_config.json")
    parser.add_argument("--apply", action="store_true",
                       help="APLICA o update no config (default: dry-run, só imprime)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                       format="%(asctime)s [%(levelname)s] %(message)s")

    # Carrega report + config
    report = json.loads(Path(args.report).read_text())
    config = json.loads(Path(args.config).read_text())

    # Itera pares com candidato
    candidates = []
    for pair_key, info in report.get("per_pair", {}).items():
        if not info.get("top_k"):
            continue
        # Dedup por (strategy, params_hash) — pega APENAS o melhor único
        seen = set()
        unique = []
        for c in info["top_k"]:
            params = c.get("params", {})
            params_key = tuple(sorted(params.items()))
            sig = (c["strategy"], params_key)
            if sig in seen:
                continue
            seen.add(sig)
            unique.append(c)
            if len(unique) >= 3:
                break
        info["top_k_dedup"] = unique

        candidates.append({
            "pair": pair_key,
            "best": unique[0],
            "alternatives": unique[1:],
        })

    if not candidates:
        log.error("Nenhum candidato no report")
        return 1

    # Compara cada par
    print()
    print("=" * 110)
    print(f"  VERIFICAÇÃO SUPER-AGI v5 — comparando candidatos vs config ATUAL")
    print(f"  Config version: {config.get('_version')}")
    print(f"  Report: {args.report}")
    print("=" * 110)
    print()
    print(f"{'Par':<10} {'Status':<10} {'Strategy':<22} {'PnL R$':>10} "
          f"{'PF':>5} {'WR%':>5} {'n':>4} {'DD R$':>9}  {'Decision':<22}")
    print("-" * 110)

    bar_counts = {"M5": 2500, "M15": 900, "M30": 500, "H1": 260}
    strategy_by_tf = config.get("strategy_by_tf", {})
    params_by_tf = config.get("params_by_tf", {})

    recommendations = []

    for cand in candidates:
        pair = cand["pair"]
        sym_root, tf = pair.split("_", 1)
        best = cand["best"]

        # Simbol mapping para fetch
        resolved = config.get("resolved_symbols", {})
        full_sym = resolved.get(sym_root, f"{sym_root}$")

        # 1) Estrategia ATUAL
        current_strat = strategy_by_tf.get(pair)
        current_params = params_by_tf.get(pair, {})
        if not current_strat:
            current_strat = config.get("strategy", {}).get(sym_root, "VWAP")

        # 2) CANDIDATO
        cand_strat = best["strategy"]
        cand_params = best.get("params", {})

        # Fetch bars (1x per pair)
        n_bars = bar_counts.get(tf, 2500)
        df = _fetch_bars(full_sym, tf, n_bars)
        if df is None:
            print(f"{pair:<10} {'NO BARS':<10}")
            continue

        # Run both
        current_m = _evaluate(current_params, df, sym_root, tf, current_strat)
        cand_m_raw = best["full"]
        # Normalize: cand_m tem chaves 'total_pnl', current_m tem 'pnl'
        cand_m = {
            "pnl": cand_m_raw.get("total_pnl", cand_m_raw.get("pnl", 0)),
            "pf": cand_m_raw.get("pf", 0),
            "wr": cand_m_raw.get("wr", 0),
            "n_trades": cand_m_raw.get("n_trades", 0),
            "max_dd": cand_m_raw.get("max_dd", 0),
            "sharpe": cand_m_raw.get("sharpe", 0),
        }

        # Decision: aplica só se candid > current E ambos com PF > 1
        cand_pnl = cand_m.get("total_pnl", cand_m.get("pnl", 0))
        cur_pnl = current_m.get("pnl", 0)
        delta_pnl = cand_pnl - cur_pnl
        pct_improvement = (delta_pnl / abs(cur_pnl) * 100) if cur_pnl != 0 else float("inf")

        apply_status = "✅ APPLY"
        rejection_reason = ""
        if cand_pnl <= 0:
            apply_status = "❌ REJECT (cand<0)"
            rejection_reason = "candidate PnL negative"
        elif cur_pnl > 0 and cand_pnl <= cur_pnl:
            apply_status = "❌ REJECT (not better)"
            rejection_reason = f"cand {cand_pnl:.0f} <= current {cur_pnl:.0f}"
        elif cur_pnl <= 0 and cand_pnl <= 0:
            apply_status = "❌ REJECT (both lose)"
            rejection_reason = "both negative"
        elif cand_m["n_trades"] < 15:
            apply_status = "⚠️ SMALL SAMPLE"
            rejection_reason = f"n={cand_m['n_trades']} < 15"

        # Imprime linha current + linha cand
        print(f"{pair:<10}")
        print(f"  current: {current_strat:<22} {cur_pnl:>+9.0f} "
              f"{current_m['pf']:>5.2f} {current_m['wr']:>5.1f} {current_m['n_trades']:>4d} "
              f"{current_m['max_dd']:>+9.0f}")
        print(f"  cand:    {cand_strat:<22} {cand_pnl:>+9.0f} "
              f"{cand_m.get('pf', 0):>5.2f} {cand_m.get('wr', 0):>5.1f} {cand_m.get('n_trades', 0):>4d} "
              f"{cand_m.get('max_dd', 0):>+9.0f}    Δ={delta_pnl:+.0f} ({pct_improvement:+.0f}%)")
        print(f"  → {apply_status}  {rejection_reason}")
        print()

        if apply_status == "✅ APPLY":
            recommendations.append({
                "pair": pair,
                "current_strategy": current_strat,
                "new_strategy": cand_strat,
                "current_pnl_30d": cur_pnl,
                "new_pnl_30d": cand_pnl,
                "delta_pnl": delta_pnl,
                "params": cand_params,
                "metrics": cand_m,
            })

    print("-" * 110)
    print()
    print(f"📋 Recomendações para aplicar: {len(recommendations)}")

    print("-" * 110)
    print()
    print(f"📋 Recomendações para aplicar: {len(recommendations)}")
    if recommendations:
        total_delta = sum(r["delta_pnl"] for r in recommendations)
        print(f"   Δ PnL total projetado 30d: R$ {total_delta:+,.0f}")
        print()
        for r in recommendations:
            print(f"   • {r['pair']}: {r['current_strategy']} → {r['new_strategy']} "
                  f"(Δ R$ {r['delta_pnl']:+.0f})")

    if not args.apply:
        print()
        print("⚠️  MODO DRY-RUN — nenhuma alteração feita.")
        print(f"   Para aplicar: --apply")
        return 0

    # MODO APPLY
    print()
    print("🚀 APLICANDO MUDANÇAS...")
    from core.vt_config_loader import load_config, save_full_config

    # Carrega config atualizada (read-modify-write)
    cfg = load_config(force=True)
    if not cfg:
        print("❌ Não foi possível carregar config — abortando")
        return 3

    # Salva recommendations também em JSON para auditoria
    rec_path = Path(args.report).parent / "recommendations.json"
    rec_path.write_text(json.dumps(recommendations, ensure_ascii=False, indent=2))
    print(f"   Audit: {rec_path}")

    # Aplica cada recomendação em strategy_by_tf + params_by_tf
    strategy_by_tf = cfg.setdefault("strategy_by_tf", {})
    params_by_tf = cfg.setdefault("params_by_tf", {})

    for r in recommendations:
        print(f"   {r['pair']}: {r['current_strategy']} → {r['new_strategy']}", end=" ... ")
        try:
            strategy_by_tf[r["pair"]] = r["new_strategy"]
            if r.get("params"):
                params_by_tf.setdefault(r["pair"], {}).update(r["params"])
            print("✓")
        except Exception as e:
            print(f"❌ {e}")

    # Wave 12 — bump version + lineage
    cfg["_version"] = cfg.get("_version", 0) + 1
    cfg["_updated_at"] = __import__("datetime").datetime.now().isoformat()
    cfg["_updated_by"] = "super_agi_v5_wave_12_sunday"

    try:
        save_full_config(cfg, updated_by="super_agi_v5_wave_12_sunday")
        print()
        print(f"✅ Config salvo: v{cfg['_version']} (by {cfg['_updated_by']})")
    except Exception as e:
        print(f"❌ save_full_config falhou: {e}")
        return 4

    return 0


if __name__ == "__main__":
    sys.exit(main())
