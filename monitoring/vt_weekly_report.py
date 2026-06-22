#!/usr/bin/env python3
"""
vt_weekly_report.py — Relatório semanal do Vibe-Trading.

Gera relatório completo da semana (segunda a sexta) com:
- Resumo geral (PnL, WR, trades)
- PnL diário (gráfico de barras)
- PnL por ativo (gráfico de barras)
- PnL por estratégia (gráfico de barras)
- Equity curve
- Top 5 melhores e piores trades
- Análise de performance por ativo/timeframe

Uso:
    python3 vt_weekly_report.py                  # Semana atual
    python3 vt_weekly_report.py --prev           # Semana anterior
    python3 vt_weekly_report.py --export-csv     # Exporta CSV
"""

import sys
import json
import sqlite3
import csv
import os
from datetime import datetime, date, timedelta
from pathlib import Path
from collections import defaultdict

DB_PATH = Path(__file__).parent.parent / "vt_trades.db"
REPORTS_DIR = Path(__file__).parent.parent / "reports"
TELEGRAM_GROUP = "-1004284773048"

# Matplotlib com Agg backend (sem display)
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import FancyBboxPatch
import numpy as np

# Dark theme
plt.rcParams.update({
    'figure.facecolor': '#0f0f23',
    'axes.facecolor': '#1a1a3a',
    'axes.edgecolor': '#333366',
    'axes.labelcolor': '#e0e0ff',
    'text.color': '#e0e0ff',
    'xtick.color': '#8888aa',
    'ytick.color': '#8888aa',
    'grid.color': '#222244',
    'grid.alpha': 0.5,
    'font.size': 10,
    'axes.titlesize': 13,
    'axes.titleweight': 'bold',
})


def get_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def get_week_range(target_date=None, prev=False):
    """Retorna (monday, friday) da semana do target_date."""
    if target_date is None:
        target_date = date.today()
    if prev:
        target_date -= timedelta(days=7)
    # Encontrar segunda-feira
    monday = target_date - timedelta(days=target_date.weekday())
    friday = monday + timedelta(days=4)
    return monday, friday


def get_weekly_trades(monday, friday):
    """Busca todos os trades da semana."""
    conn = get_db()
    trades = conn.execute("""
        SELECT id, symbol, direction, volume, timeframe, strategy,
               entry_time, exit_time, entry_price, exit_price,
               exit_reason, gross_pnl, fees, swap, net_pnl,
               signal_detail, notes
        FROM trades
        WHERE date(entry_time) >= ? AND date(entry_time) <= ?
        ORDER BY entry_time ASC
    """, (monday.isoformat(), friday.isoformat())).fetchall()

    daily = conn.execute("""
        SELECT date, symbol, n_trades, n_winners, n_losers,
               gross_pnl, fees, net_pnl, max_win, max_loss
        FROM daily_summary
        WHERE date >= ? AND date <= ?
        ORDER BY date ASC
    """, (monday.isoformat(), friday.isoformat())).fetchall()

    conn.close()
    return [dict(t) for t in trades], [dict(d) for d in daily]


