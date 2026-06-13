#!/usr/bin/env python3
"""
Vibe-Trading Tax Report — Gera relatório completo pro Imposto de Renda.

Uso:
    python vt_tax_report.py                     # Mês atual
    python vt_tax_report.py 06 2026              # Mês específico
    python vt_tax_report.py --csv                # Exporta CSV
    python vt_tax_report.py --send               # Envia pelo Telegram
    python vt_tax_report.py --year 2026         # Resumo anual
"""

import sys
import os
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from vt_trade_log import init_db, get_tax_report, export_csv
from mt5_orchestrator import _run_wine, EXECUTOR_WIN


def format_report(report: dict) -> str:
    """Formata relatório para Telegram."""
    p = report["period"]
    lines = [
        f"📋 *RELATÓRIO IR — {p}*",
        f"",
        f"📊 *Resumo:*",
        f"• Trades: {report['n_trades']}",
        f"• Acertos: {report['n_winners']} ({report['win_rate']}%)",
        f"• Erros: {report['n_losers']}",
        f"",
        f"💰 *Financeiro:*",
        f"• PnL Bruto: R$ {report['total_gross_pnl']:+,.2f}",
        f"• Taxas: R$ {report['total_fees']:,.2f}",
        f"• *PnL Líquido: R$ {report['total_net_pnl']:+,.2f}*",
        f"",
        f"🏛️ *Imposto de Renda:*",
        f"• Alíquota Day Trade: {report['ir_rate']}",
        f"• *IR Devido: R$ {report['ir_due']:,.2f}*",
        f"• Prejuízos anteriores: R$ {report['prev_losses_compensable']:,.2f}",
        f"• Compensado este mês: R$ {report['compensated_this_month']:,.2f}",
        f"• Prejuízo remanescente: R$ {report['remaining_loss_carry']:,.2f}",
    ]

    if report["daily_summary"]:
        lines.append(f"\n📅 *Diário:*")
        for d in report["daily_summary"]:
            pnl = d["net_pnl"]
            lines.append(
                f"• {d['date']} | {d['n_trades']} trades | "
                f"R$ {pnl:+,.2f}"
            )

    return "\n".join(lines)


def format_detail(report: dict) -> str:
    """Formata detalhamento dos trades."""
    lines = [f"\n📝 *DETALHES — {report['period']}*\n"]
    lines.append(f"{'#':>3} {'Ativo':<8} {'Dir':<4} {'Qtd':>3} {'PnL Bruto':>12} {'Taxas':>8} {'PnL Líq':>12} {'Motivo'}")
    lines.append("─" * 75)

    for t in report["trades"]:
        lines.append(
            f"{t['id']:>3} {t['symbol']:<8} {t['direction']:<4} {t['volume']:>3.0f} "
            f"R$ {t['gross_pnl']:>+9,.2f} R$ {t['fees']:>6,.2f} R$ {t['net_pnl']:>+9,.2f} {t['exit_reason']}"
        )

    return "\n".join(lines)


def import_mt5_deals():
    """Importa deals do MT5 e os registra no log."""
    print("Importando deals do MT5...")
    result = _run_wine(EXECUTOR_WIN, "history", "", "30")
    print(result.get("info", result.get("error", "resultado vazio")))

    if "history" in result and result["history"]:
        from vt_trade_log import import_mt5_history
        import_mt5_history(result["history"])
        print(f"{len(result['history'])} deals importados")


def main():
    init_db()

    # Argumentos
    month = None
    year = None
    do_csv = False
    do_send = False
    do_year = False
    do_import = False

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--csv":
            do_csv = True
        elif args[i] == "--send":
            do_send = True
        elif args[i] == "--year":
            do_year = True
            year = int(args[i + 1]) if i + 1 < len(args) else datetime.now().year
            i += 1
        elif args[i] == "--import":
            do_import = True
        elif not args[i].startswith("--"):
            month = int(args[i])
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                year = int(args[i + 1])
                i += 1
        i += 1

    if do_import:
        import_mt5_deals()
        return

    if do_year:
        # Relatório anual
        year = year or datetime.now().year
        print(f"=== RELATÓRIO ANUAL {year} ===\n")
        total_net = 0
        total_ir = 0
        for m in range(1, 13):
            report = get_tax_report(m, year)
            if report["n_trades"] > 0:
                total_net += report["total_net_pnl"]
                total_ir += report["ir_due"]
                print(f"  {m:02d}/{year}: {report['n_trades']:>3} trades | "
                      f"R$ {report['total_net_pnl']:>+10,.2f} | IR R$ {report['ir_due']:>8,.2f}")
        print(f"\n  TOTAL:       R$ {total_net:>+10,.2f}")
        print(f"  IR TOTAL:    R$ {total_ir:>10,.2f}")
        return

    report = get_tax_report(month, year)
    print(format_report(report))
    print(format_detail(report))

    if do_csv:
        csv_path = export_csv(month, year)
        print(f"\n📄 CSV exportado: {csv_path}")

    if do_send:
        msg = format_report(report) + format_detail(report)
        from vt_hermes_helper import hermes_send
        hermes_send("telegram:-1004284773048", msg, timeout=15)
        print("✅ Relatório enviado ao Telegram")


if __name__ == "__main__":
    main()
