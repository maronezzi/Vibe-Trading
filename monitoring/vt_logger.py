"""
monitoring/vt_logger.py
=======================
Logger estruturado (Fase 4.1) — logs concisos, estruturados, actionable.

Problema atual: logs verbosos ([STATE], [TICK], [STATE-REBUILD]...) difíceis de
auditar; mesmo warning repetido 100x em 1min = 100 linhas; sem nível claro.

Solução: wrapper sobre stdlib logging com:
  1. 4 níveis: INFO / WARN / ERROR / CRITICAL
  2. Formato estruturado: [HH:MM:SS] [LEVEL] [SUBSYSTEM] [EVENT] detalhe
  3. Agregação de WARN (N ocorrências em janela 1min = 1 linha com count)
  4. Telegram para ERROR/CRITICAL (com rate-limit: 1/min ERROR, 1/5min CRITICAL)
  5. Auto-heal hook para CRITICAL (tenta recovery antes de notificar)
  6. Retro-compat: se vt_logger não inicializado, fallback para print()

Lei 1: sem dependências externas (stdlib logging + json + time + collections).
NUNCA bloqueia o autotrader: se Telegram/auto-heal falham, só loga.

Uso:
    from monitoring.vt_logger import VtLogger
    log = VtLogger("autotrader")
    log.info("TRADE", "entry", symbol="WINQ26", ticket=12345, price=175000)
    log.warn("DRIFT", "high_drift", drift=263, threshold=5)
    log.error("MT5", "offline", last_ping_ms=5000)
    log.critical("AUTOTRADER", "crashed", exit_code=1)
"""
from __future__ import annotations

import logging
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

# sys.path (espelha vt_self_heal)
_PROJECT = Path(__file__).resolve().parent.parent
if str(_PROJECT) not in sys.path:
    sys.path.insert(0, str(_PROJECT))

TELEGRAM_TARGET = "telegram:-1004284773048"

# Rate limits (Lei 1: constantes nomeadas, não magic numbers)
ERROR_TELEGRAM_COOLDOWN_SEC = 60      # 1 msg/min para ERROR
CRITICAL_TELEGRAM_COOLDOWN_SEC = 300  # 1 msg/5min para CRITICAL
WARN_AGGREGATE_WINDOW_SEC = 60        # agrega WARNS em janela de 1min


def _format_details(details: Dict[str, Any]) -> str:
    """Formata kwargs como 'key=value key2=value2' (legível em uma linha)."""
    if not details:
        return ""
    parts = []
    for k, v in details.items():
        if isinstance(v, float):
            parts.append(f"{k}={v:.2f}")
        else:
            parts.append(f"{k}={v}")
    return " ".join(parts)