def analyze_week(trades, daily):
    """Analisa dados da semana e retorna métricas."""
    if not trades:
        return None

    total = len(trades)
    closed = [t for t in trades if t["exit_time"] is not None]
    wins = [t for t in closed if (t["net_pnl"] or 0) > 0]
    losses = [t for t in closed if (t["net_pnl"] or 0) <= 0]

    total_pnl = sum(t["net_pnl"] or 0 for t in closed)
    total_gross = sum(t["gross_pnl"] or 0 for t in closed)
    total_fees = sum(t["fees"] or 0 for t in closed)
    total_swap = sum(t["swap"] or 0 for t in closed)

    # Por dia
    by_day = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0})
    for t in closed:
        day = t["entry_time"][:10] if t["entry_time"] else "?"
        by_day[day]["trades"] += 1
        by_day[day]["pnl"] += t["net_pnl"] or 0
        if (t["net_pnl"] or 0) > 0:
            by_day[day]["wins"] += 1

    # Por símbolo
    by_symbol = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0, "gross": 0, "fees": 0})
    for t in closed:
        sym = t["symbol"]
        by_symbol[sym]["trades"] += 1
        by_symbol[sym]["pnl"] += t["net_pnl"] or 0
        by_symbol[sym]["gross"] += t["gross_pnl"] or 0
        by_symbol[sym]["fees"] += t["fees"] or 0
        if (t["net_pnl"] or 0) > 0:
            by_symbol[sym]["wins"] += 1

    # Por estratégia
    by_strategy = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0})
    for t in closed:
        strat = t["strategy"] or "UNKNOWN"
        by_strategy[strat]["trades"] += 1
        by_strategy[strat]["pnl"] += t["net_pnl"] or 0
        if (t["net_pnl"] or 0) > 0:
            by_strategy[strat]["wins"] += 1

    # Por timeframe
    by_tf = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0})
    for t in closed:
        tf = t["timeframe"] or "?"
        by_tf[tf]["trades"] += 1
        by_tf[tf]["pnl"] += t["net_pnl"] or 0
        if (t["net_pnl"] or 0) > 0:
            by_tf[tf]["wins"] += 1

    # Por exit_reason
    by_reason = defaultdict(lambda: {"count": 0, "pnl": 0})
    for t in closed:
        reason = t["exit_reason"] or "UNKNOWN"
        by_reason[reason]["count"] += 1
        by_reason[reason]["pnl"] += t["net_pnl"] or 0

    # Top trades
    sorted_trades = sorted(closed, key=lambda x: x["net_pnl"] or 0)
    best5 = sorted_trades[-5:][::-1]
    worst5 = sorted_trades[:5]

    # Equity curve
    equity = []
    cumulative = 0
    for t in closed:
        cumulative += t["net_pnl"] or 0
        equity.append({
            "time": t["exit_time"],
            "pnl": t["net_pnl"] or 0,
            "cumulative": cumulative,
            "symbol": t["symbol"],
        })

    # Drawdown
    peak = 0
    max_dd = 0
    for e in equity:
        if e["cumulative"] > peak:
            peak = e["cumulative"]
        dd = peak - e["cumulative"]
        if dd > max_dd:
            max_dd = dd

    return {
        "total_trades": total,
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / max(len(closed), 1) * 100, 1),
        "total_pnl": round(total_pnl, 2),
        "total_gross": round(total_gross, 2),
        "total_fees": round(total_fees, 2),
        "total_swap": round(total_swap, 2),
        "best_trade": round(max((t["net_pnl"] or 0 for t in closed), default=0), 2),
        "worst_trade": round(min((t["net_pnl"] or 0 for t in closed), default=0), 2),
        "avg_trade": round(total_pnl / max(len(closed), 1), 2),
        "max_drawdown": round(max_dd, 2),
        "by_day": dict(by_day),
        "by_symbol": dict(by_symbol),
        "by_strategy": dict(by_strategy),
        "by_tf": dict(by_tf),
        "by_reason": dict(by_reason),
        "best5": best5,
        "worst5": worst5,
        "equity": equity,
        "trades": closed,
    }


