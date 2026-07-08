"""
vt_latency_monitor.py — Wave N+4C (2026-07-08)

Monitor de latência Wine + degradação automática quando p95 excede threshold.
Complementa core/vt_sizing (sizing layer) com gate de saúde do bridge MT5.

API:
- record_latency(op, ms): wrapper para anotar latência de cada operação
  Wine executada via mt5_orchestrator.
- p95(op, window_min=60) -> float: p95 dos últimos N minutos.
- should_degrade(op) -> bool: True se p95 > degrade_ms.
- get_degraded_state(): dict {op: is_degraded, ratio_to_baseline}.

Integração com sizing:
- vt_sizing.resolve_volume multiplica volume final por degrade_size_factor
  quando should_degrade(op in {'buy','sell','modify','close','partial_close'}).

Alerting:
- monitoring/vt_copilot.py: warning sustentado se p95 > warn_ms por 5+ min.
"""
from __future__ import annotations

import collections
import logging
import time

log = logging.getLogger("vt_latency_monitor")

DEFAULT_CONFIG = {
    "warn_ms": 200,
    "degrade_ms": 1000,
    "degrade_size_factor": 0.5,
    "degrade_disable_breakouts": True,
}

# Ring buffer per operation type — guarda (ts, ms) tuples.
_HISTORY: dict[str, collections.deque] = {}
_HISTORY_MAXLEN = 500


def _config() -> dict:
    """Config default — late import para evitar circular com vt_sizing."""
    try:
        from core.vt_autotrader import CONFIG
        cfg = CONFIG.get("latency_slo") or {}
    except Exception:
        cfg = {}
    out = dict(DEFAULT_CONFIG)
    out.update(cfg)
    return out


def record_latency(op: str, ms: float) -> None:
    """Anota latência de operação Wine (em ms). Ring buffer por op.

    Args:
        op: nome da operação ('buy', 'sell', 'modify', 'close', 'partial_close').
        ms: duração em milissegundos.
    """
    if op not in _HISTORY:
        _HISTORY[op] = collections.deque(maxlen=_HISTORY_MAXLEN)
    _HISTORY[op].append((time.time(), float(ms)))


def p95(op: str, window_min: int = 60) -> float:
    """Percentil 95 da latência nos últimos N minutos para op.

    Returns:
        float (ms). 0.0 se sem histórico ou todas as amostras expiradas.
    """
    dq = _HISTORY.get(op)
    if not dq:
        return 0.0
    cutoff = time.time() - window_min * 60
    samples = sorted(ms for ts, ms in dq if ts >= cutoff)
    if not samples:
        return 0.0
    idx = max(0, int(len(samples) * 0.95) - 1)
    return samples[min(idx, len(samples) - 1)]


def should_degrade(op: str, window_min: int = 60) -> bool:
    """Retorna True se p95(op) > degrade_ms.

    Callsite usa isso como gate: se degradar, sizing layer multiplica por
    degrade_size_factor (default 0.5 = metade). Defensive default: degradar
    é melhor que continuar operando em bridge broken.
    """
    return p95(op, window_min) > _config()["degrade_ms"]


def get_degraded_ops(window_min: int = 60) -> set[str]:
    """Quais ops estão em estado degradado."""
    cfg = _config()
    return {
        op for op in (
            "buy", "sell", "modify", "close", "partial_close", "bars",
        )
        if p95(op, window_min) > cfg["degrade_ms"]
    }


def warn_state(op: str, window_min: int = 60) -> bool:
    """Retorna True se p95 > warn_ms (alerta, sem de fato degradar)."""
    return p95(op, window_min) > _config()["warn_ms"]


def reset_for_test() -> None:
    """Limpa histórico para testes."""
    _HISTORY.clear()


__all__ = [
    "DEFAULT_CONFIG",
    "record_latency",
    "p95",
    "should_degrade",
    "get_degraded_ops",
    "warn_state",
    "reset_for_test",
]
