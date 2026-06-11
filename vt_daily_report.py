#!/usr/bin/env python3
"""
Vibe-Trading Relatório Diário — Python puro, ZERO LLM.

Executa às 16:50 (depois do EOD 16:45):
1. Fecha qualquer posição aberta restante
2. Gera relatório completo do dia
3. Envia pro grupo Telegram

Uso:
    python vt_daily_report.py          # Relatório do dia atual
    python vt_daily_report.py --date 2026-06-09  # Relatório de dia específico
"""

import sys
import os
import subprocess
import json
import sqlite3
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mt5_orchestrator import status, close_all


DB_PATH = Path(__file__).parent / "vt_trades.db"
TELEGRAM_GROUP = "-1004284773048"


def log(msg: str):
    """Log simples."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def close_remaining_positions() -> dict:
    """Fecha posições abertas restantes."""
    s = status()
    positions = s.get("positions", [])
    
    if not positions:
        return {"closed": 0, "message": "Nenhuma posição aberta"}
    
    log(f"Fechando {len(positions)} posição(ões) restante(s)...")
    result = close_all()
    
    # Parse result
    if "raw_stdout" in result:
        try:
            data = json.loads(result["raw_stdout"].split("\n")[0])
            return {"closed": data.get("closed", 0), "message": f"Fechou {data.get('closed', 0)} posição(ões)"}
        except Exception:
            pass
    
    return {"closed": len(positions), "message": f"Fechou {len(positions)} posição(ões)"}


def get_trades_report(target_date: str = None) -> dict:
    """Gera relatório de trades do dia."""
    if target_date is None:
        # Ajusta para fuso horário local (UTC-3)
        target_date = date.today().isoformat()
    
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    
    # Trades do dia
    trades = db.execute('''
        SELECT symbol, timeframe, direction, strategy,
               entry_time, entry_price, entry_sl,
               exit_time, exit_price, exit_reason,
               gross_pnl, fees, swap, net_pnl
        FROM trades 
        WHERE date(entry_time) = ?
        ORDER BY entry_time
    ''', (target_date,)).fetchall()
    
    # Estatísticas
    total_trades = len(trades)
    wins = sum(1 for t in trades if t['net_pnl'] and t['net_pnl'] > 0)
    losses = sum(1 for t in trades if t['net_pnl'] and t['net_pnl'] < 0)
    breakeven = total_trades - wins - losses
    
    total_pnl = sum(t['net_pnl'] or 0 for t in trades)
    total_gross = sum(t['gross_pnl'] or 0 for t in trades)
    total_fees = sum(t['fees'] or 0 for t in trades)
    
    best_trade = max((t['net_pnl'] or 0 for t in trades), default=0)
    worst_trade = min((t['net_pnl'] or 0 for t in trades), default=0)
    
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    
    # Por símbolo
    symbols = {}
    for t in trades:
        sym = t['symbol']
        if sym not in symbols:
            symbols[sym] = {"trades": 0, "wins": 0, "pnl": 0}
        symbols[sym]["trades"] += 1
        if t['net_pnl'] and t['net_pnl'] > 0:
            symbols[sym]["wins"] += 1
        symbols[sym]["pnl"] += t['net_pnl'] or 0
    
    # Por estratégia
    strategies = {}
    for t in trades:
        strat = t['strategy'] or 'UNKNOWN'
        if strat not in strategies:
            strategies[strat] = {"trades": 0, "wins": 0, "pnl": 0}
        strategies[strat]["trades"] += 1
        if t['net_pnl'] and t['net_pnl'] > 0:
            strategies[strat]["wins"] += 1
        strategies[strat]["pnl"] += t['net_pnl'] or 0
    
    db.close()
    
    return {
        "date": target_date,
        "trades": [dict(t) for t in trades],
        "summary": {
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "breakeven": breakeven,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "total_gross": total_gross,
            "total_fees": total_fees,
            "best_trade": best_trade,
            "worst_trade": worst_trade,
        },
        "by_symbol": symbols,
        "by_strategy": strategies,
    }


def format_report(report: dict, close_info: dict) -> str:
    """Formata relatório para Telegram."""
    s = report["summary"]
    d = report["date"]
    
    # Header
    lines = [
        f"📊 *RELATÓRIO DIÁRIO Vibe-Trading*",
        f"📅 {d}",
        "─" * 25,
        "",
    ]
    
    # Estado da conta
    try:
        acc = status().get("account", {})
        lines.append("💰 *Estado da Conta*")
        lines.append(f"• Saldo: R$ {acc.get('balance', 0):,.2f}")
        lines.append(f"• Equity: R$ {acc.get('equity', 0):,.2f}")
        lines.append("")
    except Exception:
        pass
    
    # Fechamento de posições
    if close_info.get("closed", 0) > 0:
        lines.append(f"🔒 *{close_info['message']}*")
        lines.append("")
    
    # Resumo geral
    pnl_icon = "🟢" if s["total_pnl"] > 0 else "🔴" if s["total_pnl"] < 0 else "⚪"
    lines.extend([
        "📈 *Resumo do Dia*",
        f"• Trades: {s['total_trades']}",
        f"• Acertos: {s['wins']} ({s['win_rate']:.0f}%)",
        f"• Erros: {s['losses']}",
        f"• Melhor: R$ {s['best_trade']:+.2f}",
        f"• Pior: R$ {s['worst_trade']:+.2f}",
        "",
        f"{pnl_icon} *PnL Líquido: R$ {s['total_pnl']:+.2f}*",
        "",
    ])
    
    # Por símbolo
    if report["by_symbol"]:
        lines.append("📊 *Por Símbolo*")
        for sym, data in report["by_symbol"].items():
            wr = (data["wins"] / data["trades"] * 100) if data["trades"] > 0 else 0
            icon = "🟢" if data["pnl"] > 0 else "🔴" if data["pnl"] < 0 else "⚪"
            lines.append(f"{icon} {sym}: {data['trades']}t | WR {wr:.0f}% | R$ {data['pnl']:+.2f}")
        lines.append("")
    
    # Por estratégia
    if report["by_strategy"]:
        lines.append("🎯 *Por Estratégia*")
        for strat, data in report["by_strategy"].items():
            wr = (data["wins"] / data["trades"] * 100) if data["trades"] > 0 else 0
            icon = "🟢" if data["pnl"] > 0 else "🔴" if data["pnl"] < 0 else "⚪"
            lines.append(f"{icon} {strat}: {data['trades']}t | WR {wr:.0f}% | R$ {data['pnl']:+.2f}")
        lines.append("")
    
    # Detalhes dos trades (últimos 10)
    if report["trades"]:
        lines.append("📋 *Trades*")
        for i, t in enumerate(report["trades"][-10:], 1):
            pnl = t['net_pnl'] or 0
            icon = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
            exit_price = t['exit_price'] or "-"
            exit_reason = t['exit_reason'] or "ABERTO"
            entry_time = t['entry_time'].split(" ")[1][:5] if t['entry_time'] else "?"
            # Horário real de saída (não o motivo)
            exit_time = t['exit_time'].split(" ")[1][:5] if t['exit_time'] else "?"
            lines.append(
                f"{i}. {icon} {t['symbol']} {t['direction']} | "
                f"{entry_time} @ {t['entry_price']} → {exit_time} @ {exit_price} | "
                f"R$ {pnl:+.2f} | {exit_reason}"
            )
        
        if len(report["trades"]) > 10:
            lines.append(f"... e mais {len(report['trades']) - 10} trades")
        lines.append("")
    
    # Footer
    lines.extend([
        "─" * 25,
        f"🤖 Relatório gerado automaticamente em {datetime.now().strftime('%H:%M')}"
    ])
    
    return "\n".join(lines)


def send_telegram(message: str):
    """Envia mensagem pro grupo Telegram via hermes."""
    subprocess.run(
        ["hermes", "send", "-t", f"telegram:{TELEGRAM_GROUP}", message],
        capture_output=True, timeout=30
    )


def main():
    target_date = None
    
    # Parse args
    if "--date" in sys.argv:
        idx = sys.argv.index("--date")
        if idx + 1 < len(sys.argv):
            target_date = sys.argv[idx + 1]
    
    log("Iniciando relatório diário...")
    
    # 1. Fechar posições restantes
    close_info = close_remaining_positions()
    log(close_info["message"])
    
    # 2. Gerar relatório
    report = get_trades_report(target_date)
    log(f"Relatório: {report['summary']['total_trades']} trades, P&L R$ {report['summary']['total_pnl']:.2f}")
    
    # 3. Formatarktigsund enviar
    formatted = format_report(report, close_info)
    print(formatted)  # Output pro cron
    
    # 4. Enviar pro Telegram
    send_telegram(formatted)
    log("Relatório enviado!")
    
    return formatted


if __name__ == "__main__":
    main()
