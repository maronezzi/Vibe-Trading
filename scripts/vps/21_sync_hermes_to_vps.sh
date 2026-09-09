#!/bin/bash
# =====================================================================
# Vibe-Trading — Fase B2: estado do Hermes local → VPS (1 gateway só)
# Roda NA MÁQUINA LOCAL. O Hermes é a fonte nº 1 de duplicidade na migração:
# gateway conectado ao Telegram + 36 jobs internos (vários ativos e
# sobrepostos ao crontab). Estratégia: O GATEWAY É ÚNICO e mora na VPS
# após o cutover; este script só move o ESTADO, nunca inicia processo.
#
# O que vai:  config.yaml, auth.json (credenciais Telegram/providers),
#             cron/jobs.json (scheduler interno), SOUL.md, skills/,
#             memories/, memory/, scripts/, hooks/, plugins/, bin/,
#             channel_directory.json
# O que NÃO vai: state.db (1,6 GB de histórico de sessões — opcional
#             com --state), venv/, node/, lsp/ (rebuild na VPS), sockets/pids.
#
# Uso: bash 21_sync_hermes_to_vps.sh [--state] [--dry-run]
# =====================================================================
set -euo pipefail
cd "$(dirname "$0")"
. ./vps.conf

WITH_STATE=0; DRY=""
for a in "$@"; do
    case "$a" in
        --state) WITH_STATE=1 ;;
        --dry-run) DRY="--dry-run" ;;
        *) echo "flag desconhecida: $a"; exit 1 ;;
    esac
done

H="$VT_HERMES_HOME"
[[ -d "$H" ]] || { echo "ERRO: $H não existe"; exit 1; }

echo "== Estado essencial do Hermes → ${VT_VPS_SSH} ${DRY:-} =="
# ATENÇÃO: .env carrega segredos (TELEGRAM_BOT_TOKEN, chaves de provider) que
# NÃO estão no config.yaml — sem ele, `hermes send` direto falha na VPS
# (lição 2026-09-03: "Platform 'telegram' is not configured").
rsync -a ${DRY} \
    --exclude 'gateway.*' --exclude '*.sock' --exclude '*.pid' --exclude '*.lock' \
    "$H/config.yaml" "$H/auth.json" "$H/.env" "$H/SOUL.md" "$H/channel_directory.json" \
    "${VT_VPS_SSH}:/home/bruno/.hermes/"
ssh "$VT_VPS_SSH" 'chmod 600 /home/bruno/.hermes/.env' 2>/dev/null || true

for DIR in cron skills memories memory scripts hooks plugins bin; do
    [[ -d "$H/$DIR" ]] && rsync -a ${DRY} --exclude '.jobs.lock' \
        "$H/$DIR/" "${VT_VPS_SSH}:/home/bruno/.hermes/$DIR/"
done

if [[ $WITH_STATE -eq 1 ]]; then
    echo "== state.db (histórico de sessões, 1,6 GB) — opcional =="
    # Para no destino: sqlite não gosta de ser sobrescrito quente; na VPS o
    # gateway ainda não existe neste ponto, então é seguro.
    rsync -a ${DRY} "$H/state.db" "${VT_VPS_SSH}:/home/bruno/.hermes/state.db"
fi

echo "== Fonte do hermes-agent (sem .git/venv — venv é rebuildado na VPS) =="
rsync -a ${DRY} \
    --exclude '.git/' --exclude 'venv/' --exclude 'node_modules/' \
    --exclude '__pycache__/' --exclude '.mypy_cache/' \
    "$H/hermes-agent/" "${VT_VPS_SSH}:/home/bruno/.hermes/hermes-agent/"

echo
echo "== Sync Hermes concluído (nada foi iniciado) =="
echo "Build do venv na VPS (uma vez, roda via ssh):"
echo "  ssh $VT_VPS_SSH 'cd ~/.hermes/hermes-agent && python3.12 -m venv venv && venv/bin/pip install -e .'"
echo "  ssh $VT_VPS_SSH 'mkdir -p ~/.local/bin && printf \"#!/usr/bin/env bash\nexec ~/.hermes/hermes-agent/venv/bin/hermes \\\"\\\$@\\\"\n\" > ~/.local/bin/hermes && chmod +x ~/.local/bin/hermes'"
