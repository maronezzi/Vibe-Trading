"""
test_wave880_profit_lock.py — Testes para o bloco PROFIT_LOCK no autotrader.

Wave 880.A1: PORT do profit_lock_r do backtest_v944.py:396-399 para
core/vt_autotrader.py manage_position(). Antes o parâmetro era dead code.

Estes testes validam via inspeção de código (mais robusto que mockar
manage_position, que tem dezenas de dependências MT5/Wine):
  1. Bloco PROFIT_LOCK existe no autotrader.
  2. Lê `profit_lock_r` dos params.
  3. Dispara quando profit_pts >= profit_lock_r * abs(sl_pts).
  4. Seta sl_pts NEGATIVO (profit-lock = SL acima entry p/ BUY).
  5. Compartilha flag be_applied com BREAKEVEN (mutuamente exclusivos).
  6. tp2_done faz parte do state dict da posição em _execute_entry.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_AUTOTRADER_PATH = _PROJECT_ROOT / "core" / "vt_autotrader.py"


def _read_autotrader() -> str:
    return _AUTOTRADER_PATH.read_text()


def test_profit_lock_block_exists():
    """Bloco PROFIT_LOCK deve existir no autotrader (Wave 880.A1)."""
    src = _read_autotrader()
    assert "PROFIT_LOCK" in src, (
        "Bloco PROFIT_LOCK não encontrado em vt_autotrader.py. "
        "Wave 880.A1 não foi aplicada?"
    )
    assert "profit_lock_r" in src, "profit_lock_r deve ser lido dos params"


def test_profit_lock_uses_abs_sl_pts():
    """profit_lock_r multiplica abs(sl_pts) (1R = distância absoluta do SL)."""
    src = _read_autotrader()
    # A condição deve comparar profit_pts com profit_lock_r * abs(sl_pts)
    assert "profit_lock_r * _one_r_pts" in src or "profit_lock_r * abs(sl_pts)" in src, (
        "PROFIT_LOCK deve comparar profit_pts com profit_lock_r * abs(sl_pts) (1R)."
    )


def test_profit_lock_sets_negative_sl_pts():
    """Profit-lock usa sl_pts NEGATIVO (SL acima entry p/ BUY, abaixo p/ SELL).

    cmd_modify é sign-aware: BUY SL = entry - sl_pts*point, então sl_pts<0
    coloca SL acima de entry.
    """
    src = _read_autotrader()
    # lock_pts deve ser negativo
    assert "lock_pts = -" in src, (
        "PROFIT_LOCK deve setar lock_pts NEGATIVO (sign-aware em cmd_modify)."
    )


def test_profit_lock_shares_be_applied_with_breakeven():
    """be_applied é compartilhado entre PROFIT_LOCK e BREAKEVEN —
    mutuamente exclusivos (quem disparar primeiro sela o SL)."""
    src = _read_autotrader()
    # O BREAKEVEN deve checar `not be_applied`
    assert "not be_applied" in src, (
        "BREAKEVEN deve respeitar `not be_applied` (setado pelo PROFIT_LOCK)."
    )
    # E o bloco PROFIT_LOCK deve setar be_applied = True
    assert "be_applied = True" in src


def test_tp2_state_in_position_dict():
    """_execute_entry deve inicializar tp2_done=False no state dict."""
    src = _read_autotrader()
    assert '"tp2_done": False' in src or "'tp2_done': False" in src, (
        "_execute_entry deve setar tp2_done=False no dict da posição (Wave 880.B4)."
    )


def test_tp2_block_exists():
    """Bloco TP2 deve existir no autotrader (Wave 880.B4)."""
    src = _read_autotrader()
    assert "[TP2]" in src, "Bloco TP2 não encontrado (Wave 880.B4 não aplicada?)"
    assert "tp2_r" in src and "tp2_pct" in src


def test_hard_exit_is_conditional_on_pnl():
    """Wave 880.B2: hard_exit_minutes só força close se profit_pts <= 0."""
    src = _read_autotrader()
    # A condição do HARD_EXIT deve incluir `profit_pts <= 0`
    assert "profit_pts <= 0" in src, (
        "HARD_EXIT deve ser condicional a profit_pts <= 0 (Wave 880.B2). "
        "Antes fechava vencedoras no auge aos 45min."
    )


def test_tp1_trail_order_fixed():
    """Wave 880.B1: trail_distance (default) deve vir ANTES do override
    atr_trail_mult condicional ao tp1_done. Antes era invertido (bug)."""
    src = _read_autotrader()
    # O bloco TP1_TRAIL (Wave 880.B1) deve virar DEPOIS do default
    # Pode haver múltiplas ocorrências; procuramos a Wave 880.B1 marcada
    assert "Wave 880.B1" in src, "Comentário Wave 880.B1 não encontrado"
    idx_wave_b1 = src.find("Wave 880.B1")
    idx_trail_default = src.find('trail_dist_cfg = params.get("trail_distance"', idx_wave_b1)
    assert idx_trail_default > 0, (
        "trail_distance default deve ser atribuído no escopo Wave 880.B1."
    )
    idx_tp1_check = src.find("if pos.get(\"tp1_done\")", idx_trail_default)
    assert idx_tp1_check > idx_trail_default, (
        "tp1_done check deve virar APÓS trail_distance default (Wave 880.B1). "
        "Antes era invertido e sobrescrevia silenciosamente o tighter trail."
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
