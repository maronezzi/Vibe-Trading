"""
Vibe-Trading Block Notify — notificador centralizado de BLOQUEIOS DE OPERAÇÃO.

Motivação (Wave N+block_notify, Bruno 2026-07-20):
  O autotrader tem 30+ mecanismos de bloqueio (halt_trading, halt_new_trades,
  disabled_symbols/timeframes, aggregate_blackout, max_daily_loss, Lei 3/4 do
  orchestrator, validator LLM fail-closed, etc.). A maioria só logga, sem
  notificar o operador no Telegram. Quando o bot PARA de operar por motivo não-
  óbvio, o Bruno só descobre horas depois (ou no daily report 16:50).

  Esta central resolve com 1 função + 8 hooks (high-severity deste wave).
  Demais gaps ficam para waves incrementais.

API:
  notify_block_activated(category, symbol, tf, reason, severity, cooldown_min)
    - info     → só log estruturado, nunca Telegram
    - warning  → Telegram COM dedup (default 60min)
    - critical → Telegram COM dedup mais curto (default 30min). Caller pode
                 ajustar via cooldown_min (ex: max_daily_loss dispara todo
                 tick; use cooldown_min=1440 para silenciar até o dia
                 seguinte).

Categorias pre-definidas (CAT_*):
  HALT_TRADING, HALT_NEW_TRADES, DISABLED_SYMBOLS, DISABLED_TF,
  AGGREGATE_BLACKOUT, MAX_DAILY_LOSS, VALIDATOR_LLM_DOWN,
  LEI3_MISSING_SL, LEI4_RETCODE, ...

Dedup key: f"BLOCK:{category}:{symbol}:{tf}"
  - symbol="" para bloqueios cross-symbol (ex: max_daily_loss)
  - tf="" para bloqueios per-symbol-root (ex: disabled_symbols)

Targets Telegram:
  - Default: telegram:-1004284773048:1 (mesmo target do autotrader, com ":1"
    thread suffix que bypassa o anti-loop guard do hermes).
  - Override: env var VT_TELEGRAM_TARGET_BLOCK.

Não usar diretamente notify_telegram() do autotrader (closure de TELEGRAM_TARGET
não é visível). Esta função é o ÚNICO caminho para notificar bloqueios.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from core.vt_notify import notify_once  # noqa: E402  (path sys.path self-bootstrap)

_logger = logging.getLogger("vt.block_notify")
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [vt.block_notify] %(message)s",
        datefmt="%H:%M:%S",
    ))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)


TELEGRAM_TARGET_DEFAULT = "telegram:-1004284773048:1"


# ============================================================
# Categorias (constantes) — referência única para evitar typo
# ============================================================

CAT_HALT_TRADING        = "HALT_TRADING"
CAT_HALT_NEW_TRADES     = "HALT_NEW_TRADES"
CAT_DISABLED_SYMBOLS    = "DISABLED_SYMBOLS"
CAT_DISABLED_TF         = "DISABLED_TF"
CAT_AGGREGATE_BLACKOUT  = "AGGREGATE_BLACKOUT"
CAT_MAX_DAILY_LOSS      = "MAX_DAILY_LOSS"
CAT_VALIDATOR_LLM_DOWN  = "VALIDATOR_LLM_DOWN"
CAT_LEI3_MISSING_SL     = "LEI3_MISSING_SL"
CAT_LEI4_RETCODE        = "LEI4_RETCODE"


# ============================================================
# Envio
# ============================================================

def _send_telegram(msg: str) -> None:
    """Envia msg pro Telegram via hermes_send. Falha → log only (não crash)."""
    target = os.environ.get("VT_TELEGRAM_TARGET_BLOCK", TELEGRAM_TARGET_DEFAULT)
    try:
        from core.vt_hermes_helper import hermes_send
        ok = hermes_send(target, msg, timeout=15)
        if not ok:
            _logger.warning("hermes_send retornou False (target=%s)", target)
    except Exception as e:
        _logger.warning("falha ao enviar Telegram: %s (target=%s)", e, target)


def _send_to_logger_only(msg: str) -> None:
    """Severidade info: só log estruturado, NUNCA Telegram."""
    _logger.info(msg)


# ============================================================
# Formatadores por categoria (PT-BR)
# ============================================================

_EMOJI = {
    CAT_HALT_TRADING:       "🛑",
    CAT_HALT_NEW_TRADES:    "⏸️",
    CAT_DISABLED_SYMBOLS:   "🚫",
    CAT_DISABLED_TF:        "🚫",
    CAT_AGGREGATE_BLACKOUT: "⛔",
    CAT_MAX_DAILY_LOSS:     "🛑",
    CAT_VALIDATOR_LLM_DOWN: "🤖",
    CAT_LEI3_MISSING_SL:    "⚠️",
    CAT_LEI4_RETCODE:       "⚠️",
}


def _format_msg(category: str, symbol: str, tf: str, reason: str) -> str:
    """Monta msg Telegram PT-BR. Inclui símbolo/TF quando aplicável."""
    emoji = _EMOJI.get(category, "🔒")
    head = f"{emoji} *BLOQUEIO [{category}]*"
    if symbol and tf:
        head += f" — {symbol} {tf}"
    elif symbol:
        head += f" — {symbol}"
    return f"{head}\n{reason}\n🤖 Bot parado de operar"


# ============================================================
# API pública
# ============================================================

def notify_block_activated(
    category: str,
    symbol: str = "",
    tf: str = "",
    reason: str = "",
    severity: str = "warning",
    cooldown_min: Optional[float] = None,
) -> bool:
    """
    Notifica ativação de um bloqueio de operação.

    Args:
        category: uma das constantes CAT_* (string livre aceita, mas use a
            constante para evitar typo no dedup key).
        symbol: símbolo afetado (WIN, WDO, BIT, ...) ou "" para cross-symbol.
        tf: timeframe afetado (M5, H1, ...) ou "" se não aplicar.
        reason: descrição PT-BR curta do motivo (≤200 chars recomendado).
        severity: "info" (log only) | "warning" (Telegram com dedup 60min) |
            "critical" (Telegram com dedup 30min — mais apertado).
        cooldown_min: janela de dedup em minutos. Se None, usa default por
            severity (warning=60, critical=30). Caller pode sobrescrever
            (ex: max_daily_loss dispara todo tick → 1440min para silenciar
            até o dia seguinte; halt_trading/halt_new_trades idem).

    Returns:
        True se enviou (ou info → log), False se suprimido por dedup ou erro.
    """
    if severity not in ("info", "warning", "critical"):
        _logger.warning("severity inválida %r (esperado info|warning|critical) — tratando como warning", severity)
        severity = "warning"

    if cooldown_min is None:
        cooldown_min = {"info": 0, "warning": 60, "critical": 30}[severity]

    msg = _format_msg(category, symbol, tf, reason)

    if severity == "info":
        _send_to_logger_only(msg)
        return True

    # warning e critical: dedup por (category, symbol, tf)
    key = f"BLOCK:{category}:{symbol}:{tf}"
    try:
        return notify_once(
            key=key,
            msg=msg,
            send_fn=_send_telegram,
            cooldown_min=cooldown_min,
        )
    except Exception as e:
        _logger.error("notify_block_activated(%s) falhou: %s", severity, e)
        return False


# ============================================================
# Helpers de teste
# ============================================================

def reset_for_tests() -> None:
    """Limpa dedup cache do vt_notify. Útil para testes."""
    from core.vt_notify import reset_cooldown
    reset_cooldown()
    _logger.debug("block_notify dedup cache limpo")
