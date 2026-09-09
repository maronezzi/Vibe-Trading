#!/bin/bash
# start_mt5linux.sh — garante o MT5 de ordens ativo (Vibe-Trading).
# VPS-M3 (2026-09-03): a versão anterior apontava para um MT5 inexistente no
# ~/.wine64 e travava 60s quando o self-heal invocava a alavanca (alerta
# "auto-cura FALHOU"). Comportamento novo:
#   1. Produção/VPS: unidade systemd mt5-order.service.
#      MT5 vivo  → exit 0 imediato (lentidão não derruba pregão).
#      MT5 morto → systemctl restart/start e espera terminal64 (até 120s).
#   2. Legado (desktop de dev sem a unidade): executa start_mt5linux_legacy.sh.
set -u
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet mt5-order.service 2>/dev/null; then
    if pgrep -f "terminal64.exe /portable" >/dev/null 2>&1; then
        echo "[start_mt5linux] MT5 vivo (mt5-order ativo) — nada a fazer."
        exit 0
    fi
fi
if systemctl list-unit-files 2>/dev/null | grep -q "^mt5-order.service"; then
    echo "[start_mt5linux] MT5 morto — reiniciando unidade systemd mt5-order..."
    # VPS-M4: limpa referência transiente órfã antes do restart — sem isso o
    # restart falha com "Failed to open /run/systemd/transient/..." e o pgrep
    # acha um terminal64 órfão sem login fingindo saúde (2026-09-03 19:50-20:30).
    sudo -n systemctl reset-failed mt5-order.service 2>/dev/null
    sudo -n systemctl daemon-reload 2>/dev/null
    sudo -n systemctl restart mt5-order.service 2>/dev/null || sudo -n systemctl start mt5-order.service
    for i in $(seq 1 24); do
        # Saúde = unidade ATIVA + processo vivo (órfão sem unidade não conta)
        if systemctl is-active --quiet mt5-order.service 2>/dev/null && \
           pgrep -f "terminal64.exe /portable" >/dev/null 2>&1; then
            echo "[start_mt5linux] terminal64 ativo."
            exit 0
        fi
        sleep 5
    done
    echo "[start_mt5linux] ERRO: terminal64 não subiu após o restart." >&2
    exit 1
fi
exec "$(dirname "$0")/start_mt5linux_legacy.sh" "$@"
