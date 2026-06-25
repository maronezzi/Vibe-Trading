"""
Vibe-Trading Notify — deduplicação de mensagens Telegram.

Problema (23/06/2026):
  Eventos críticos (STREAK_LOSS, DRAWDOWN, HALT, MAXIMO_DIARIO) enviavam
  mensagens repetidas a cada ciclo de 6s enquanto a condição persistia.
  Exemplo: 5x STREAK_LOSS WINQ26 M5 em 30s.

Solução:
  notify_once(key, msg, cooldown_min) — envia e marca timestamp; chamadas
  dentro do cooldown são suprimidas. Chave é uma string arbitrária
  (ex: "STREAK_LOSS:WIN:M5" ou "HALT:WDO:M15").

  Cooldowns padrão (em minutos):
    STREAK_LOSS: 60 (1h)
    DRAWDOWN: 30
    HALT: 60
    MAXIMO_DIARIO: 1440 (1 dia)
    ENTRY: 0 (sempre envia, uma msg por trade é desejável)
    EXIT: 0
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Optional

# Estado global: chave → timestamp do último envio
_state: Dict[str, float] = {}
_lock = threading.Lock()

DEFAULT_COOLDOWNS = {
    "STREAK_LOSS": 60,
    "DRAWDOWN": 30,
    "HALT": 60,
    "MAXIMO_DIARIO": 1440,
    "TRADE_OPEN": 0,
    "TRADE_CLOSE": 0,
    "RECONCILE_DRIFT": 30,
    "WARN": 30,
}


def notify_once(
    key: str,
    msg: str,
    send_fn: Callable[[str], None],
    cooldown_min: Optional[float] = None,
    force: bool = False,
) -> bool:
    """
    Envia msg via send_fn se a chave não foi enviada dentro do cooldown.

    Args:
        key: identificador único do evento (ex: "STREAK_LOSS:WIN:M5").
        msg: texto a enviar.
        send_fn: função que recebe string e envia (ex: notify_telegram).
        cooldown_min: cooldown em minutos. Se None, usa DEFAULT_COOLDOWNS
            inferido pelo prefixo da key, ou 60 se não bater.
        force: se True, ignora cooldown e envia sempre.

    Returns:
        True se enviou, False se suprimido por cooldown.
    """
    if cooldown_min is None:
        for prefix, cd in DEFAULT_COOLDOWNS.items():
            if key.startswith(prefix):
                cooldown_min = cd
                break
        if cooldown_min is None:
            cooldown_min = 60

    cooldown_sec = cooldown_min * 60
    now = time.time()

    with _lock:
        if not force:
            last = _state.get(key, 0.0)
            if cooldown_sec > 0 and (now - last) < cooldown_sec:
                return False
        _state[key] = now

    try:
        send_fn(msg)
        return True
    except Exception:
        # Reverter marcação se envio falhou (permite retry no próximo ciclo)
        with _lock:
            _state.pop(key, None)
        raise


def reset_cooldown(key: Optional[str] = None) -> None:
    """Reseta cooldown de uma chave (ou todas se key=None). Útil para testes."""
    with _lock:
        if key is None:
            _state.clear()
        else:
            _state.pop(key, None)


def get_state() -> Dict[str, float]:
    """Snapshot read-only do estado (para debug/testes)."""
    with _lock:
        return dict(_state)


# ============================================================
# Mensagens padronizadas em PT-BR com fonte
# ============================================================

def fmt_trade_open(symbol: str, tf: str, side: str, qty: int, price: float,
                   sl: float, atr: float, strategy: str, ticket: int,
                   equity: float, daily_pnl: float) -> str:
    """Formata msg de abertura de trade em PT-BR com fonte [MT5]."""
    # PT-BR: ponto nos milhares, vírgula nos decimais
    equity_str = f"{equity:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    pnl_str = f"{daily_pnl:+,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return (
        f"📊 *{side} {symbol} {tf}* ({strategy})\n"
        f"• Entrada: {price:.2f} | SL: {sl:.2f}\n"
        f"• ATR: {atr:.0f} | SL: {sl/atr:.1f}x ATR\n"
        f"• Volume: {qty} contrato(s) | Ticket: {ticket}\n"
        f"• [MT5] Equity: R$ {equity_str} | PnL dia: R$ {pnl_str}"
    )


def fmt_trade_close(symbol: str, tf: str, side: str, qty: int,
                    entry: float, exit_price: float, pnl: float,
                    reason: str, ticket: int, equity: float,
                    daily_pnl: float, source: str = "MT5") -> str:
    """Formata msg de fechamento em PT-BR."""
    emoji = "🟢" if pnl >= 0 else "🔴"
    return (
        f"⚡ *Fechou {symbol} {tf}*\n"
        f"• {side} | {emoji} R$ {pnl:+,.2f}\n"
        f"• Entrada: {entry:.2f} → Saída: {exit_price:.2f}\n"
        f"• Volume: {qty} | Ticket: {ticket}\n"
        f"• Motivo: {reason} | [{source}]\n"
        f"• [MT5] Equity: R$ {equity:,.2f} | PnL dia: R$ {daily_pnl:+,.2f}"
    )


def fmt_streak_loss(symbol_root: str, tf: str, count: int, limit: int,
                    daily_pnl: float) -> str:
    return (
        f"🔻 *STREAK_LOSS* {symbol_root} {tf}\n"
        f"{count} perdas consecutivas em {symbol_root}\n"
        f"• Limite: {limit} | PnL Dia: R$ {daily_pnl:+,.2f}\n"
        f"• Próxima perda ativa HALT de 1h"
    )


def fmt_drawdown(symbol: str, tf: str, side: str, pnl: float,
                 entry: float, current: float, sl_dist: float,
                 atr: float, ticket: int, qty: int, duration_min: int,
                 daily_pnl: float) -> str:
    return (
        f"⚠️ *DRAWDOWN* {symbol} {tf}\n"
        f"{side} {symbol} {tf}\n"
        f"• Prejuízo: R$ {pnl:+,.2f}\n"
        f"• Entrada: {entry:.2f} → Atual: {current:.2f}\n"
        f"• SL: {sl_dist:.2f} pts restantes\n"
        f"• ATR: {atr:.0f} | Drawdown: {abs(current-entry)/atr:.1f}x ATR\n"
        f"• Ticket: {ticket} | Vol: {qty}\n"
        f"• Duração: {duration_min}min\n"
        f"• PnL Dia: R$ {daily_pnl:+,.2f}"
    )


def fmt_reconcile(result: dict) -> str:
    """Formata msg de reconciliação detectando drift."""
    lines = ["🔄 *[RECONCILIADO] Sincronização MT5 ↔ DB*"]
    if result.get("inserted"):
        lines.append(f"• {len(result['inserted'])} posição(ões) inserida(s) no DB")
        for ins in result["inserted"][:3]:
            lines.append(
                f"  - {ins['direction']} {ins['symbol']} {ins['tf']} @ {ins['entry_price']} (ticket {ins['ticket']})"
            )
    if result.get("closed"):
        lines.append(f"• {len(result['closed'])} posição(ões) fechada(s) (sumiram do MT5)")
    if result.get("divergences"):
        lines.append(f"• {len(result['divergences'])} divergência(s) de preço:")
        for d in result["divergences"][:3]:
            lines.append(
                f"  - ticket {d['ticket']}: DB={d['db_entry']} MT5={d['mt5_entry']}"
            )
    if result.get("errors"):
        lines.append(f"• {len(result['errors'])} erro(s): {result['errors'][:2]}")
    if not any([result.get("inserted"), result.get("closed"), result.get("divergences")]):
        lines.append("• ✅ DB e MT5 em sincronia")
    return "\n".join(lines)
