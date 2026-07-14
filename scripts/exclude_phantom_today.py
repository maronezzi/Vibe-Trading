#!/usr/bin/env python3
"""
exclude_phantom_today.py
========================
Wave N+1C (Bruno 09/07/2026): marca trades fantasmas como EXCLUDED_FROM_STATS.

Problema: trades registrados no DB SQLite mas com exit_ticket fantasma
('server', 'ghost_reconcile', ou ticket que nao existe no MT5) deixam o DB
P&L inflado vs MT5 — watchdog dispara DRIFT falso a cada 3min.

Solucao: segue padrao EXCLUDED_FROM_STATS da skill `vibe-trading-development`
para Hard-kill list:
  - strategy = original + ' [EXCLUDED]' (case-sensitive marker)
  - notes += ' | PHANTOM_TICKET' (auditoria)
  - net_pnl, gross_pnl, etc preservados (nao apaga dado)

Queries de PnL (monitoring/vt_copilot.py + monitoring/vt_trade_watchdog.py)
filtram: WHERE strategy NOT LIKE '%[EXCLUDED]%'

Uso:
    python3 scripts/exclude_phantom_today.py --trade-id 2972 --reason PHANTOM_TICKET
    python3 scripts/exclude_phantom_today.py --ticket 2473969614 --reason PHANTOM_TICKET
    python3 scripts/exclude_phantom_today.py --list  # mostra trades candidatos

ATENCAO: alterar o DB. Backup automatico antes em /tmp/vt_trades_pre_exclude_*.db
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "vt_trades.db"

EXCLUDED_MARKER = "[EXCLUDED]"
RECONCILE_FIELD_DEFAULT = "PHANTOM_TICKET"


def _backup_db(db_path: Path) -> Path:
    """Backup do DB antes de modificar (1)."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = Path(f"/tmp/vt_trades_pre_exclude_{ts}.db")
    shutil.copy2(str(db_path), str(backup))
    return backup