def generate_charts(analysis, monday, friday):
    """Gera gráficos da semana. Retorna lista de paths de PNGs."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    week_str = f"{monday.strftime('%d%m')}_{friday.strftime('%d%m')}"
    charts = []

    # === 1. PnL Diário (barras) ===
    fig, ax = plt.subplots(figsize=(10, 5))
    days = sorted(analysis["by_day"].keys())
    pnls = [analysis["by_day"][d]["pnl"] for d in days]
    day_labels = [d[5:] for d in days]  # MM-DD
    colors = ['#00e676' if p >= 0 else '#ff5252' for p in pnls]

    bars = ax.bar(day_labels, pnls, color=colors, width=0.6, edgecolor='none')
    ax.axhline(y=0, color='#444466', linewidth=0.8)
    ax.set_title(f'PnL Diario — Semana {monday.strftime("%d/%m")} a {friday.strftime("%d/%m")}')
    ax.set_ylabel('PnL (R$)')
    ax.grid(axis='y', alpha=0.3)

    for bar, pnl in zip(bars, pnls):
        y = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2, y + (20 if y >= 0 else -60),
                f'R${pnl:+,.0f}', ha='center', va='bottom' if y >= 0 else 'top',
                fontsize=9, fontweight='bold', color='#e0e0ff')

    plt.tight_layout()
    path = REPORTS_DIR / f"weekly_pnl_dia_{week_str}.png"
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    charts.append(str(path))

    # === 2. PnL por Ativo (barras horizontais) ===
    fig, ax = plt.subplots(figsize=(10, 5))
    syms = sorted(analysis["by_symbol"].keys(), key=lambda s: analysis["by_symbol"][s]["pnl"])
    sym_pnls = [analysis["by_symbol"][s]["pnl"] for s in syms]
    colors = ['#00e676' if p >= 0 else '#ff5252' for p in sym_pnls]

    ax.barh(syms, sym_pnls, color=colors, height=0.6, edgecolor='none')
    ax.axvline(x=0, color='#444466', linewidth=0.8)
    ax.set_title('PnL por Ativo')
    ax.set_xlabel('PnL (R$)')
    ax.grid(axis='x', alpha=0.3)

    for i, (sym, pnl) in enumerate(zip(syms, sym_pnls)):
        ax.text(pnl + (50 if pnl >= 0 else -50), i,
                f'R${pnl:+,.0f}', va='center',
                ha='left' if pnl >= 0 else 'right',
                fontsize=9, fontweight='bold', color='#e0e0ff')

    plt.tight_layout()
    path = REPORTS_DIR / f"weekly_pnl_ativo_{week_str}.png"
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    charts.append(str(path))

    # === 3. PnL por Estratégia (pizza) ===
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    # Pizza: distribuição de trades
    strats = list(analysis["by_strategy"].keys())
    strat_trades = [analysis["by_strategy"][s]["trades"] for s in strats]
    strat_colors = ['#6c63ff', '#00d4aa', '#ff9100', '#448aff', '#ff4081', '#18ffff', '#ffd740']

    if strats:
        wedges, texts, autotexts = ax1.pie(strat_trades, labels=strats, autopct='%1.0f%%',
                                            colors=strat_colors[:len(strats)],
                                            textprops={'color': '#e0e0ff', 'fontsize': 9})
        for t in autotexts:
            t.set_color('#0f0f23')
            t.set_fontweight('bold')
    ax1.set_title('Trades por Estrategia')

    # Barras: PnL por estratégia
    strat_pnls = [analysis["by_strategy"][s]["pnl"] for s in strats]
    colors = ['#00e676' if p >= 0 else '#ff5252' for p in strat_pnls]
    ax2.barh(strats, strat_pnls, color=colors, height=0.6)
    ax2.axvline(x=0, color='#444466', linewidth=0.8)
    ax2.set_title('PnL por Estrategia')
    ax2.set_xlabel('PnL (R$)')
    ax2.grid(axis='x', alpha=0.3)

    plt.tight_layout()
    path = REPORTS_DIR / f"weekly_estrategia_{week_str}.png"
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    charts.append(str(path))

    # === 4. Equity Curve ===
    fig, ax = plt.subplots(figsize=(10, 5))
    eq = analysis["equity"]
    if eq:
        x = range(len(eq))
        y = [e["cumulative"] for e in eq]
        ax.fill_between(x, y, alpha=0.3, color='#6c63ff')
        ax.plot(x, y, color='#6c63ff', linewidth=2)

        # Marcar peak e drawdown
        peak_idx = 0
        peak_val = 0
        for i, v in enumerate(y):
            if v > peak_val:
                peak_val = v
                peak_idx = i
        ax.plot(peak_idx, peak_val, 'o', color='#00e676', markersize=8, zorder=5)
        ax.annotate(f'Peak R${peak_val:+,.0f}', (peak_idx, peak_val),
                    textcoords="offset points", xytext=(10, 10),
                    fontsize=9, color='#00e676', fontweight='bold')

        min_idx = y.index(min(y))
        ax.plot(min_idx, min(y), 'o', color='#ff5252', markersize=8, zorder=5)
        ax.annotate(f'Max DD R${analysis["max_drawdown"]:,.0f}', (min_idx, min(y)),
                    textcoords="offset points", xytext=(10, -15),
                    fontsize=9, color='#ff5252', fontweight='bold')

    ax.set_title('Equity Curve — Semana')
    ax.set_ylabel('PnL Acumulado (R$)')
    ax.set_xlabel('Trades')
    ax.grid(alpha=0.3)
    plt.tight_layout()
    path = REPORTS_DIR / f"weekly_equity_{week_str}.png"
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    charts.append(str(path))

    # === 5. Win Rate por Ativo ===
    fig, ax = plt.subplots(figsize=(10, 5))
    syms = sorted(analysis["by_symbol"].keys())
    wrs = []
    for s in syms:
        d = analysis["by_symbol"][s]
        wr = (d["wins"] / d["trades"] * 100) if d["trades"] > 0 else 0
        wrs.append(wr)

    colors = ['#00e676' if w >= 50 else '#ffd740' if w >= 35 else '#ff5252' for w in wrs]
    bars = ax.bar(syms, wrs, color=colors, width=0.6, edgecolor='none')
    ax.axhline(y=50, color='#00e676', linewidth=1, linestyle='--', alpha=0.5, label='50% WR')
    ax.axhline(y=35, color='#ff5252', linewidth=1, linestyle='--', alpha=0.5, label='35% WR')
    ax.set_title('Win Rate por Ativo')
    ax.set_ylabel('Win Rate (%)')
    ax.set_ylim(0, 100)
    ax.grid(axis='y', alpha=0.3)
    ax.legend(fontsize=8)

    for bar, wr in zip(bars, wrs):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 2,
                f'{wr:.0f}%', ha='center', va='bottom',
                fontsize=10, fontweight='bold', color='#e0e0ff')

    plt.tight_layout()
    path = REPORTS_DIR / f"weekly_wr_{week_str}.png"
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    charts.append(str(path))

    # === 6. Exit Reasons (pizza) ===
    fig, ax = plt.subplots(figsize=(8, 5))
    reasons = list(analysis["by_reason"].keys())
    counts = [analysis["by_reason"][r]["count"] for r in reasons]
    reason_colors = ['#ff5252', '#ffd740', '#448aff', '#00e676', '#6c63ff', '#ff4081']

    if reasons:
        wedges, texts, autotexts = ax.pie(counts, labels=reasons, autopct='%1.0f%%',
                                           colors=reason_colors[:len(reasons)],
                                           textprops={'color': '#e0e0ff', 'fontsize': 9})
        for t in autotexts:
            t.set_color('#0f0f23')
            t.set_fontweight('bold')
    ax.set_title('Motivos de Saida')
    plt.tight_layout()
    path = REPORTS_DIR / f"weekly_reasons_{week_str}.png"
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    charts.append(str(path))

    return charts


def format_report(analysis, monday, friday):
    """Formata relatório semanal para Telegram."""
    a = analysis
    lines = [
        f"📊 *RELATÓRIO SEMANAL Vibe-Trading*",
        f"📅 {monday.strftime('%d/%m')} a {friday.strftime('%d/%m/%Y')}",
        "━" * 28,
        "",
    ]

    # Resumo geral
    pnl_icon = "🟢" if a["total_pnl"] >= 0 else "🔴"
    lines.extend([
        f"{pnl_icon} *PnL Semanal: R$ {a['total_pnl']:+,.2f}*",
        "",
        f"📈 *Resumo Geral*",
        f"• Trades: {a['closed']} ({a['wins']}W / {a['losses']}L | WR {a['win_rate']:.0f}%)",
        f"• PnL Bruto: R$ {a['total_gross']:+,.2f}",
        f"• Taxas: R$ {a['total_fees']:,.2f}",
        f"• Melhor trade: R$ {a['best_trade']:+,.2f}",
        f"• Pior trade: R$ {a['worst_trade']:+,.2f}",
        f"• Média/trade: R$ {a['avg_trade']:+,.2f}",
        f"• Max Drawdown: R$ {a['max_drawdown']:,.2f}",
        "",
    ])

    # PnL por dia
    lines.append("📅 *PnL por Dia*")
    for day in sorted(a["by_day"].keys()):
        d = a["by_day"][day]
        wr = (d["wins"] / d["trades"] * 100) if d["trades"] > 0 else 0
        icon = "🟢" if d["pnl"] >= 0 else "🔴"
        day_name = ["Seg", "Ter", "Qua", "Qui", "Sex"][datetime.strptime(day, "%Y-%m-%d").weekday()]
        lines.append(f"{icon} {day_name} ({day[5:]}): {d['trades']}t | WR {wr:.0f}% | R$ {d['pnl']:+,.2f}")
    lines.append("")

    # Por símbolo
    lines.append("🏷️ *Por Ativo*")
    for sym in sorted(a["by_symbol"].keys(), key=lambda s: a["by_symbol"][s]["pnl"], reverse=True):
        d = a["by_symbol"][sym]
        wr = (d["wins"] / d["trades"] * 100) if d["trades"] > 0 else 0
        icon = "🟢" if d["pnl"] >= 0 else "🔴"
        lines.append(f"{icon} {sym}: {d['trades']}t | WR {wr:.0f}% | R$ {d['pnl']:+,.2f}")
    lines.append("")

    # Por estratégia
    lines.append("🎯 *Por Estratégia*")
    for strat in sorted(a["by_strategy"].keys(), key=lambda s: a["by_strategy"][s]["pnl"], reverse=True):
        d = a["by_strategy"][strat]
        wr = (d["wins"] / d["trades"] * 100) if d["trades"] > 0 else 0
        icon = "🟢" if d["pnl"] >= 0 else "🔴"
        lines.append(f"{icon} {strat}: {d['trades']}t | WR {wr:.0f}% | R$ {d['pnl']:+,.2f}")
    lines.append("")

    # Por timeframe
    lines.append("⏱️ *Por Timeframe*")
    tf_order = ["M5", "M15", "M30", "H1"]
    for tf in tf_order:
        if tf not in a["by_tf"]:
            continue
        d = a["by_tf"][tf]
        wr = (d["wins"] / d["trades"] * 100) if d["trades"] > 0 else 0
        icon = "🟢" if d["pnl"] >= 0 else "🔴"
        lines.append(f"{icon} {tf}: {d['trades']}t | WR {wr:.0f}% | R$ {d['pnl']:+,.2f}")
    lines.append("")

    # Exit reasons
    lines.append("🚪 *Motivos de Saída*")
    for reason in sorted(a["by_reason"].keys(), key=lambda r: a["by_reason"][r]["count"], reverse=True):
        d = a["by_reason"][reason]
        icon = "🔴" if "SL" in reason else "🟡" if "EOD" in reason else "🟢"
        lines.append(f"{icon} {reason}: {d['count']}x | R$ {d['pnl']:+,.2f}")
    lines.append("")

    # Top 5 melhores
    if a["best5"]:
        lines.append("🏆 *Top 5 Melhores Trades*")
        for i, t in enumerate(a["best5"], 1):
            time = t["entry_time"].split(" ")[1][:5] if t["entry_time"] else "?"
            lines.append(f"  {i}. 🟢 {t['symbol']} {t['direction']} {t['timeframe']} | "
                        f"{t['strategy']} | {time} | R$ {t['net_pnl']:+,.2f}")
        lines.append("")

    # Top 5 piores
    if a["worst5"]:
        lines.append("💀 *Top 5 Piores Trades*")
        for i, t in enumerate(a["worst5"], 1):
            time = t["entry_time"].split(" ")[1][:5] if t["entry_time"] else "?"
            lines.append(f"  {i}. 🔴 {t['symbol']} {t['direction']} {t['timeframe']} | "
                        f"{t['strategy']} | {time} | R$ {t['net_pnl']:+,.2f}")
        lines.append("")

    # Gráficos
    lines.extend([
        "━" * 28,
        "📊 Gráficos anexos:",
        "• PnL diário",
        "• PnL por ativo",
        "• Estratégias (trades + PnL)",
        "• Equity curve",
        "• Win rate por ativo",
        "• Motivos de saída",
    ])

    return "\n".join(lines)


def export_csv(analysis, monday, friday):
    """Exporta trades da semana para CSV."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    week_str = f"{monday.strftime('%d%m')}_{friday.strftime('%d%m')}"
    csv_path = REPORTS_DIR / f"weekly_trades_{week_str}.csv"

    fieldnames = [
        "Data Entrada", "Data Saída", "Ativo", "Direção", "Volume",
        "Timeframe", "Estratégia", "Preço Entrada", "Preço Saída",
        "Motivo Saída", "PnL Bruto", "Taxas", "Swap", "PnL Líquido"
    ]

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        for t in analysis["trades"]:
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
            })

    return str(csv_path)


def main():
    prev = "--prev" in sys.argv
    export = "--export-csv" in sys.argv

    monday, friday = get_week_range(prev=prev)
    print(f"Semana: {monday} a {friday}")

    trades, daily = get_weekly_trades(monday, friday)
    if not trades:
        print("Nenhum trade na semana.")
        return

    analysis = analyze_week(trades, daily)
    if not analysis:
        print("Erro na análise.")
        return

    # Gerar gráficos
    print("Gerando gráficos...")
    charts = generate_charts(analysis, monday, friday)
    print(f"  {len(charts)} gráficos gerados")

    # Formatar relatório
    report = format_report(analysis, monday, friday)
    print(report)

    # Export CSV
    if export:
        csv_path = export_csv(analysis, monday, friday)
        print(f"\n📄 CSV: {csv_path}")

    # Salvar relatório texto
    week_str = f"{monday.strftime('%d%m')}_{friday.strftime('%d%m')}"
    txt_path = REPORTS_DIR / f"weekly_report_{week_str}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"📄 Relatório: {txt_path}")

    # Output charts paths para o cron enviar
    print(f"\nCHARTS:{'|'.join(charts)}")


if __name__ == "__main__":
    main()
