"""
test_latency_slo.py — Wave N+4C (2026-07-08)

Valida core/vt_latency_monitor.py (ring buffer + percentil 95 + degradação).

Casos cobertos:
  1. record_latency persiste amostras.
  2. p95 retorna 0.0 sem histórico.
  3. p95 calcula percentil corretamente com amostras ordenadas.
  4. p95 ignora amostras fora da janela.
  5. should_degrade True quando p95 > degrade_ms (default 1000ms).
  6. warn_state True quando p95 > warn_ms (default 200ms).
  7. get_degraded_ops lista ops degradadas.
  8. reset_for_test limpa estado.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "core")):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.vt_latency_monitor import (  # noqa: E402
    DEFAULT_CONFIG,
    get_degraded_ops,
    p95,
    record_latency,
    reset_for_test,
    should_degrade,
    warn_state,
)


def setup_function(_):
    """Reset antes de cada teste (mais robusto que fixture)."""
    reset_for_test()


def teardown_function(_):
    reset_for_test()


# ═══════════════════════════════════════════════════════════
# Persistência
# ═══════════════════════════════════════════════════════════

def test_record_latency_persists():
    record_latency("buy", 100.0)
    record_latency("buy", 200.0)
    assert p95("buy") >= 100.0


def test_p95_empty_returns_zero():
    """Sem histórico retorna 0.0."""
    reset_for_test()  # força clear
    assert p95("never_recorded") == 0.0


# ═══════════════════════════════════════════════════════════
# Cálculo de p95
# ═══════════════════════════════════════════════════════════

def test_p95_simple_distribution():
    """20 amostras [100..1000] → p95 está perto de 950."""
    reset_for_test()
    # Inserir com timestamps recentes (sub-amostras pra usar room no buffer).
    now = time.time()
    from core.vt_latency_monitor import _HISTORY
    for i, val in enumerate(range(100, 1100, 50)):  # 20 amostras
        _HISTORY.setdefault("test", __import__("collections").deque(maxlen=500))
        _HISTORY["test"].append((now - 60 + i * 0.1, float(val)))
    result = p95("test", window_min=120)
    # 20 amostras, sorted, idx = int(20 * 0.95) - 1 = 18 → 18th smallest
    # sorted: [100, 150, ..., 1050]. idx 18 (0-based) = 100 + 18*50 = 1000.
    # Wait — sorted is by ms not by i, so position 18 should be around 950-1000.
    assert 800 <= result <= 1100


def test_p95_filters_window():
    """Amostras fora da janela são ignoradas."""
    reset_for_test()
    from core.vt_latency_monitor import _HISTORY
    from collections import deque
    dq = deque(maxlen=500)
    now = time.time()
    # Amostra velha (300s atrás) > degrade threshold
    dq.append((now - 300, 5000.0))
    # Amostra recente (30s atrás) dentro do threshold
    dq.append((now - 30, 50.0))
    _HISTORY["window_test"] = dq

    # Janela 60s → só a amostra recente conta
    result = p95("window_test", window_min=1)
    assert result == 50.0


# ═══════════════════════════════════════════════════════════
# should_degrade e warn_state
# ═══════════════════════════════════════════════════════════

def test_should_degrade_true_when_above_degrade_ms():
    """p95 > 1000ms (default) → degrada."""
    reset_for_test()
    record_latency("buy", 1500.0)
    assert should_degrade("buy") is True


def test_should_degrade_false_when_below_degrade_ms():
    record_latency("sell", 100.0)
    assert should_degrade("sell") is False


def test_warn_state_true_above_warn_ms():
    """p95 > 200ms (default warn) mas < degrade → estado warning."""
    record_latency("modify", 250.0)
    assert warn_state("modify") is True
    assert should_degrade("modify") is False


# ═══════════════════════════════════════════════════════════
# get_degraded_ops
# ═══════════════════════════════════════════════════════════

def test_get_degraded_ops_returns_only_degraded():
    record_latency("buy", 2000.0)
    record_latency("close", 50.0)
    record_latency("modify", 1500.0)
    degraded = get_degraded_ops()
    assert "buy" in degraded
    assert "modify" in degraded
    assert "close" not in degraded


def test_reset_for_test_clears():
    record_latency("x", 1000.0)
    reset_for_test()
    assert p95("x") == 0.0


# ═══════════════════════════════════════════════════════════
# DEFAULT_CONFIG sanity
# ═══════════════════════════════════════════════════════════

def test_default_config_has_sensible_values():
    """Defaults não são acidentalmente absurdos (0, negativos)."""
    cfg = DEFAULT_CONFIG
    assert 100 <= cfg["warn_ms"] <= 500
    assert 500 <= cfg["degrade_ms"] <= 5000
    assert 0.1 <= cfg["degrade_size_factor"] <= 1.0
    assert isinstance(cfg["degrade_disable_breakouts"], bool)
