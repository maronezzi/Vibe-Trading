"""
vt_position_manager.py — Refator 3.1 (2026-07-08).

Módulo de extensões para gestão de posição. Por design conservador, a função
``manage_position`` principal fica em ``core/vt_autotrader.py`` (acoplada a
state global, MT5 bridge, indicators locais). Helpers NOVOS que
complementam position management ficam aqui para criar uma superfície
testável e reusável fora do monólito de 4.090 linhas.

Este módulo funciona com o autotrader via:
- ``from core.vt_position_manager import bump_loss_cooldown, ...``
- O autotrader importa daqui e chama em momentos chave do manage_position.

Estado-membro:
- ``bump_loss_cooldown(symbol, direction)``
- ``reset_loss_cooldown(symbol, direction)``
- ``check_loss_cooldown_active(symbol, direction) -> bool``
- ``day_trade_flatten_window(symbol, tf, pos_minutes, buffer_minutes=15) -> bool``

Por que split conservador (e não full extract de manage_position):
- manage_position referencia CONFIG/STATE módulo-globais do autotrader
  ~80 vezes. Mover exige late-imports (_at.X) ou refator de state.
- Isso cria diff grande com risco alto sem mudança comportamental.
- Padrão: novos hooks ficam em módulos dedicados; função principal
  fica onde já está bem testada.
"""
from __future__ import annotations

import logging
from datetime import datetime

log = logging.getLogger("vt_position_manager")


def check_loss_cooldown_active(
    symbol: str,
    direction: str,
    *,
    state=None,
    config: dict | None = None,
    max_consecutive: int | None = None,
    cooldown_minutes: int | None = None,
) -> bool:
    """Wave N+4B: cooldown per-(symbol, direction) pós loss consecutiva.

    Args:
        symbol: contrato MT5 resolvido (ex.: 'WINQ26').
        direction: 'BUY' ou 'SELL'.
        state: SessionState-like (lazy — autotrader passa seu ``state``).
        config: vt_config dict (lazy). Se None, lê via late import.
        max_consecutive: override direto (default: config.loss_cooldown.max_consecutive=2).
        cooldown_minutes: override direto (default: config.loss_cooldown.cooldown_minutes=30).

    Returns:
        True se cooldown está ativo (= bloquear).

    Defaults:
        - enabled=True (Wave opt-in; setar enabled=false para desligar).
        - max_consecutive=2, cooldown_minutes=30.
    """
    cfg = config if config is not None else _config_safe()
    if not cfg.get("enabled", True):
        return False
    mc = max_consecutive if max_consecutive is not None else cfg.get("max_consecutive", 2)
    cm = cooldown_minutes if cooldown_minutes is not None else cfg.get("cooldown_minutes", 30)
    if state is None:
        return False

    key = f"{symbol}_{direction}"
    count = state.consecutive_loss_direction_count.get(key, 0)
    if count < mc:
        return False
    last_ts = state.last_loss_direction_per_symbol.get(key)
    if last_ts is None:
        return False
    elapsed = (datetime.now() - last_ts).total_seconds() / 60.0
    if elapsed < cm:
        log.debug(
            f"[LOSS_COOLDOWN] {key}: {count}/{mc} losses, "
            f"{elapsed:.0f}min/{cm}min restantes"
        )
        return True
    # Cooldown expirou — limpa contador (state hygiene).
    state.consecutive_loss_direction_count[key] = 0
    return False


def bump_loss_cooldown(symbol: str, direction: str, state=None) -> None:
    """Wave N+4B: incrementa contador de loss per-(symbol, direction).

    Chamado em close detection quando pnl < 0.
    Reset implícito em reset_loss_cooldown (após WIN).
    """
    if state is None:
        return
    key = f"{symbol}_{direction}"
    cur = state.consecutive_loss_direction_count.get(key, 0)
    state.consecutive_loss_direction_count[key] = cur + 1
    state.last_loss_direction_per_symbol[key] = datetime.now()


def reset_loss_cooldown(symbol: str, direction: str, state=None) -> None:
    """Wave N+4B: reseta contador per-(symbol, direction) após WIN."""
    if state is None:
        return
    key = f"{symbol}_{direction}"
    state.consecutive_loss_direction_count[key] = 0
    state.last_loss_direction_per_symbol.pop(key, None)


def day_trade_flatten_window(
    symbol: str,
    tf: str,
    pos_minutes: float,
    *,
    config: dict | None = None,
    buffer_minutes: int = 15,
    now=None,
) -> bool:
    """Wave N+5A: deve fechar day-trade antes do EOD?

    Lê CONFIG.day_trade_intent[<sym>_<tf>] (default True).
    Quando intent=True e minutes_to_eod <= buffer_minutes → flatten.

    Returns:
        True se precisa fechar agora.
    """
    if config is None:
        config = _config_safe()
    symbol_root = _symbol_root(symbol)
    is_day_trade = config.get("day_trade_intent", {}).get(
        f"{symbol_root}_{tf}", True,
    )
    if not is_day_trade:
        return False

    eod_hour = config.get("close_hour", 16)
    eod_minute = config.get("close_minute", 45)
    if now is None:
        now = datetime.now()
    eod = now.replace(hour=eod_hour, minute=eod_minute, second=0, microsecond=0)
    minutes_to_eod = (eod - now).total_seconds() / 60.0
    return minutes_to_eod <= buffer_minutes


# ─── Helpers privados (compat) ─────────────────────────────────────────


def _symbol_root(symbol: str) -> str:
    """Root simples (WIN, WDO, BIT, etc.)."""
    for r in ("WIN", "WDO", "BIT", "DOL", "IND", "WSP"):
        if r in symbol:
            return r
    return symbol


def _config_safe() -> dict:
    """Lê CONFIG atual do autotrader (late import, defensivo)."""
    try:
        from core.vt_autotrader import CONFIG
        return CONFIG or {}
    except Exception:
        return {}


__all__ = [
    "check_loss_cooldown_active",
    "bump_loss_cooldown",
    "reset_loss_cooldown",
    "day_trade_flatten_window",
]
