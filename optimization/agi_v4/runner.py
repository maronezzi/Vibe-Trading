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
import fcntl
import logging
import os
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
    # Wave 880.C4 / Wave 17h-completa (2026-07-30): modo de operação por horário
    # de cron. AMBOS rodam o loop de convergência completo (até convergir,
    # estagnar ou bater o deadline de 90min). A distinção é só rótulo/log:
    #   exploration  (cron 12:00): meio do pregão, usa metade das barras do dia.
    #   conservative (cron 17:10): pós-close (mercado fecha 16:45), usa o pregão
    #     INTEIRO de barras reais (09h-17h) e tem a noite toda pra testar.
    #     Historicamente o conservative era capado em max-iterations 1 com a
    #     premissa falsa de "perto do EOD do pregão atual" — mas às 17:10 o
    #     close já aconteceu (16:45) e posições estão flat. Bruno 30/07: as
    #     17:10 deve ser a otimização MAIS completa do dia.
    # Default 'auto' detecta pela hora do sistema (12h=exploration, 17h=conservative).
    p.add_argument("--mode", choices=["exploration", "conservative", "auto"],
                   default="auto",
                   help="modo de operação (rótulo): exploration (cron 12h), "
                        "conservative (cron 17h, pós-close), auto (detecta "
                        "pela hora). Ambos rodam o loop completo.")
    return p.parse_args(argv)


# ─── Lock anti-paralelismo (Wave anti-colisao, Bruno 30/07) ──────────────────
# LOCK ATÔMICO no kernel via fcntl.flock. O wrapper shell (run_agi_v4_cron.sh)
# tem um PID check, mas quem chama runner.py DIRETO (job externo, midday.sh,
# manual) bypassa esse check e colide no Wine/MT5 (single-session) — foi a
# causa raiz do "16/16 failing" das 17h de 30/07 (duas runs disputaram Wine).
# Este lock vive no runner (entrypoint sancionado), então vale pra QUALQUER
# invocação. Arquivo dedicado .lock evita conflito de formato com o .pid.
RUN_LOCK_PATH = Path("/tmp/vt_agi_v4.lock")


def _acquire_run_lock():
    """Tenta pegar lock exclusivo (não-bloqueante). Retorna fd ou None.

    Retorna um file handle aberto (segura o lock enquanto vivo) se conseguiu,
    ou None se já há uma run em andamento. flock é liberado automaticamente
    quando o fd fecha ou o processo morre (mesmo em crash/kill -9).
    """
    try:
        fd = os.open(str(RUN_LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        # Sem acesso a /tmp: deixa rodar (fail-open — não trava o cron).
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Lock ocupado: outra run está em andamento.
        os.close(fd)
        return None
    # Registra o PID no arquivo (informativo p/ diagnóstico, não p/ lock).
    try:
        os.write(fd, f"{os.getpid()}\n".encode())
    except OSError:
        pass
    return fd


def main(argv: list[str] | None = None) -> int:
    """Entry point principal. Retorna exit code (0 = sucesso)."""
    args = _parse_args(argv)
    _setup_logging(args.verbose)
    log = logging.getLogger("agi_v4.runner")

    # Lock anti-paralelismo: só uma run por vez (Wine/MT5 é single-session).
    # Se já há uma run ativa, sai graciosamente (exit 0, não-fatal — igual o
    # wrapper faz). Isto independe de quem chamou (cron, job externo, manual).
    lock_fd = _acquire_run_lock()
    if lock_fd is None:
        log.warning("AGI v4 já em execução (lock /tmp/vt_agi_v4.lock ocupado) — "
                    "abortando esta run para evitar colisão no Wine/MT5")
        return 0
    _setup_logging(args.verbose)
    log = logging.getLogger("agi_v4.runner")

    # Resolve --mode auto pela hora do sistema (rótulo p/ log/Telegram).
    # 12h (cron almoço) → exploration; 17h (cron fechamento) → conservative.
    # Ambos rodam o loop de convergência completo (ver abaixo).
    mode = args.mode
    if mode == "auto":
        from datetime import datetime as _dt
        _hour = _dt.now().hour
        mode = "exploration" if 11 <= _hour <= 13 else "conservative"
        log.info(f"--mode auto → '{mode}' (hora atual: {_hour}h)")

    # max_iterations é teto de segurança (não limite lógico). O loop para
    # por convergência (todo par PnL>0), estagnação (2 iterações sem
    # melhorar nenhum par) ou deadline de 90min (_DEADLINE_SECS no pipeline).
    # Default 1000 garante que bug nunca prenda o cron.
    #
    # Wave 17h-completa (Bruno 30/07): REMOVIDO o cap de max_iterations=1 que
    # o conservative tinha. Às 17:10 o mercado já fechou (16:45), posições
    # estão flat, e há a noite toda pra testar — o conservative usa o pregão
    # INTEIRO de barras reais e deve ser a otimização mais completa do dia.
    # Antes o cap se baseava na premissa falsa "perto do EOD do pregão atual".
    max_it = max(1, args.max_iterations)

    # dry_run OU shadow → não aplica. Em produção (cron), ambos são False.
    effective_dry_run = args.dry_run or args.shadow
    if effective_dry_run:
        log.info("Modo NÃO-PRODUTIVO — mudanças não serão aplicadas "
                 f"(dry_run={args.dry_run}, shadow={args.shadow})")

    log.info(f"═══ AGI v4 START ═══ days={args.days} mode={mode} "
             f"max_iterations={max_it} dry_run={effective_dry_run}")

    try:
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
    finally:
        # Libera o lock explicitamente (flock também libera no exit do processo,
        # mas o finally garante liberação mesmo em return/exception intermediário).
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
