#!/bin/bash
# =====================================================================
# Vibe-Trading — Fase B: sync de código local → VPS (deploy de desenvolvimento)
# Roda NA MÁQUINA LOCAL. Convenção de ownership:
#   - Código/estratégias: nasce no local, vai para a VPS (este script).
#   - vt_trades.db / vt_config.json / relatórios: runtime da VPS,
#     voltam pelo espelho (50_mirror_pull). Este script NÃO sobrescreve.
#
# Uso:
#   bash 20_sync_code_to_vps.sh [--wine] [--with-config] [--prune] [--dry-run]
#     --wine         inclui os prefixes Wine (~7 GB na 1ª vez; idempotente depois)
#     --with-config  envia o vt_config.json local (senão, quem manda é a VPS)
#     --prune        espelho exato (--delete) — só use quando local for a verdade
#     --dry-run      mostra o que faria, não altera nada
# =====================================================================
set -euo pipefail
cd "$(dirname "$0")"
. ./vps.conf

WINE=0; WITH_CONFIG=0; PRUNE=0; DRY=""
for a in "$@"; do
    case "$a" in
        --wine) WINE=1 ;;
        --with-config) WITH_CONFIG=1 ;;
        --prune) PRUNE=1 ;;
        --dry-run) DRY="--dry-run" ;;
        *) echo "flag desconhecida: $a"; exit 1 ;;
    esac
done

RSYNC_EXCLUDES=(
    --exclude '.git/'
    --exclude '__pycache__/' --exclude '*.pyc'
    --exclude '.venv/' --exclude 'venv/' --exclude 'node_modules/'
    --exclude 'vt_trades.db' --exclude 'vt_trades.db-wal' --exclude 'vt_trades.db-shm'
    --exclude 'monitoring/reports/'
    --exclude 'data/'                 # caches de backtest — rebuild na VPS
    --exclude 'strategies/_pending/'  # gerado pela AGI na VPS
    --exclude '*.log'
    --exclude '.zcode/' --exclude '.claude/'
)
[[ $WITH_CONFIG -eq 1 ]] || RSYNC_EXCLUDES+=(--exclude 'vt_config.json')
[[ $PRUNE -eq 1 ]] && RSYNC_EXCLUDES+=(--delete)

echo "== Código local → ${VT_VPS_SSH}:${VT_PROJECT} ${DRY:-} =="
rsync -a ${DRY} "${RSYNC_EXCLUDES[@]}" \
    "$VT_PROJECT/" "${VT_VPS_SSH}:${VT_PROJECT}/"

if [[ $WINE -eq 1 ]]; then
    echo "== Prefixes Wine → VPS (mesmo usuário/caminho = registro compatível) =="
    for PREFIX in "$HOME/.wine" "$HOME/.wine64"; do
        NAME=$(basename "$PREFIX")
        rsync -a ${DRY} \
            --exclude 'drive_c/users/*/Temp/' \
            --exclude '*.log' --exclude 'temp/' \
            "$PREFIX/" "${VT_VPS_SSH}:/home/bruno/${NAME}/"
    done
fi

echo "== Sync concluído =="
