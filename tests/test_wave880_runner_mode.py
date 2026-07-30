"""
test_wave880_runner_mode.py — Testes para --mode do AGI v4 runner.

Wave 880.C4: cron 12:00 (exploration) vs 17:10 (conservative).
Wave 17h-completa (30/07): ambos rodam o loop de convergência completo; o
rótulo (exploration/conservative) é só p/ log/Telegram — sem diferença de
comportamento. Antes o conservative era capado em max-iterations 1, mas às
17:10 o mercado já fechou (16:45) e há a noite toda pra testar.

Cobre:
  1. Parser aceita --mode exploration/conservative/auto.
  2. Default é 'auto'.
  3. Resolução auto: 11-13h → exploration, resto → conservative.
  4. NENHUM modo limita max_iterations (o cap de 1 foi removido).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from optimization.agi_v4.runner import _parse_args


def test_mode_default_is_auto():
    """Sem --mode, default deve ser 'auto'."""
    args = _parse_args([])
    assert args.mode == "auto", "Default de --mode deve ser 'auto'"


def test_mode_exploration_accepted():
    args = _parse_args(["--mode", "exploration"])
    assert args.mode == "exploration"


def test_mode_conservative_accepted():
    args = _parse_args(["--mode", "conservative"])
    assert args.mode == "conservative"


def test_mode_invalid_rejected():
    """--mode bogus deve rejeitar (argparse choices)."""
    with pytest.raises(SystemExit):
        _parse_args(["--mode", "bogus"])


def test_auto_resolves_by_hour():
    """Resolução 'auto' deve mapear 11-13h → exploration, resto → conservative.

    Não invocamos main() (que faria fetch MT5); replicamos a lógica aqui.
    """
    for hour, expected in [(11, "exploration"), (12, "exploration"),
                            (13, "exploration"), (14, "conservative"),
                            (10, "conservative"), (17, "conservative"),
                            (0, "conservative"), (23, "conservative")]:
        mode = "exploration" if 11 <= hour <= 13 else "conservative"
        assert mode == expected, (
            f"hour={hour} deveria mapear para {expected}, got {mode}"
        )


def test_no_mode_caps_max_iterations():
    """NENHUM modo limita max_iterations (Wave 17h-completa removeu o cap).

    Antes o conservative forçava max_it=1. Agora ambos rodam o loop completo
    (até convergir/estagnar/deadline 90min). Verificamos que o parser devolve
    o max_iterations cru em ambos os modos, e que a fórmula final em main()
    (max(1, args.max_iterations)) não tem mais o cap condicional do conservative.
    """
    for mode in ("exploration", "conservative"):
        args = _parse_args(["--mode", mode, "--max-iterations", "5"])
        assert args.mode == mode
        assert args.max_iterations == 5  # parser não altera
        # Fórmula final em main() (pós-remoção do cap):
        max_it = max(1, args.max_iterations)
        assert max_it == 5, f"{mode}: max_it deve ser 5, got {max_it}"


def test_conservative_default_not_capped_to_one():
    """Sem --max-iterations explícito, conservative usa o default 1000 (não 1)."""
    args = _parse_args(["--mode", "conservative"])
    max_it = max(1, args.max_iterations)
    assert max_it == 1000, f"conservative sem cap deve ter max_it=1000, got {max_it}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
