"""
Vibe-Trading Notify Log Filter — filtros de ruído operacional.

REGRA (Bruno, 23/06/2026):
"informações que foram CORRIGIDAS e NÃO IMPACTAM na operação NÃO devem
aparecer no Telegram."

Este módulo adiciona 3 níveis de notificação ao watchdog:

- notify_critical(msg, key=None): sempre envia (vai pro Telegram).
    Com `key`, dedup dentro do cooldown (default 60min).
    Use para: TRUE ORPHAN, WATCHDOG ALERTA, RECONCILIATION com drift real.

- notify_sync_ok(category, msg): envia SÓ se o conteúdo MUDOU desde o
    último envio da mesma categoria. Se for igual, suprime silenciosamente.
    Use para: heartbeat "OK", confirmações de state file sync que não
    alteram nada operacional.

- notify_silent(msg): NUNCA envia pro Telegram. Só registra no logger.
    Use para: [INFO], [DEBUG], [SYNC FIX] quando estado já está OK,
    logs de manutenção interna.

Integração com reconcile:
- notify_reconcile_drift(result, key): se result tem inserted/closed/
    divergences não-vazios, envia via critical. Se está tudo vazio,
    silencioso. Usa fmt_reconcile() do vt_notify.

Substitui (no cron vt_trade_watchdog.py) as chamadas diretas a
`notify_telegram()` para os prefixos [INFO], [DEBUG], [SYNC FIX],
[STATE FILE SYNC], [RECONCILE] e `format_ok()` heartbeats.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional, Dict, Any

# Logger estruturado (não print). Configurado no consumer (autotrader/watchdog).
_logger = logging.getLogger("vt.notify_log_filter")
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [vt.notify_log_filter] %(message)s",
        datefmt="%H:%M:%S",
    ))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)

# Cache de sync_ok: category → last_content (string) + last_sent (bool)
# - last_content: último conteúdo visto (para detectar mudança)
# - last_sent: True se o último conteúdo JÁ foi enviado
#
# Regra "start silent":
#   - Primeira chamada: NÃO envia (precisa ver o baseline)
#   - Chamadas seguintes com MESMO conteúdo: NÃO envia
#   - Chamadas seguintes com conteúdo DIFERENTE: ENVIA (mudança real)
#
# Isso garante que mensagens "heartbeat OK" repetidas com mesmo conteúdo
# nunca poluam o Telegram, mas mudanças relevantes (positions count,
# equity, divergences) cheguem ao operador.
_sync_cache: Dict[str, Dict[str, Any]] = {}
_sync_lock = threading.Lock()


def _send(msg: str) -> None:
    """
    Envia msg pro Telegram via hermes_send. Função isolada para mock em teste.

    Por padrão usa o mesmo target do autotrader (-1004284773048). Se
    `VT_TELEGRAM_TARGET` env var estiver setada, usa ele.
    """
    import os
    from core.vt_hermes_helper import hermes_send
    target = os.environ.get("VT_TELEGRAM_TARGET", "telegram:-1004284773048")
    try:
        hermes_send(target, msg, timeout=15)
    except Exception as e:
        _logger.warning("falha ao enviar Telegram: %s", e)


def _send_to_logger_only(msg: str) -> None:
    """Silencioso: só log, NUNCA Telegram."""
    _logger.info(msg)


# ============================================================
# API pública
# ============================================================

def notify_critical(msg: str, key: Optional[str] = None,
                    cooldown_min: float = 0) -> bool:
    """
    Envia msg pro Telegram. Comportamento:
    - Sem key: envia sempre (sem dedup).
    - Com key: dedup via notify_once() do vt_notify (cooldown_min default 60).

    Use para: problemas reais que exigem ação/atenção do operador.
    """
    if key is None:
        # Sem dedup: envia direto
        try:
            _send(msg)
        except Exception as e:
            _logger.error("notify_critical falhou: %s", e)
            return False
        return True

    # Com dedup: usa notify_once do vt_notify
    import core.vt_notify as nt
    return nt.notify_once(
        key=key,
        msg=msg,
        send_fn=_send,
        cooldown_min=cooldown_min if cooldown_min > 0 else None,
    )


def notify_sync_ok(category: str, msg: str) -> bool:
    """
    Notifica mudanças reais de estado (regra "start silent").

    Comportamento:
    - Primeira chamada: NÃO envia (baseline silencioso).
    - Chamadas com MESMO conteúdo: NÃO envia.
    - Chamadas com conteúdo DIFERENTE: ENVIA (mudança detectada).

    `category` é uma string livre (ex: "WATCHDOG_HEARTBEAT",
    "STATE_FILE_SYNC") que agrupa mensagens do mesmo tipo.

    Retorna True se enviou, False se suprimido.
    """
    with _sync_lock:
        entry = _sync_cache.get(category)
        if entry is None:
            # Primeira vez: registra baseline, NÃO envia
            _sync_cache[category] = {"last_content": msg, "last_sent": False}
            _logger.debug("sync_ok[%s]: baseline registrado, silencioso", category)
            return False

        if entry["last_content"] == msg:
            # Conteúdo igual → silencioso
            return False

        # Conteúdo mudou → envia e atualiza
        entry["last_content"] = msg
        entry["last_sent"] = True

    try:
        _send(msg)
    except Exception as e:
        _logger.error("notify_sync_ok falhou: %s", e)
        return False
    return True


def notify_silent(msg: str) -> None:
    """
    NUNCA envia pro Telegram. Só registra no logger estruturado.

    Use para: [INFO], [DEBUG], [SYNC FIX] informativo, [STATE FILE SYNC]
    que só confirma que está em dia, logs de reconciliação sem drift.
    """
    _send_to_logger_only(msg)


def notify_reconcile_drift(result: dict, key: Optional[str] = None) -> bool:
    """
    Notifica drift de reconciliação SÓ se houve mudança real.

    Se result["inserted"], result["closed"] ou result["divergences"] estão
    todos vazios → silencioso (reconciliação limpa).

    Caso contrário, usa fmt_reconcile() do vt_notify e envia como critical
    com dedup por key.
    """
    has_drift = bool(
        result.get("inserted") or result.get("closed") or result.get("divergences")
    )
    if not has_drift:
        # Reconciliação limpa: log info, não Telegram
        _logger.info("reconcile_with_mt5: DB e MT5 em sincronia, nenhuma divergência")
        return False

    # Drift real: formata e envia como critical
    import core.vt_notify as nt
    msg = nt.fmt_reconcile(result)
    if key is None:
        # Auto-gera key baseada nos tickets afetados
        tickets = []
        for ins in result.get("inserted", []):
            tickets.append(str(ins.get("ticket", "")))
        for c in result.get("closed", []):
            tickets.append(str(c.get("ticket", "")))
        for d in result.get("divergences", []):
            tickets.append(str(d.get("ticket", "")))
        key = f"RECONCILE:DRIFT:{','.join(sorted(tickets))}" if tickets else "RECONCILE:DRIFT"

    return notify_critical(msg, key=key, cooldown_min=30)


# ============================================================
# Utilitários de teste
# ============================================================

def reset_sync_cache(category: Optional[str] = None) -> None:
    """Reseta cache de sync_ok (útil para testes)."""
    with _sync_lock:
        if category is None:
            _sync_cache.clear()
        else:
            _sync_cache.pop(category, None)


def get_sync_cache() -> Dict[str, str]:
    """Snapshot read-only do cache de sync_ok (para debug/testes)."""
    with _sync_lock:
        return dict(_sync_cache)
