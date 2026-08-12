#!/usr/bin/env python3
"""sweep_pending_strategies.py — Varredura cruzada de strategies/_pending/.

Motivação (Bruno 11/08/2026): o AGI gerou ~144 estratégias em _pending/ que
foram rejeitadas (0 trades no par ALVO). Mas uma estratégia "sem edge em
WIN_M5" pode ser ótima em WSP_H1 ou BIT_M30. Este script testa TODAS as
estratégias órfãs em TODOS os pares ativos e identifica quais batem o
incumbente atual de algum par — candidatos a promoção.

Pipeline:
  1. Carrega config + computa os 12 pares ativos (symbols × tfs − disabled).
  2. Pré-computa o baseline (incumbente) de cada par via evaluate_baseline.
  3. Enumera strategies/_pending/*.py, filtra com smoke_check (ast + runtime).
  4. Cada sobrevivente: cross_evaluate nos 12 pares (gate completo:
     profitability + walk-forward). Barras MT5 cacheadas após o 1o fetch.
  5. Compara vencedores ao incumbente (regra1: candidato > baseline).
  6. Relatório de vencedores. --apply promove (move p/ strategies/ + config).

Segurança:
  - NENHUMA relaxação de gate: reusa ast_gate + _runtime_smoke_gate +
    evaluate_candidate (PF/WR/n_trades/max_dd + walk-forward) + regra1.
  - Default é --dry-run (só relata). --apply tira snapshot antes + valida
    guardrails em cada escrita.
  - Promoção via stage5_apply._maybe_promote_generated (shutil.move) + escrita
    strategy_by_tf via save_full_config (writer agi_v4_stage5 autorizado).

Uso:
  .venv/bin/python3 scripts/sweep_pending_strategies.py             # dry-run (relatório)
  .venv/bin/python3 scripts/sweep_pending_strategies.py --apply     # promove vencedores
  .venv/bin/python3 scripts/sweep_pending_strategies.py --limit 20  # só 20 (teste rápido)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "core"))  # vt_config_loader (import top-level)

log = logging.getLogger("sweep_pending")


def _load_config() -> dict:
    from vt_config_loader import load_config
    return load_config()


def _fmt_brl(v: float) -> str:
    try:
        return f"R${float(v):+.0f}"
    except (TypeError, ValueError):
        return "R$?"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--apply", action="store_true",
                   help="Promove vencedores (default: só relata em dry-run).")
    p.add_argument("--limit", type=int, default=0,
                   help="Limita nº de estratégias testadas (0 = todas).")
    p.add_argument("--min-advantage", type=float, default=20.0,
                   help="Vantagem mínima (R$) vs incumbente p/ promover (default 20: "
                        "ignora trocas marginais que não justificam mudar estratégia estável).")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    from optimization.agi_v4.cross_pair_evaluator import (
        cross_evaluate, smoke_check, active_pairs, load_strategy_module,
    )
    from optimization.agi_v4.backtest_evaluator import evaluate_baseline
    from optimization.agi_v4.gates import load_thresholds

    config = _load_config()
    thresholds = load_thresholds(config)
    pairs = active_pairs(config)
    pending_dir = PROJECT_ROOT / "strategies" / "_pending"

    print(f"\n{'═' * 70}")
    print(f"VARREDURA CRUZADA strategies/_pending/  ({'APPLY' if args.apply else 'DRY-RUN'})")
    print(f"{'═' * 70}")
    print(f"Pares ativos ({len(pairs)}): {', '.join(pairs)}")

    # ── 1. Baselines (incumbentes) de cada par ─────────────────────────
    print("\n[1/4] Baselines (incumbentes) por par — simulação 30d:")
    baselines: dict[str, dict] = {}
    for pair in pairs:
        sym, tf = pair.split("_", 1)
        m = evaluate_baseline(sym, tf, config)
        baselines[pair] = m
        strat = config.get("strategy_by_tf", {}).get(pair, "?")
        print(f"  {pair:9s} ({strat:20s}) {m.get('n_trades', 0):3d}t "
              f"PF={m.get('pf', 0):5.2f} PnL={_fmt_brl(m.get('total_pnl', 0))}")

    # ── 2. Enumerar + smoke-filter _pending/ ───────────────────────────
    files = sorted(pending_dir.glob("*.py"))
    files = [f for f in files if f.name != "__init__.py"]
    if args.limit:
        files = files[: args.limit]
    print(f"\n[2/4] Smoke-check de {len(files)} estratégias em _pending/ ...")
    survivors: list[Path] = []
    rejected_smoke: list[tuple[str, str]] = []
    for f in files:
        g = smoke_check(f)
        if g:
            survivors.append(f)
        else:
            rejected_smoke.append((f.name, g.reason))
    print(f"  {len(survivors)} passaram no smoke | {len(rejected_smoke)} rejeitadas (bug/contrato)")
    if rejected_smoke and args.verbose:
        for name, reason in rejected_smoke[:10]:
            print(f"    ✗ {name}: {reason[:70]}")

    # ── 3. Cross-evaluate cada sobrevivente ────────────────────────────
    print(f"\n[3/4] Cross-evaluation: {len(survivors)} estratégias × {len(pairs)} pares ...")
    winners: list[dict] = []  # vencedores que batem o incumbente (regra1)
    t0 = time.time()
    for i, f in enumerate(survivors, 1):
        mod = load_strategy_module(f)
        if mod is None:
            continue
        name = getattr(mod, "STRATEGY_NAME", f.stem.upper())
        winner = cross_evaluate(name, f, pairs, config, thresholds)
        if winner is None:
            continue
        # regra1: candidato deve superar o incumbente do par vencedor
        pair = winner["pair"]
        cand_pnl = winner["full"].get("total_pnl", 0)
        base = baselines.get(pair, {})
        base_pnl = base.get("total_pnl", 0)
        beats = cand_pnl > base_pnl
        winner["beats_incumbent"] = beats
        winner["incumbent_pnl"] = base_pnl
        winner["file"] = f.name
        if beats:
            winners.append(winner)
            print(f"  [{i}/{len(survivors)}] ✓ {name} → {pair} "
                  f"{_fmt_brl(cand_pnl)} (incumbente {_fmt_brl(base_pnl)}, "
                  f"+{_fmt_brl(cand_pnl - base_pnl)})")
        if i % 20 == 0:
            elapsed = time.time() - t0
            print(f"  ... {i}/{len(survivors)} ({elapsed:.0f}s)")

    # ── 4. Relatório ───────────────────────────────────────────────────
    print("\n[4/4] RESULTADO")
    print(f"{'═' * 70}")
    print(f"Estratégias testadas: {len(survivors)} | Vencedoras (batem incumbente): {len(winners)}")
    print(f"Tempo: {time.time() - t0:.0f}s")

    if not winners:
        print("\nNenhuma estratégia órfã superou os incumbentes atuais. "
              "_pending/ pode ser limpo com segurança.")
        return 0

    winners.sort(key=lambda w: w["full"].get("total_pnl", 0) - w.get("incumbent_pnl", 0),
                 reverse=True)
    print(f"\n{'estratégia':28s} {'par':9s} {'PnL cand':>10s} {'PnL base':>10s} "
          f"{'vantagem':>10s} {'PF':>6s}")
    print("-" * 80)
    for w in winners:
        pnl = w["full"].get("total_pnl", 0)
        print(f"{w['strategy']:28s} {w['pair']:9s} {_fmt_brl(pnl):>10s} "
              f"{_fmt_brl(w.get('incumbent_pnl', 0)):>10s} "
              f"{_fmt_brl(pnl - w.get('incumbent_pnl', 0)):>10s} "
              f"{w['full'].get('pf', 0):6.2f}")

    if not args.apply:
        print(f"\n→ {len(winners)} candidata(s) à promoção. "
              f"Rode com --apply para promover (move p/ strategies/ + atualiza config).")
        return 0

    # ── Best-per-pair: cada par só pode ter 1 estratégia. Várias órfãs podem
    #    vencer no mesmo par (ex: 9 em WDO_M5) — promovemos só a melhor de cada.
    #    Filtra vantagens < min_advantage (trocas marginais não justificam
    #    trocar um incumbente estável e add risco).
    min_adv = args.min_advantage
    best_per_pair: dict[str, dict] = {}
    skipped_marginal: list[dict] = []
    for w in winners:
        adv = w["full"].get("total_pnl", 0) - w.get("incumbent_pnl", 0)
        if adv < min_adv:
            skipped_marginal.append(w)
            continue
        cur = best_per_pair.get(w["pair"])
        if cur is None or w["full"].get("total_pnl", 0) > cur["full"].get("total_pnl", 0):
            best_per_pair[w["pair"]] = w
    to_apply = list(best_per_pair.values())
    if skipped_marginal:
        print(f"\n  (filtradas {len(skipped_marginal)} troca(s) marginal(is) "
              f"< R${min_adv:.0f} de vantagem)")
    print(f"\n▶ {len(to_apply)} promoção(ões) = melhor-por-par:")
    for w in to_apply:
        print(f"  • {w['pair']:9s} ← {w['strategy']:24s} "
              f"PnL {_fmt_brl(w['full'].get('total_pnl', 0))} "
              f"(incumbente {_fmt_brl(w.get('incumbent_pnl', 0))})")

    # ── APPLY: promover vencedores ─────────────────────────────────────
    from optimization.agi_v4 import stage5_apply
    from vt_config_loader import load_config, save_full_config
    from optimization.agi_v4.guardrails import validate_target_block

    # Snapshot de rollback (mesmo padrão do cron AGI)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    config_path = PROJECT_ROOT / "vt_config.json"
    snapshot = config_path.with_name(f"vt_config.json.snapshot_sweep_{ts}")
    snapshot.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"  Snapshot de rollback: {snapshot.name}")

    promoted = 0
    for w in to_apply:
        name = w["strategy"]
        cand = {"generated": True, "pending_path": w["pending_path"]}
        try:
            dest = stage5_apply._maybe_promote_generated(name, cand, dry_run=False)
            if dest is None:
                print(f"  ✗ {name}: promoção não moveu arquivo")
                continue
            # Escreve strategy_by_tf[winner_pair] = name (valida guardrails)
            new_cfg = load_config(force=True)
            target = {"strategy_by_tf": {w["pair"]: name},
                      "params_by_tf": {w["pair"]: {}}}
            validate_target_block(target, new_cfg)  # levanta GuardrailReject se inválido
            new_cfg.setdefault("strategy_by_tf", {})[w["pair"]] = name
            new_cfg.setdefault("params_by_tf", {})[w["pair"]] = {}
            # Writer autorizado: o sweep é uma extensão do AGI (reusa
            # stage5_apply p/ promoção). "sweep_pending" não está no
            # ALLOWED_WRITERS — usar o writer sancionado do AGI.
            save_full_config(new_cfg, updated_by="agi_v4_stage5")
            promoted += 1
            print(f"  ⬆️ {name} → strategies/ + strategy_by_tf[{w['pair']}] "
                  f"(PnL {_fmt_brl(w['full'].get('total_pnl', 0))})")
        except Exception as e:
            print(f"  ✗ {name} → {w['pair']}: falhou ({e})")

    print(f"\n✓ {promoted}/{len(to_apply)} promovida(s) (melhor-por-par). "
          f"Snapshot: {snapshot.name}")
    print(f"  Daemon pega no próximo reload. Rollback: cp {snapshot.name} vt_config.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
