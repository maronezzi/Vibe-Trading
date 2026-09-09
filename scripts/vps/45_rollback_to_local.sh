#!/bin/bash
# =====================================================================
# Vibe-Trading — ROLLBACK: VPS → LOCAL (voltar ao estado anterior)
# Roda NA MÁQUINA LOCAL. Reverso do 40_cutover:
#   1. Para trading + gateway hermes NA VPS e remove o crontab de lá
#   2. Traz de volta o runtime que mudou na VPS (DB + config)
#   3. Restaura o crontab local de produção (backup do cutover)
#   4. Sobe MT5 local; daemon volta pelo cron 09:00 (ou --start-now)
#
# Uso: bash 45_rollback_to_local.sh [--start-now] [--dry-run] [--execute]
# =====================================================================
set -euo pipefail
cd "$(dirname "$0")"
. ./vps.conf

START_NOW=0; DRY=1
for a in "$@"; do
    case "$a" in
        --start-now) START_NOW=1 ;;
        --dry-run) DRY=1 ;;
        --execute) DRY=0 ;;
        *) echo "flags: --start-now --dry-run --execute"; exit 1 ;;
    esac
done

echo "==============================================================="
echo " ROLLBACK Vibe-Trading: VPS → LOCAL ($([ $DRY -eq 1 ] && echo SIMULAÇÃO || echo EXECUÇÃO REAL))"
echo "==============================================================="

LAST_CRON=$(ls -1t "$HOME"/Backups/crontab.local.pre_cutover.* 2>/dev/null | head -1)
[[ -n "$LAST_CRON" ]] || { echo "❌ sem backup de crontab local em ~/Backups — abortado"; exit 1; }
echo "crontab local a restaurar: $LAST_CRON"

if [[ $DRY -eq 1 ]]; then
    echo "Simulação. Rode com --execute."
    exit 0
fi

read -r -p "Digite VOLTAR para confirmar o rollback: " CONF
[[ "$CONF" == "VOLTAR" ]] || { echo "abortado."; exit 1; }

echo "── [1/4] Parando VPS (trading + hermes; crontab removido) ──"
ssh "$VT_VPS_SSH" '
    pkill -f vt_autotrader.py 2>/dev/null || true
    pkill -f vt_trade_event_watcher.py 2>/dev/null || true
    pkill -f forward_walker 2>/dev/null || true
    pkill -f "hermes_cli.main gateway run" 2>/dev/null || true
    crontab -r 2>/dev/null && echo crontab-VPS-removido || echo crontab-VPS-ja-vazio
'

echo "── [2/4] Trazendo runtime da VPS (DB + config — o que mudou por lá) ──"
rsync -a "${VT_VPS_SSH}:${VT_PROJECT}/vt_trades.db" "$VT_PROJECT/vt_trades.db"
rsync -a "${VT_VPS_SSH}:${VT_PROJECT}/vt_config.json" "$VT_PROJECT/vt_config.json"

echo "── [3/4] Crontab local de produção restaurado ──"
crontab "$LAST_CRON"

echo "── [4/4] Subindo MT5 local (daemon pelo cron 09:00$( [[ $START_NOW -eq 1 ]] && echo ' ou agora' )) ──"
/usr/bin/Xvfb :99 -screen 0 1920x1080x24 -ac >/tmp/xvfb.log 2>&1 &
sleep 2
bash "$VT_PROJECT/scripts/start_mt5linux.sh" >> /tmp/vt_mt5linux_boot.log 2>&1 || true
sleep 8
if [[ $START_NOW -eq 1 ]]; then
    bash "$VT_PROJECT/scripts/start_autotrader.sh" >> /tmp/vt_start.log 2>&1
fi

echo
echo "==============================================================="
echo " ROLLBACK CONCLUÍDO. Sistema único agora = LOCAL."
echo " Lembre: gateway hermes local NÃO é reativado automaticamente"
echo " (evita brigar com nada) — se quiser: nohup ~/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main gateway run &"
echo "==============================================================="
