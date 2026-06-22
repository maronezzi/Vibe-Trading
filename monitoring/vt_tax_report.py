#!/usr/bin/env python3
"""
vt_tax_report.py — Relatório de Imposto de Renda para Day Trade.

Gera relatório mensal com:
- Todos os trades fechados no mês
- PnL bruto, taxas, líquido
- Compensação de prejuízos anteriores
- IR devido (20% day trade)
- Export CSV para a Receita Federal

Uso:
    python3 vt_tax_report.py                    # Mês atual
    python3 vt_tax_report.py --month 6 --year 2026
    python3 vt_tax_report.py --export-csv        # Exporta CSV
    python3 vt_tax_report.py --all-months        # Todos os meses
"""

import sys
import json
import sqlite3
import csv
from datetime import datetime, date
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "vt_trades.db"
CSV_DIR = Path(__file__).parent.parent / "reports"


def get_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def get_tax_report(month=None, year=None):
    """Gera relatório de IR para um mês específico."""
    if month is None:
        month = datetime.now().month
    if year is None:
        year = datetime.now().year

    conn = get_db()
    month_str = f"{year:04d}-{month:02d}"

    # Trades fechados no mês
    rows = conn.execute("""
        SELECT id, symbol, direction, volume, timeframe, strategy,
               entry_time, exit_time, entry_price, exit_price,
               exit_reason, gross_pnl, fees, swap, net_pnl,
               is_day_trade, signal_detail, notes
        FROM trades
        WHERE exit_time IS NOT NULL
        AND strftime('%Y-%m', exit_time) = ?
        ORDER BY exit_time ASC
    """, (month_str,)).fetchall()

    trades = []
    total_gross = 0
    total_fees = 0
    total_swap = 0
    total_net = 0
    total_wins = 0
    total_losses = 0

    for r in rows:
        gross = r["gross_pnl"] or 0
        fees = r["fees"] or 0
        swap = r["swap"] or 0
        net = r["net_pnl"] or 0
        total_gross += gross
        total_fees += fees
        total_swap += swap
        total_net += net
        if net > 0:
            total_wins += 1
        else:
            total_losses += 1

        trades.append({
            "id": r["id"],
            "symbol": r["symbol"],
            "direction": r["direction"],
            "volume": r["volume"],
            "timeframe": r["timeframe"],
            "strategy": r["strategy"],
            "entry_time": r["entry_time"],
            "exit_time": r["exit_time"],
            "entry_price": r["entry_price"],
            "exit_price": r["exit_price"],
            "exit_reason": r["exit_reason"],
            "gross_pnl": round(gross, 2),
            "fees": round(fees, 2),
            "swap": round(swap, 2),
            "net_pnl": round(net, 2),
            "is_day_trade": r["is_day_trade"],
        })

    # Prejuízos acumulados de meses anteriores
    prev_losses = conn.execute("""
        SELECT COALESCE(SUM(CASE WHEN net_pnl < 0 THEN net_pnl ELSE 0 END), 0) as total
        FROM trades
        WHERE exit_time IS NOT NULL
        AND strftime('%Y-%m', exit_time) < ?
    """, (month_str,)).fetchone()["total"]

    # Resumo diário do mês
    daily = conn.execute("""
        SELECT date, symbol, n_trades, n_winners, n_losers,
               gross_pnl, fees, net_pnl, max_win, max_loss
        FROM daily_summary
        WHERE strftime('%Y-%m', date) = ?
        ORDER BY date ASC
    """, (month_str,)).fetchall()

    # Por símbolo
    by_symbol = {}
    for t in trades:
        sym = t["symbol"]
        if sym not in by_symbol:
            by_symbol[sym] = {"trades": 0, "wins": 0, "losses": 0, "gross": 0, "fees": 0, "net": 0}
        by_symbol[sym]["trades"] += 1
        by_symbol[sym]["gross"] += t["gross_pnl"]
        by_symbol[sym]["fees"] += t["fees"]
        by_symbol[sym]["net"] += t["net_pnl"]
        if t["net_pnl"] > 0:
            by_symbol[sym]["wins"] += 1
        else:
            by_symbol[sym]["losses"] += 1

    # Arredondar
    for sym in by_symbol:
        for k in ["gross", "fees", "net"]:
            by_symbol[sym][k] = round(by_symbol[sym][k], 2)

    conn.close()

    # Cálculo IR
    ir_rate = 0.20  # 20% para day trade
    compensable_loss = min(abs(prev_losses), total_net) if total_net > 0 else 0
    ir_due = max(0, (total_net - compensable_loss) * ir_rate) if total_net > 0 else 0
    remaining_loss = abs(prev_losses) - compensable_loss

    return {
        "month": month_str,
        "month_name": _month_name(month),
        "year": year,
        "month_num": month,
        "trades": trades,
        "by_symbol": by_symbol,
        "daily": [dict(d) for d in daily],
        "summary": {
            "total_trades": len(trades),
            "wins": total_wins,
            "losses": total_losses,
            "win_rate": round(total_wins / max(len(trades), 1) * 100, 1),
            "gross_pnl": round(total_gross, 2),
            "total_fees": round(total_fees, 2),
            "total_swap": round(total_swap, 2),
            "net_pnl": round(total_net, 2),
        },
        "ir": {
            "rate": ir_rate,
            "prev_losses": round(prev_losses, 2),
            "compensable_loss": round(compensable_loss, 2),
            "ir_base": round(total_net - compensable_loss, 2),
            "ir_due": round(ir_due, 2),
            "remaining_loss": round(remaining_loss, 2),
        },
    }