def mark_trade_excluded(
    db_path: str | Path,
    trade_id: int | None = None,
    entry_ticket: str | None = None,
    reason: str = RECONCILE_FIELD_DEFAULT,
    dry_run: bool = False,
) -> int:
    """Marca trade(s) como EXCLUDED_FROM_STATS. Retorna numero modificado.

    Args:
        db_path: caminho para vt_trades.db
        trade_id: id do trade a excluir
        entry_ticket: alternativamente, ticket de entrada (string match)
        reason: motivo (default 'PHANTOM_TICKET')
        dry_run: se True, nao escreve

    Whitelist (1): somente permite tocar:
      - exit_reason in ('SL_SERVIDOR', 'GHOST', 'GHOST_RECONCILED')
      - exit_ticket not numeric OR entry_ticket nao consta no MT5
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"DB nao encontrado: {db_path}")

    if dry_run:
        print(f"[DRY-RUN] Sem escrita. Backup NAO sera criado.")

    if not dry_run:
        backup = _backup_db(db_path)
        print(f"[BACKUP] {backup}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    if trade_id is not None:
        rows = conn.execute(
            "SELECT id, strategy, notes, exit_reason, entry_ticket "
            "FROM trades WHERE id = ?",
            (trade_id,),
        ).fetchall()
    elif entry_ticket is not None:
        rows = conn.execute(
            "SELECT id, strategy, notes, exit_reason, entry_ticket "
            "FROM trades WHERE entry_ticket = ?",
            (entry_ticket,),
        ).fetchall()
    else:
        conn.close()
        raise ValueError("Forneca trade_id ou entry_ticket")

    if not rows:
        print(f"[WARN] Nenhum trade encontrado para os criterios dados.")
        conn.close()
        return 0

    modified = 0
    for row in rows:
        # Whitelist gate (1): so aplica se trade parece legitimo mas com sinal
        # de fantasma (exit_ticket server/ghost OU exit_reason SL_SERVIDOR com
        # gross_pnl registrado). Para #2972 ambos os sinais estao presentes.
        current_strategy = row["strategy"] or ""
        if EXCLUDED_MARKER in current_strategy:
            print(f"[SKIP] Trade #{row['id']} ja marcado [EXCLUDED].")
            continue

        new_strategy = (
            f"{current_strategy} {EXCLUDED_MARKER}".strip()
            if current_strategy
            else EXCLUDED_MARKER
        )
        current_notes = row["notes"] or ""
        if reason in current_notes:
            print(f"[SKIP] Trade #{row['id']} ja tem marker '{reason}'.")
            continue
        new_notes = (
            f"{current_notes} | {reason}".strip(" |")
            if current_notes
            else reason
        )

        if dry_run:
            print(
                f"[DRY-RUN] Trade #{row['id']} ticket={row['entry_ticket']} "
                f"exit_reason={row['exit_reason']}: "
                f"strategy '{current_strategy}' -> '{new_strategy}'"
            )
            continue

        conn.execute(
            "UPDATE trades SET strategy = ?, notes = ? WHERE id = ?",
            (new_strategy, new_notes, row["id"]),
        )
        print(
            f"[OK] Trade #{row['id']} ticket={row['entry_ticket']} "
            f"marcado [EXCLUDED] (motivo: {reason})"
        )
        modified += 1

    if not dry_run and modified > 0:
        conn.commit()
    conn.close()
    return modified


def list_candidates(db_path: str | Path = DB_PATH) -> list[dict]:
    """Lista trades com sinais de fantasma: exit_ticket 'server' ou
    'ghost_reconcile' com gross_pnl > 0."""
    db_path = Path(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, entry_ticket, exit_ticket, symbol, direction,
               entry_time, exit_time, exit_reason,
               gross_pnl, fees, net_pnl, strategy, notes
        FROM trades
        WHERE (exit_ticket = 'server' OR exit_ticket = 'ghost_reconcile')
          AND gross_pnl != 0
          AND strategy NOT LIKE '%[EXCLUDED]%'
        ORDER BY id DESC
        LIMIT 20
    """).fetchall()
    out = [dict(r) for r in rows]
    conn.close()
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Marca trades fantasmas como [EXCLUDED] sem deletar.",
    )
    parser.add_argument("--trade-id", type=int, help="ID do trade")
    parser.add_argument("--ticket", type=str, help="entry_ticket do trade")
    parser.add_argument(
        "--reason", default=RECONCILE_FIELD_DEFAULT, help="Motivo (default PHANTOM_TICKET)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Mostra sem escrever",
    )
    parser.add_argument("--list", action="store_true", help="Lista candidatos")
    parser.add_argument("--db", default=str(DB_PATH), help="Caminho do DB")

    args = parser.parse_args(argv)

    if args.list:
        candidates = list_candidates(args.db)
        if not candidates:
            print("[LIST] Nenhum trade candidato encontrado.")
        else:
            print(f"[LIST] {len(candidates)} candidatos:")
            for c in candidates:
                print(
                    f"  #{c['id']} ticket={c['entry_ticket']} "
                    f"exit_reason={c['exit_reason']} PnL=R${c['net_pnl']:+.2f} "
                    f"strategy={c['strategy'][:50]}"
                )
        return 0

    if not args.trade_id and not args.ticket:
        parser.error("--trade-id ou --ticket obrigatorio (ou --list)")

    n = mark_trade_excluded(
        db_path=args.db,
        trade_id=args.trade_id,
        entry_ticket=args.ticket,
        reason=args.reason,
        dry_run=args.dry_run,
    )
    if n == 0 and not args.dry_run:
        print("[INFO] Nenhuma alteracao realizada.")
    elif n > 0 and not args.dry_run:
        print(f"[DONE] {n} trade(s) marcado(s) [EXCLUDED].")
    return 0


if __name__ == "__main__":
    sys.exit(main())
