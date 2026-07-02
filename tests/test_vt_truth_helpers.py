"""
Testes do helper compute_sl_atr (Fase 3, Entregável 6 — Lei 3).

Valida que o helper SEMPRE retorna SL > 0 (floor min_sl), mesmo com entradas
degeneradas (atr=0, atr=None, mult=0, mult negativo). Lei 3: SL obrigatório.
"""
from __future__ import annotations

import pytest

from core.vt_truth import compute_sl_atr


class TestComputeSlAtr:
    def test_normal_calculation(self):
        """atr * mult normal."""
        assert compute_sl_atr(10.0, 1.5) == pytest.approx(15.0)

    def test_respects_min_sl_floor(self):
        """atr pequeno → sl cai abaixo de min_sl → usa min_sl (Lei 3)."""
        # 0.5 * 1.5 = 0.75 < 1.0 → floor 1.0
        assert compute_sl_atr(0.5, 1.5, min_sl=1.0) == pytest.approx(1.0)

    def test_zero_atr_returns_min_sl(self):
        """atr=0 não pode dar SL=0 (Lei 3). Retorna min_sl."""
        assert compute_sl_atr(0.0, 1.5, min_sl=1.0) == pytest.approx(1.0)

    def test_none_atr_returns_min_sl(self):
        """atr=None (ATR indisponível) → min_sl, nunca 0."""
        assert compute_sl_atr(None, 1.5, min_sl=1.0) == pytest.approx(1.0)

    def test_zero_mult_uses_default_mult(self):
        """sl_atr_mult=0 ou None → fallback 1.5 (não 0)."""
        # mult=0 → usa 1.5 → 10 * 1.5 = 15
        assert compute_sl_atr(10.0, 0, min_sl=1.0) == pytest.approx(15.0)
        assert compute_sl_atr(10.0, None, min_sl=1.0) == pytest.approx(15.0)

    def test_negative_mult_uses_default(self):
        """Mult negativo (config inválido) → fallback 1.5."""
        assert compute_sl_atr(10.0, -2.0, min_sl=1.0) == pytest.approx(15.0)

    def test_negative_atr_returns_min_sl(self):
        """atr negativo (dado corrompido) → min_sl, nunca negativo."""
        assert compute_sl_atr(-5.0, 1.5, min_sl=1.0) == pytest.approx(1.0)

    def test_custom_min_sl(self):
        """Caller pode subir o floor (ex: min_sl=5 para WDO)."""
        assert compute_sl_atr(2.0, 1.0, min_sl=5.0) == pytest.approx(5.0)

    def test_never_returns_zero_or_negative(self):
        """Propriedade fundamental (Lei 3): SL é SEMPRE > 0."""
        cases = [
            (0, 0), (None, None), (-1, -1), (0.001, 0.001),
            (1000, 0), (0, 1000),
        ]
        for atr, mult in cases:
            sl = compute_sl_atr(atr, mult, min_sl=1.0)
            assert sl > 0, f"SL={sl} para atr={atr} mult={mult} (deve ser > 0)"