def _month_name(month):
    names = {1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril",
             5: "Maio", 6: "Junho", 7: "Julho", 8: "Agosto",
             9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro"}
    return names.get(month, str(month))


def format_tax_report(report):
    """Formata relatório de IR para texto."""
    s = report["summary"]
    ir = report["ir"]
    lines = [
        f"📋 *RELATÓRIO DE IMPOSTO DE RENDA*",
        f"📅 {report['month_name']} {report['year']}",
        "─" * 30,
        "",
        f"📊 *Resumo do Mês*",
        f"• Trades: {s['total_trades']} ({s['wins']}W / {s['losses']}L | WR {s['win_rate']:.0f}%)",
        f"• PnL Bruto: R$ {s['gross_pnl']:+,.2f}",
        f"• Taxas (fees): R$ {s['total_fees']:,.2f}",
        f"• Swap: R$ {s['total_swap']:+,.2f}",
        f"• *PnL Líquido: R$ {s['net_pnl']:+,.2f}*",
        "",
    ]

    # Por símbolo
    if report["by_symbol"]:
        lines.append("📈 *Por Símbolo*")
        for sym, data in sorted(report["by_symbol"].items()):
            wr = (data["wins"] / data["trades"] * 100) if data["trades"] > 0 else 0
            icon = "🟢" if data["net"] > 0 else "🔴"
            lines.append(f"{icon} {sym}: {data['trades']}t ({wr:.0f}% WR) | R$ {data['net']:+,.2f}")
        lines.append("")

    # IR
    lines.extend([
        "🏦 *Cálculo do IR (Day Trade — 20%)*",
        f"• Base: R$ {s['net_pnl']:+,.2f}",
        f"• Prejuízos anteriores: R$ {ir['prev_losses']:+,.2f}",
        f"• Compensável: R$ {ir['compensable_loss']:,.2f}",
        f"• Base líquida: R$ {ir['ir_base']:+,.2f}",
        f"• *IR devido: R$ {ir['ir_due']:+,.2f}*",
    ])

    if ir["remaining_loss"] > 0:
        lines.append(f"• Prejuízo restante p/ compensar: R$ {ir['remaining_loss']:,.2f}")

    lines.extend([
        "",
        "─" * 30,
        f"🤖 Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}",
    ])

    return "\n".join(lines)


def export_csv(report, output_dir=None):
    """Exporta trades do mês para CSV (formato Receita Federal)."""
    if output_dir is None:
        output_dir = CSV_DIR
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    month_str = report["month"]
    csv_path = output_dir / f"trades_{month_str}.csv"

    fieldnames = [
        "Data Entrada", "Data Saída", "Ativo", "Direção", "Volume",
        "Timeframe", "Estratégia", "Preço Entrada", "Preço Saída",
        "Motivo Saída", "PnL Bruto", "Taxas", "Swap", "PnL Líquido",
        "Day Trade"
    ]

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        for t in report["trades"]:
            writer.writerow({
                "Data Entrada": t["entry_time"],
                "Data Saída": t["exit_time"],
                "Ativo": t["symbol"],
                "Direção": t["direction"],
                "Volume": t["volume"],
                "Timeframe": t["timeframe"],
                "Estratégia": t["strategy"],
                "Preço Entrada": t["entry_price"],
                "Preço Saída": t["exit_price"],
                "Motivo Saída": t["exit_reason"],
                "PnL Bruto": f"{t['gross_pnl']:.2f}".replace(".", ","),
                "Taxas": f"{t['fees']:.2f}".replace(".", ","),
                "Swap": f"{t['swap']:.2f}".replace(".", ","),
                "PnL Líquido": f"{t['net_pnl']:.2f}".replace(".", ","),
                "Day Trade": "Sim" if t["is_day_trade"] else "Não",
            })

    # Resumo
    summary_path = output_dir / f"resumo_{month_str}.csv"
    with open(summary_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["Métrica", "Valor"])
        writer.writerow(["Mês", report["month_name"] + " " + str(report["year"])])
        writer.writerow(["Total Trades", report["summary"]["total_trades"]])
        writer.writerow(["Wins", report["summary"]["wins"]])
        writer.writerow(["Losses", report["summary"]["losses"]])
        writer.writerow(["Win Rate", f"{report['summary']['win_rate']:.1f}%"])
        writer.writerow(["PnL Bruto", f"R$ {report['summary']['gross_pnl']:.2f}"])
        writer.writerow(["Taxas", f"R$ {report['summary']['total_fees']:.2f}"])
        writer.writerow(["PnL Líquido", f"R$ {report['summary']['net_pnl']:.2f}"])
        writer.writerow(["Prejuízos Anteriores", f"R$ {report['ir']['prev_losses']:.2f}"])
        writer.writerow(["IR Devido (20%)", f"R$ {report['ir']['ir_due']:.2f}"])

    return str(csv_path), str(summary_path)


def get_all_months():
    """Lista todos os meses com trades."""
    conn = get_db()
    months = conn.execute("""
        SELECT DISTINCT strftime('%Y-%m', exit_time) as month
        FROM trades
        WHERE exit_time IS NOT NULL
        ORDER BY month ASC
    """).fetchall()
    conn.close()
    return [m["month"] for m in months]


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Relatório de IR - Vibe-Trading")
    parser.add_argument("--month", type=int, default=None)
    parser.add_argument("--year", type=int, default=None)
    parser.add_argument("--export-csv", action="store_true")
    parser.add_argument("--all-months", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.all_months:
        months = get_all_months()
        if not months:
            print("Nenhum trade registrado no banco.")
            return

        total_ir = 0
        for m_str in months:
            y, m = int(m_str[:4]), int(m_str[5:7])
            report = get_tax_report(m, y)
            print(format_tax_report(report))
            print()
            if args.export_csv:
                csv_path, summary_path = export_csv(report)
                print(f"  CSV: {csv_path}")
                print(f"  Resumo: {summary_path}")
                print()
            total_ir += report["ir"]["ir_due"]

        if len(months) > 1:
            print(f"\n{'='*40}")
            print(f"💰 *IR TOTAL ACUMULADO: R$ {total_ir:+,.2f}*")
        return

    report = get_tax_report(args.month, args.year)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print(format_tax_report(report))

    if args.export_csv:
        csv_path, summary_path = export_csv(report)
        print(f"\n📄 CSV exportado: {csv_path}")
        print(f"📄 Resumo: {summary_path}")


if __name__ == "__main__":
    main()
