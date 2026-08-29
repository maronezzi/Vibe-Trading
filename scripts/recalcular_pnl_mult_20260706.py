#!/usr/bin/env python3
"""
W872 (2026-07-06) — Recalcula PnL histórico do DB com multipliers calibrados.

PROBLEMA: o DB tinha multipliers errados (WIN=0.20 mas real=1.0, WDO=10.0 mas
real=0.0015, etc.). Isso fazia o gross_pnl/net_pnl histórico ser calculado
com magnitude errada — WIN subestimava perdas 5x, WDO superestimava 6666x.

SOLUÇÃO: para cada trade fechado, recalcula multiplier/gross_pnl/net_pnl
usando os valores calibrados por broker-truth MT5. Preferência: se o trade
tem broker-truth em notes ("PnL real: R$X"), usa X como net_pnl autoritativo.

Multipliers calibrados (R$ por ponto de preço nativo):
  WIN=1.0, WDO=0.0015, BIT=0.01, WSP=0.01, DOL=0.0018, IND=1.0

Prova empírica: WIN trade 1318 (move=1700pts, broker=+R$1700) → mult=1.0 ✓

USO:
    python3 scripts/recalcular_pnl_mult_20260706.py           # dry-run (só mostra)
    python3 scripts/recalcular_pnl_mult_20260706.py --apply    # aplica no DB

NÃO mexe em vt_config.json (só no vt_trades.db). Backup já feito
(vt_trades.db.bak.20260706_mult) antes de rodar com --apply.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "vt_trades.db"

# Multipliers calibrados por broker-truth MT5 (W872, 2026-07-06).
# Mult = R$ por ponto de preço nativo (unidade do código).
MULT_BY_ROOT = {
    "WIN": 1.0,
    "WDO": 0.0015,
    "BIT": 0.01,
    "WSP": 0.01,
    "DOL": 0.0018,
    "IND": 1.0,
}


def _root_of(symbol: str) -> str:
    """Extrai raiz do símbolo (WINQ26 → WIN, WDOU26 → WDO)."""
    for root in sorted(MULT_BY_ROOT, key=len, reverse=True):
        if root in symbol:
            return root
    return ""


def _mult_for(symbol: str) -> float | None:
    root = _root_of(symbol)
    return MULT_BY_ROOT.get(root)


def _parse_broker_truth(notes: str) -> float | None:
    """Extrai PnL real do broker de notes. Retorna None se não houver."""
    if not notes:
        return None
    # Padrões: "PnL real: R$-400.00" ou "PnL estimado: R$-5.00"
    m = re.search(r"PnL (?:real|estimado): R\$([+-]?\d+\.?\d*)", notes)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _is_real_trade(exit_reason: str | None) -> bool:
    """True se o trade é real (não fantasma/ghost/excluded)."""
    if not exit_reason:
        return False
    if exit_reason in ("GHOST", "ORPHAN_MANUAL_CLOSE"):
        return False
    if exit_reason.startswith("FANTASMA"):
        return False
    if "EXCLUDED" in exit_reason:
        return False
    return True


def recalc(dry_run: bool = True) -> dict:
    """Recalcura PnL histórico. Retorna stats."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT id, symbol, direction, entry_price, exit_price, multiplier,
               volume, gross_pnl, net_pnl, fees, swap, notes, exit_reason
        FROM trades
        WHERE exit_time IS NOT NULL
    """).fetchall()

    stats = {
        "total": len(rows),
        "real_trades": 0,
        "updated": 0,
        "broker_truth_used": 0,
        "calc_used": 0,
        "skipped_ghost": 0,
        "skipped_no_change": 0,
        "by_root": {},
    }
    updates = []  # (id, new_mult, new_gross, new_net)

    for r in rows:
        sym = r["symbol"]
        root = _root_of(sym)
        stats["by_root"][root] = stats["by_root"].get(root, 0) + 1

        if not _is_real_trade(r["exit_reason"]):
            stats["skipped_ghost"] += 1
            continue

        new_mult = _mult_for(sym)
        if new_mult is None:
            continue

        stats["real_trades"] += 1

        # Calcular gross_pts (movimento em pontos de preço nativo)
        if r["direction"] == "BUY":
            gross_pts = (r["exit_price"] or 0) - (r["entry_price"] or 0)
        else:
            gross_pts = (r["entry_price"] or 0) - (r["exit_price"] or 0)

        volume = r["volume"] or 1.0
        fees = r["fees"] or 0.0
        swap = r["swap"] or 0.0

        # Preferência 1: broker-truth autoritativo
        broker_pnl = _parse_broker_truth(r["notes"] or "")
        if broker_pnl is not None and abs(broker_pnl) > 0.001:
            new_net = broker_pnl
            new_gross = new_net + fees - swap  # inverter net = gross - fees + swap
            stats["broker_truth_used"] += 1
        else:
            # Preferência 2: cálculo com mult calibrado
            new_gross = gross_pts * new_mult * volume
            new_net = new_gross - fees + swap
            stats["calc_used"] += 1

        # Só atualizar se mudou significativamente
        old_net = r["net_pnl"] or 0
        old_gross = r["gross_pnl"] or 0
        if (abs(new_net - old_net) < 0.01 and abs(new_gross - old_gross) < 0.01
                and abs(new_mult - (r["multiplier"] or 0)) < 0.0001):
            stats["skipped_no_change"] += 1
            continue

        updates.append((new_net, new_gross, new_mult, r["id"]))
        stats["updated"] += 1

    if not dry_run and updates:
        conn.executemany(
            "UPDATE trades SET net_pnl=?, gross_pnl=?, multiplier=? WHERE id=?",
            updates,
        )
        conn.commit()

    conn.close()
    return stats


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--apply", action="store_true",
                   help="Aplica mudanças no DB (default: dry-run)")
    args = p.parse_args()

    print(f"{'═' * 70}")
    print(f"W872 — Recálculo de PnL histórico (multipliers calibrados)")
    print(f"{'═' * 70}")
    print(f"Modo: {'APLICAR' if args.apply else 'DRY-RUN (não aplica)'}")
    print()

    stats = recalc(dry_run=not args.apply)

    print(f"Total trades analisados:  {stats['total']}")
    print(f"Trades reais (não-ghost): {stats['real_trades']}")
    print(f"  - com broker-truth:     {stats['broker_truth_used']}")
    print(f"  - cálculo local:        {stats['calc_used']}")
    print(f"Skipped (ghost/excluded): {stats['skipped_ghost']}")
    print(f"Skipped (sem mudança):    {stats['skipped_no_change']}")
    print(f"ATUALIZADOS:              {stats['updated']}")
    print()
    print("Distribuição por root:")
    for root, n in sorted(stats["by_root"].items()):
        mult = MULT_BY_ROOT.get(root, "?")
        print(f"  {root or '?':>5s}: {n:>4d} trades (mult={mult})")

    if stats["updated"] == 0:
        print()
        print(">>> Nenhuma mudança necessária — PnL já está correto.")
    elif not args.apply:
        print()
        print(f">>> DRY-RUN: {stats['updated']} trades seriam atualizados.")
        print(">>> Rode com --apply para persistir.")
    else:
        print()
        print(f">>> APLICADO: {stats['updated']} trades atualizados no DB.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
