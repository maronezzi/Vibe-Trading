"""
runner.py — Entrypoint do cron para a AGI v4.

Substitui optimization/agi_tuning_17h.py (mantido intacto como fallback).

Uso:
    python3 optimization/agi_v4/runner.py                      # defaults
    python3 optimization/agi_v4/runner.py --days 7             # janela 7d
    python3 optimization/agi_v4/runner.py --dry-run            # não aplica
    python3 optimization/agi_v4/runner.py --max-iterations 3   # loop convergência

Cron (ver crontab.txt):
    00 12 * * 1-5 ... runner.py   # almoço
    10 17 * * 1-5 ... runner.py   # fechamento

Este módulo é o ÚNICO entrypoint sancionado pela AGI v4. Ele é listado em
core/vt_config_loader.py ALLOWED_WRITERS — pode orquestrar escritas no
vt_config.json (via save_full_config / save_params chamados pelo stage5).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Garantir que a raiz do projeto está no sys.path para imports absolutos
# (core.vt_config_loader, optimization.agi_v4.*, etc.)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _setup_logging(verbose: bool = False) -> None:
    """Configura logging para o cron (stderr → /tmp/vt_agi_v4.log)."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse dos argumentos CLI."""
    p = argparse.ArgumentParser(
        prog="agi_v4.runner",
        description="AGI v4 — Busca Exaustiva + Web + Geração de Estratégias",
    )
    p.add_argument("--days", type=int, default=7,
                   help="janela de análise em dias (default: 7)")
    p.add_argument("--dry-run", action="store_true", default=False,
                   help="não aplica mudanças no vt_config.json")
    p.add_argument("--max-iterations", type=int, default=1000,
                   help="teto de segurança anti-loop (default 1000). O loop "
                        "para por convergência (todo par positivo) ou estagnação "
                        "(2 iterações sem progresso).")
    p.add_argument("--verbose", action="store_true", default=False,
                   help="logging DEBUG")
    p.add_argument("--shadow", action="store_true", default=False,
                   help="modo shadow: otimiza em cópia do config, não aplica")
    # Wave 880.C4 (2026-07-19): modo de operação por horário de cron.
    # exploration (cron 12:00): max-iterations 3, busca candidatos novos.
    # conservative (cron 17:10): max-iterations 1, só revalida existentes,
    #   threshold +10% (mais rígido perto do EOD do pregão atual).
    # Default 'auto' detecta pela hora do sistema (12h=exploration, 17h=conservative).
    p.add_argument("--mode", choices=["exploration", "conservative", "auto"],
                   default="auto",
                   help="modo de operação: exploration (cron 12h, busca), "
                        "conservative (cron 17h, só revalida), auto (detecta "
                        "pela hora do sistema)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point principal. Retorna exit code (0 = sucesso)."""
    args = _parse_args(argv)
    _setup_logging(args.verbose)
    log = logging.getLogger("agi_v4.runner")

    # Wave 880.C4: resolve --mode auto pela hora do sistema.
    # 12h (cron almoço) → exploration; 17h (cron fechamento) → conservative.
    mode = args.mode
    if mode == "auto":
        from datetime import datetime as _dt
        _hour = _dt.now().hour
        mode = "exploration" if 11 <= _hour <= 13 else "conservative"
        log.info(f"--mode auto → '{mode}' (hora atual: {_hour}h)")

    # max_iterations é teto de segurança (não limite lógico). O loop para
    # por convergência (todo par PnL>0) ou estagnação (2 iterações sem
    # melhorar nenhum par). Default 1000 garante que bug nunca prenda o cron.
    max_it = max(1, args.max_iterations)

    # Wave 880.C4: modo conservative força max-iterations 1 (sem loop de
    # busca agressiva perto do EOD do pregão). 17:10 está a 1:35 do close.
    if mode == "conservative" and not args.dry_run and not args.shadow:
        max_it = min(max_it, 1)
        log.info(f"--mode conservative: max_iterations limitado a {max_it} "
                 f"(perto do EOD — só revalida candidatos, não busca novos)")

    # dry_run OU shadow → não aplica. Em produção (cron), ambos são False.
    effective_dry_run = args.dry_run or args.shadow
    if effective_dry_run:
        log.info("Modo NÃO-PRODUTIVO — mudanças não serão aplicadas "
                 f"(dry_run={args.dry_run}, shadow={args.shadow})")

    log.info(f"═══ AGI v4 START ═══ days={args.days} mode={mode} "
             f"max_iterations={max_it} dry_run={effective_dry_run}")

    try:
        # Import robusto: funciona tanto como módulo (python -m) quanto como
        # script direto (python optimization/agi_v4/runner.py). Quando rodado
        # como script, __package__ é None e o import relativo falha.
        try:
            from . import pipeline
        except ImportError:
            from optimization.agi_v4 import pipeline
        ctx = pipeline.run(
            days=args.days,
            dry_run=effective_dry_run,
            max_iterations=max_it,
        )
    except Exception as e:
        log.error(f"═══ AGI v4 FATAL ═══ pipeline.run falhou: {e}", exc_info=True)
        return 1

    # Resumo final no log (cron captura para /tmp/vt_agi_v4.log)
    converged = ctx.get("converged", False)
    n_applied = len(ctx.get("applied_changes", []))
    n_failing = len(ctx.get("failing_pairs", []))
    duration = ctx.get("duration_s", 0)

    log.info(f"═══ AGI v4 DONE ═══ converged={converged} "
             f"applied={n_applied} failing_pairs={n_failing} "
             f"duration={duration:.1f}s")

    # Exit code: 0 se ok (mesmo sem convergir — Lei 5 diz iterar, não abortar)
    return 0


if __name__ == "__main__":
    sys.exit(main())