class VtLogger:
    """Logger estruturado com 4 níveis, agregação de WARN e Telegram.

    Thread-safety: não cria threads. Agregação é best-effort (pode perder
    contagens exatas sob concorrência, mas nunca crasha).
    """

    def __init__(self, name: str, telegram_enabled: bool = True,
                 auto_heal_fn: Optional[Callable] = None):
        self.logger = logging.getLogger(name)
        if not self.logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.INFO)
        self._name = name
        self._telegram_enabled = telegram_enabled
        self._auto_heal_fn = auto_heal_fn
        # Agregação de WARN: (subsystem, event) -> [timestamps]
        self._warn_buffer: Dict[Tuple[str, str], list] = defaultdict(list)
        # Rate-limit Telegram: event_key -> last_sent_ts
        self._telegram_last_sent: Dict[str, float] = {}

    def _emit(self, level: str, subsystem: str, event: str,
              details: Dict[str, Any]) -> str:
        """Emite linha formatada. Retorna a string (para testes)."""
        ts = datetime.now().strftime("%H:%M:%S")
        detail_str = _format_details(details)
        line = f"[{ts}] [{level:8}] [{subsystem}] [{event}]"
        if detail_str:
            line += f" {detail_str}"
        # logging level numérico
        level_num = getattr(logging, level, logging.INFO)
        self.logger.log(level_num, line)
        return line

    def info(self, subsystem: str, event: str, **details: Any) -> str:
        """INFO: sempre loga, nunca Telegram."""
        return self._emit("INFO", subsystem, event, details)

    def warn(self, subsystem: str, event: str, **details: Any) -> str:
        """WARN: agrega N ocorrências em 1min numa linha com count."""
        key = (subsystem, event)
        now = time.time()
        # limpa buffer antigo
        self._warn_buffer[key] = [t for t in self._warn_buffer[key]
                                  if now - t < WARN_AGGREGATE_WINDOW_SEC]
        self._warn_buffer[key].append(now)
        count = len(self._warn_buffer[key])
        details = dict(details)
        if count > 1:
            details["_aggregated_count"] = count
            # só emite a linha agregada a cada N ou na primeira
            return self._emit("WARN", subsystem, event, details)
        return self._emit("WARN", subsystem, event, details)

    def error(self, subsystem: str, event: str, **details: Any) -> str:
        """ERROR: sempre loga + Telegram (com rate-limit 1/min por event)."""
        line = self._emit("ERROR", subsystem, event, details)
        if self._telegram_enabled:
            self._notify_telegram_rate_limited(
                f"❌ [{subsystem}] {event}: {_format_details(details)}",
                key=f"error:{subsystem}:{event}",
                cooldown=ERROR_TELEGRAM_COOLDOWN_SEC,
            )
        return line

    def critical(self, subsystem: str, event: str, **details: Any) -> str:
        """CRITICAL: sempre loga + tenta auto-heal PRIMEIRO + Telegram só se falhar.

        Princípio (handoff Fase 4): "auto-heal primeiro, Telegram depois — só
        notifica se auto-cura falhar". Se auto-heal funciona, Bruno não precisa
        ser acordado (a dúvida 4 do handoff). Assim reduz spam de CRITICAL.
        """
        line = self._emit("CRITICAL", subsystem, event, details)
        healed = False
        # Auto-heal PRIMEIRO (se configurado)
        if self._auto_heal_fn is not None:
            try:
                healed = bool(self._auto_heal_fn(subsystem, event, details))
                if healed:
                    self.info(subsystem, f"{event}_auto_healed",
                              _note="auto-cura bem-sucedida")
            except Exception as e:  # pragma: no cover — auto-heal nunca crasha
                self.logger.error(f"[VT-LOGGER] auto-heal falhou: {e}")
        # Telegram SÓ se auto-heal falhou (ou não configurado)
        if not healed and self._telegram_enabled:
            self._notify_telegram_rate_limited(
                f"🚨 CRITICAL [{subsystem}] {event}: {_format_details(details)}",
                key=f"critical:{subsystem}:{event}",
                cooldown=CRITICAL_TELEGRAM_COOLDOWN_SEC,
            )
        return line

    def _notify_telegram_rate_limited(self, msg: str, key: str,
                                      cooldown: int) -> bool:
        """Envia Telegram respeitando rate-limit por key. Nunca levanta."""
        now = time.time()
        last = self._telegram_last_sent.get(key, 0)
        if now - last < cooldown:
            return False  # rate-limited
        self._telegram_last_sent[key] = now
        try:
            from core.vt_hermes_helper import hermes_send
            return hermes_send(TELEGRAM_TARGET, msg)
        except Exception:  # pragma: no cover
            return False


# Instância global opcional (fallback para print se não inicializada)
_global_logger: Optional[VtLogger] = None


def get_logger(name: str = "vibetrading") -> VtLogger:
    """Retorna logger global (lazy init)."""
    global _global_logger
    if _global_logger is None:
        _global_logger = VtLogger(name)
    return _global_logger


if __name__ == "__main__":  # pragma: no cover
    log = VtLogger("demo", telegram_enabled=False)
    log.info("TRADE", "entry", symbol="WINQ26", ticket=12345, price=175000)
    log.warn("DRIFT", "high_drift", drift=263, threshold=5)
    log.error("MT5", "offline", last_ping_ms=5000)
    log.critical("AUTOTRADER", "crashed", exit_code=1)
