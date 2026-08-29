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
import json
import sqlite3
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "core"))  # noqa: E402 — fixa ModuleNotFoundError para `from vt_hermes_helper import hermes_send` (linha 252)

from mt5.mt5_orchestrator import status, close_all
from core.vt_autotrader import get_truth_from_mt5  # Wave 12.1 — helper centralizado


DB_PATH = Path(__file__).parent.parent / "vt_trades.db"
TELEGRAM_GROUP = "-1004284773048"

# Drift alert threshold (R$)
_DRIFT_ALERT_THRESHOLD = 5.0


def log(msg: str):
    """Log simples."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def get_events_daily_validation(target_date: str):
    """Validação broker-truth via mt5_trade_events para o dia.

    Retorna dict {events_pnl, events_deals, source} se pipeline saudável
    (heartbeat <10min), None se indisponível/stale.
    """
    try:
        from datetime import timedelta
        conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")

        # Staleness check
        cutoff = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S")
        last_ev = conn.execute(
            "SELECT MAX(event_time) FROM mt5_trade_events "
            "WHERE trans_type IN ('HEARTBEAT', 'LOGGER_START', 'DEAL_ADD', 'ORDER_ADD')"
        ).fetchone()[0]

        if last_ev is None or last_ev < cutoff:
            conn.close()
            return None

        # Dedup por deal_ticket (EA emite duplicatas em restart/reconect — Bruno 27/07)
        row = conn.execute("""
            SELECT COALESCE(SUM(pnl), 0.0), COUNT(*)
            FROM (
                SELECT deal_profit + deal_commission + deal_swap AS pnl
                FROM mt5_trade_events
                WHERE trans_type = 'DEAL_ADD'
                  AND deal_entry = 'OUT'
                  AND date(event_time) = ?
                GROUP BY deal_ticket
            )
        """, (target_date,)).fetchone()

        conn.close()
        return {
            "events_pnl": round(row[0], 2),
            "events_deals": row[1],
            "source": "MT5_EVENTS",
        }
    except Exception:
        return None


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
    """Gera relatório de trades do dia.

    Fonte primária: mt5_trade_events (EA broker-truth, dedup por deal_ticket).
    Fallback: tabela trades do DB (pode ter fantasmas/duplicatas).
    Bruno 27/07: EA como fonte principal.
    """
    if target_date is None:
        target_date = date.today().isoformat()

    # ── Tentar EA events primeiro ──
    try:
        from core.vt_trade_log import get_events_daily_summary
        ev = get_events_daily_summary(target_date)
    except Exception:
        ev = None

    if ev is not None:
        # Enriquecer com detalhes da tabela trades (estratégia, timeframe)
        # quando disponível — EA não tem esses campos.
        db = sqlite3.connect(str(DB_PATH))
        db.row_factory = sqlite3.Row
        trades_db = db.execute('''
            SELECT symbol, timeframe, direction, strategy,
                   entry_time, entry_price, entry_sl,
                   exit_time, exit_price, exit_reason,
                   gross_pnl, fees, swap, net_pnl,
                   signal_detail, notes
            FROM trades
            WHERE date(entry_time) = ?
            ORDER BY entry_time
        ''', (target_date,)).fetchall()
        db.close()

        return {
            "date": target_date,
            "source": "MT5_EVENTS",
            "trades": [dict(t) for t in trades_db],
            "summary": {
                "total_trades": ev["total_trades"],
                "wins": ev["wins"],
                "losses": ev["losses"],
                "breakeven": 0,
                "win_rate": ev["win_rate"],
                "total_pnl": ev["net_pnl"],
                "total_gross": 0,
                "total_fees": 0,
                "best_trade": ev["best_trade"],
                "worst_trade": ev["worst_trade"],
            },
            "by_symbol": ev.get("by_symbol", {}),
            "by_strategy": {},  # EA não tem estratégia
        }

    # ── Fallback: tabela trades ──
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row

    # Trades do dia
    trades = db.execute('''
        SELECT symbol, timeframe, direction, strategy,
               entry_time, entry_price, entry_sl,
               exit_time, exit_price, exit_reason,
               gross_pnl, fees, swap, net_pnl,
               signal_detail, notes
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


def format_report(report: dict, close_info: dict, events_validation=None) -> str:
    """Formata relatório para Telegram."""
    s = report["summary"]
    d = report["date"]

    # Header
    lines = [
        "📊 *RELATÓRIO DIÁRIO Vibe-Trading*",
        f"📅 {d}",
        "─" * 25,
        "",
    ]

    # Estado da conta — via get_truth_from_mt5() (helper centralizado, Wave 12.1).
    # Falha aqui = aviso explicito (NUNCA silenciar, ver regressao 2026-06-29).
    truth = get_truth_from_mt5()
    lines.append("💰 *Estado da Conta*")
    if truth.get("ok"):
        bal = truth.get("balance", 0.0) or 0.0
        eq = truth.get("equity", 0.0) or 0.0
        free = truth.get("margin_free", 0.0) or 0.0
        lines.append(f"• Saldo: R$ {bal:,.2f}")
        lines.append(f"• Equity: R$ {eq:,.2f}")
        lines.append(f"• Margem livre: R$ {free:,.2f}")
    else:
        # Wave 12.1: avisar explicitamente (NUNCA silenciar MT5 indisponivel)
        lines.append(f"⚠️ MT5 indisponível ({truth.get('error', '?')}) — saldo/equity não exibidos")
    lines.append("")

    # Fechamento de posições
    if close_info.get("closed", 0) > 0:
        lines.append(f"🔒 *{close_info['message']}*")
        lines.append("")

    # Resumo geral
    src = report.get("source", "DB")
    src_label = "EA broker-truth" if src == "MT5_EVENTS" else "DB (fallback)"
    pnl_icon = "🟢" if s["total_pnl"] > 0 else "🔴" if s["total_pnl"] < 0 else "⚪"
    lines.extend([
        f"📈 *Resumo do Dia* _(fonte: {src_label})_",
        f"• Trades: {s['total_trades']}",
        f"• Acertos: {s['wins']} ({s['win_rate']:.0f}%)",
        f"• Erros: {s['losses']}",
        f"• Melhor: R$ {s['best_trade']:+.2f}",
        f"• Pior: R$ {s['worst_trade']:+.2f}",
        "",
        f"{pnl_icon} *PnL Líquido: R$ {s['total_pnl']:+.2f}*",
        "",
    ])

    # Validação cruzada EA vs DB (quando EA é fonte primária)
    if events_validation is not None and src == "MT5_EVENTS":
        ev_pnl = events_validation["events_pnl"]
        db_pnl = s["total_pnl"]
        drift = round(abs(ev_pnl - db_pnl), 2)
        drift_icon = "✅" if drift <= _DRIFT_ALERT_THRESHOLD else "⚠️"
        lines.extend([
            "🔍 *Validação Cruzada (EA vs DB)*",
            f"• PnL EA: R$ {ev_pnl:+,.2f} ({events_validation['events_deals']} deals)",
            f"• PnL DB: R$ {db_pnl:+,.2f} ({s['total_trades']} trades)",
            f"{drift_icon} Drift: R$ {drift:,.2f}",
            "",
        ])
    elif events_validation is not None:
        ev_pnl = events_validation["events_pnl"]
        ev_deals = events_validation["events_deals"]
        db_pnl = s["total_pnl"]
        drift = round(abs(ev_pnl - db_pnl), 2)
        drift_icon = "✅" if drift <= _DRIFT_ALERT_THRESHOLD else "⚠️"
        lines.extend([
            "🔍 *Validação Broker-Truth (EA events)*",
            f"• PnL broker: R$ {ev_pnl:+,.2f} ({ev_deals} deals)",
            f"• PnL DB: R$ {db_pnl:+,.2f} ({s['total_trades']} trades)",
            f"{drift_icon} Drift: R$ {drift:,.2f}",
            "",
        ])
    else:
        lines.extend([
            "🔍 *Validação Broker-Truth*: indisponível (EA/watcher offline)",
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

    # Detalhes dos trades (últimos 10) — só quando fonte é DB.
    # Quando fonte é EA, a lista de trades vem do DB e pode ter fantasmas;
    # o resumo já usa EA como truth, então a lista seria inconsistente.
    if report["trades"] and report.get("source") != "MT5_EVENTS":
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
                f"{i}. {icon} {t['symbol']} {t['direction']} {t.get('timeframe','')} | "
                f"{t.get('strategy','')} | "
                f"{entry_time} @ {t['entry_price']} → {exit_time} @ {exit_price} | "
                f"R$ {pnl:+.2f} | {exit_reason}"
            )
            # Signal detail (indicadores no momento da entrada)
            sig = t.get('signal_detail')
            if sig:
                try:
                    sd = json.loads(sig) if isinstance(sig, str) else sig
                    parts = []
                    if 'rsi' in sd:
                        parts.append(f"RSI={sd['rsi']:.1f}")
                    if 'vwap' in sd:
                        parts.append(f"VWAP={sd['vwap']:.2f}")
                    if 'bb_upper' in sd:
                        parts.append(f"BB={sd.get('bb_lower',0):.0f}/{sd.get('bb_mid',0):.0f}/{sd.get('bb_upper',0):.0f}")
                    if 'atr' in sd:
                        parts.append(f"ATR={sd['atr']:.1f}")
                    if 'adx' in sd:
                        parts.append(f"ADX={sd['adx']:.1f}")
                    if 'ema_fast' in sd:
                        parts.append(f"EMA={sd.get('ema_fast',0):.0f}/{sd.get('ema_slow',0):.0f}")
                    if parts:
                        lines.append(f"   📐 {' | '.join(parts)}")
                except Exception:
                    pass
            # Notas (se houver)
            notes = t.get('notes')
            if notes and 'fees_synced' in str(notes):
                lines.append("   ✅ Fees sincronizados com MT5")

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
    from vt_hermes_helper import hermes_send
    hermes_send(f"telegram:{TELEGRAM_GROUP}", message)


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

    # 2b. Validação broker-truth (EA events)
    events_validation = get_events_daily_validation(report["date"])

    # 3. Formatar e enviar
    formatted = format_report(report, close_info, events_validation=events_validation)
    print(formatted)  # Output pro cron

    # 4. Enviar pro Telegram
    send_telegram(formatted)
    log("Relatório enviado!")

    return formatted


if __name__ == "__main__":
    main()
