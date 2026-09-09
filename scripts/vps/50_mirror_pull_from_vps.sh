#!/bin/bash
# =====================================================================
# Vibe-Trading — Fase E: ESPELHO noturno VPS → computador
# Roda NA MÁQUINA LOCAL (cron 19:40 seg-sex no crontab.local_dev.txt).
# Cumpre a regra do Bruno: cópia espelhada no computador E na nuvem.
#   - Nuvem  = VPS (runtime vivo) + GitHub (código)
#   - Local  = este espelho em $VT_MIRROR_DIR (NÃO o tree de desenvolvimento!)
#
# Ownership espelhado (direção inversa do deploy 20_sync):
#   vt_trades.db, vt_config.json, strategies/, relatórios, data/ → VPS manda
#
# Uso: bash 50_mirror_pull_from_vps.sh [--full] [--dry-run]
#   --full  inclui os prefixes Wine (~7 GB — usar manual/semanal)
# =====================================================================
set -uo pipefail
cd "$(dirname "$0")"
. ./vps.conf

FULL=0; DRY=""
for a in "$@"; do
    case "$a" in
        --full) FULL=1 ;;
        --dry-run) DRY="--dry-run" ;;
        *) echo "flags: --full --dry-run"; exit 1 ;;
    esac
done

mkdir -p "$VT_MIRROR_DIR/repo-runtime" "$VT_MIRROR_DIR/hermes"
LOG_STAMP=$(date '+%F %T')

echo "[$LOG_STAMP] == Espelho VPS → $VT_MIRROR_DIR ${DRY:-} =="

# ── 1. Runtime do repo (é espelho fiel: --delete aqui é correto) ─────
rsync -a --delete ${DRY} \
    "${VT_VPS_SSH}:${VT_PROJECT}/vt_trades.db"       "$VT_MIRROR_DIR/repo-runtime/" 2>/dev/null \
  && rsync -a --delete ${DRY} \
    "${VT_VPS_SSH}:${VT_PROJECT}/vt_config.json"     "$VT_MIRROR_DIR/repo-runtime/" \
  && rsync -a --delete ${DRY} \
    --exclude '_pending/' \
    "${VT_VPS_SSH}:${VT_PROJECT}/strategies/"        "$VT_MIRROR_DIR/repo-runtime/strategies/" \
  && rsync -a --delete ${DRY} \
    "${VT_VPS_SSH}:${VT_PROJECT}/monitoring/reports/" "$VT_MIRROR_DIR/repo-runtime/reports/" \
  || echo "[$LOG_STAMP] ⚠️ parte do repo-runtime falhou (VPS desligada? ssh?)"

# ── 2. Hermes essencial (config/auth/jobs — pequeno, diário) ─────────
rsync -a ${DRY} \
    --exclude 'gateway.*' --exclude '*.sock' --exclude '*.pid' --exclude '*.lock' \
    "${VT_VPS_SSH}:/home/bruno/.hermes/config.yaml" \
    "${VT_VPS_SSH}:/home/bruno/.hermes/auth.json" \
    "${VT_VPS_SSH}:/home/bruno/.hermes/SOUL.md" \
    "$VT_MIRROR_DIR/hermes/" 2>/dev/null \
  && rsync -a --delete ${DRY} \
    "${VT_VPS_SSH}:/home/bruno/.hermes/cron/" "$VT_MIRROR_DIR/hermes/cron/" 2>/dev/null \
  || echo "[$LOG_STAMP] ⚠️ espelho hermes falhou"

# ── 3. Opcional: prefixes Wine (pesado — manual ou semanal) ──────────
if [[ $FULL -eq 1 ]]; then
    for PREFIX in .wine .wine64; do
        mkdir -p "$VT_MIRROR_DIR/$PREFIX"
        rsync -a --delete ${DRY} \
            --exclude 'drive_c/users/*/Temp/' --exclude 'temp/' --exclude '*.log' \
            "${VT_VPS_SSH}:/home/bruno/$PREFIX/" "$VT_MIRROR_DIR/$PREFIX/" \
            && echo "[$LOG_STAMP] $PREFIX espelhado" \
            || echo "[$LOG_STAMP] ⚠️ $PREFIX falhou"
    done
fi

echo "[$LOG_STAMP] == Espelho concluído =="
