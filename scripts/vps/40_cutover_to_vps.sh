#!/bin/bash
# =====================================================================
# Vibe-Trading — Fase D: CUTOVER local → VPS (o switch de sistema único)
# Roda NA MÁQUINA LOCAL. Garante o invariante: EXATAMENTE 1 sistema ativo.
#
#   1. Guarda de horário: recusa em pregão (seg-sex 08:30–18:30 BRT) sem --force
#   2. Para TUDO local (daemon, watcher, walker, MT5, gateway hermes) e
#      troca o crontab local pelo de desenvolvimento (nada reinicia sozinho)
#   3. Delta final de estado (código + config + DB + hermes) → VPS
#   4. Na VPS: instala o crontab de produção + sobe bridge MT5 (+ gateway hermes)
#   5. Valida. Com --start-now, sobe o daemon na VPS na hora (senão o cron
#      de 09:00 assume no próximo dia útil)
#   Rollback: scripts/vps/45_rollback_to_local.sh
#
# Uso: bash 40_cutover_to_vps.sh [--start-now] [--force] [--dry-run]
# =====================================================================
set -euo pipefail
cd "$(dirname "$0")"
. ./vps.conf

START_NOW=0; FORCE=0; DRY=1
for a in "$@"; do
    case "$a" in
        --start-now) START_NOW=1 ;;
        --force) FORCE=1 ;;
        --dry-run) DRY=1 ;;            # modo seguro: default DRY, --execute tira
        --execute) DRY=0 ;;
        *) echo "flags: --start-now --force --dry-run --execute"; exit 1 ;;
    esac
done

ts() { date "+%Y%m%d_%H%M%S"; }

# ── Guarda de pregão ─────────────────────────────────────────────────
HOURMIN=$(TZ=America/Sao_Paulo date +%H%M)
DOW=$(TZ=America/Sao_Paulo date +%u)   # 1=seg ... 7=dom
# 10#: "0830" tem zero à esquerda — sem isso o bash trata como octal e explode.
if [[ $DOW -le 5 ]] && (( 10#$HOURMIN >= 10#$VT_GUARD_START && 10#$HOURMIN <= 10#$VT_GUARD_END )) && [[ $FORCE -eq 0 ]]; then
    echo "❌ PREGÃO ABERTO (${HOURMIN} BRT). Cutover só fora dele ou com --force (desaconselhado)."; exit 1
fi

echo "==============================================================="
echo " CUTOVER Vibe-Trading: LOCAL → VPS ($([ $DRY -eq 1 ] && echo SIMULAÇÃO || echo EXECUÇÃO REAL))"
echo "==============================================================="

if [[ $DRY -eq 1 ]]; then
    echo "Simulação: nenhuma ação será executada. Rode com --execute para aplicar."
    echo "  1. backup do crontab local → ~/Backups/crontab.local.pre_cutover.$(ts)"
    echo "  2. crontab local ← crontab.local_dev.txt (espelho + rescan, SEM trading)"
    echo "  3. pkill local: vt_autotrader, vt_trade_event_watcher, forward_walker,"
    echo "     vt_self_heal, terminal64.exe, wineserver, Xvfb :99, gateway hermes"
    echo "  4. delta final: 20_sync --with-config + DB + 21_sync_hermes"
    echo "  5. VPS: crontab ← crontab.txt + extras @reboot; start_mt5linux.sh"
    echo "  6. $( [[ $START_NOW -eq 1 ]] && echo 'start_autotrader.sh na VPS AGORA' || echo 'daemon sobe na VPS pelo cron 09:00 do próximo dia útil' )"
    echo "  7. 30_validate_vps.sh --hermes-test"
    exit 0
fi

read -r -p "Digite MIGRAR para confirmar o cutover (local PARA, VPS assume): " CONF
[[ "$CONF" == "MIGRAR" ]] || { echo "abortado."; exit 1; }

mkdir -p "$HOME/Backups"

echo "── [1/7] Backup do crontab local ──"
crontab -l > "$HOME/Backups/crontab.local.pre_cutover.$(ts)" 2>/dev/null || true

echo "── [2/7] Crontab local ← modo desenvolvimento (trading desligado) ──"
crontab "$VT_PROJECT/scripts/vps/crontab.local_dev.txt"

echo "── [3/7] Parando sistema local (ordem: daemon → watchers → MT5 → hermes) ──"
pkill -f 'vt_autotrader.py'            2>/dev/null || true
pkill -f 'vt_trade_event_watcher.py'   2>/dev/null || true
pkill -f 'forward_walker'              2>/dev/null || true
pkill -f 'vt_self_heal.py'             2>/dev/null || true
sleep 3
pkill -f 'terminal64.exe'              2>/dev/null || true
sleep 3
pkill -f 'Xvfb :99'                    2>/dev/null || true
pkill -f 'wineserver'                  2>/dev/null || true
pkill -f 'hermes_cli.main gateway run' 2>/dev/null || true
sleep 2
if pgrep -f 'vt_autotrader.py|terminal64.exe' >/dev/null; then
    echo "  ⚠️  processo resistiu — verificando..."; pgrep -af 'vt_autotrader|terminal64' || true
fi
echo "  local parado."

echo "── [4/7] Delta final local → VPS (código + config + DB + hermes) ──"
bash "$VT_PROJECT/scripts/vps/20_sync_code_to_vps.sh" --with-config
# DB com escritores mortos = cópia consistente por rsync.
rsync -a "$VT_PROJECT/vt_trades.db" "${VT_VPS_SSH}:${VT_PROJECT}/vt_trades.db"
rm -f /tmp/vt_autotrader_state.json   # estado efêmero do dia NÃO migra
# Overrides do copilot (disabled_symbols/timeframes) são decisões de config
# ativas — migram para a VPS se existirem no momento do cutover.
[[ -f /tmp/vt_copilot_overrides.json ]] && \
    rsync -a /tmp/vt_copilot_overrides.json "${VT_VPS_SSH}:/tmp/vt_copilot_overrides.json" || true
bash "$VT_PROJECT/scripts/vps/21_sync_hermes_to_vps.sh"
# Continuidade do chat: histórico/sessões do gateway migram com o gateway
# parado (local foi pkillado no passo 3) — cópia consistente.
rsync -a "$HOME/.hermes/state.db" "$HOME/.hermes/state.db-wal" \
    "$HOME/.hermes/state.db-shm" "${VT_VPS_SSH}:/home/bruno/.hermes/" 2>/dev/null || \
    rsync -a "$HOME/.hermes/state.db" "${VT_VPS_SSH}:/home/bruno/.hermes/"

echo "── [5/7] VPS: crontab de produção + extras @reboot + bridge MT5 ──"
GEN="$HOME/Backups/crontab.vps.generated.$(ts)"
{
    cat "$VT_PROJECT/crontab.txt"
    cat <<EXTRAS

# ─── EXTRAS VPS (gerado por 40_cutover_to_vps.sh em $(date '+%F %T')) ─────────
#     Reboot-safety da VPS: Xvfb e MT5 sobem como unidades systemd
#     (imunes a logout SSH — lição do logind, ver MIGRACAO_VPS.md §6);
#     o gateway hermes sobe pelo cron @reboot (filho do crond, sobrevive).
@reboot /usr/bin/sudo /bin/systemctl start xvfb99.service mt5-order.service || true
@reboot sleep 20 && /home/bruno/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main gateway run >> /home/bruno/.hermes/logs/gateway_reboot.log 2>&1 &
EXTRAS
} > "$GEN"
scp -q "$GEN" "${VT_VPS_SSH}:/home/bruno/crontab.vps.generated"
ssh "$VT_VPS_SSH" 'crontab /home/bruno/crontab.vps.generated && echo crontab-VPS-instalado'

echo "── subindo bridge MT5 na VPS (unidades systemd idempotentes) ──"
ssh "$VT_VPS_SSH" '
    systemctl is-active --quiet xvfb99.service 2>/dev/null || \
        sudo systemd-run --unit=xvfb99 --property="Restart=on-failure" --property="RestartSec=2" /usr/bin/Xvfb :99 -screen 0 1920x1080x24 -ac
    sleep 2
    systemctl is-active --quiet mt5-order.service 2>/dev/null || \
        sudo systemd-run --unit=mt5-order --uid=bruno \
            --setenv=DISPLAY=:99 --setenv=WINEPREFIX=/home/bruno/.wine \
            --setenv=WINEDEBUG=-all --setenv=HOME=/home/bruno \
            /usr/bin/wine "/home/bruno/.wine/drive_c/Program Files/MetaTrader 5 Terminal/terminal64.exe" /portable
    sleep 10
    ss -ltn | grep -q :5001 && echo bridge-up || echo "bridge-5001-inativa (RPyC é lever quebrado pré-VPS-M3; ordens usam wine direto)"
    pgrep -f "[t]erminal64" >/dev/null && echo mt5-vivo || echo mt5-FALHOU'

echo "── subindo gateway hermes na VPS (único do planeta) ──"
ssh "$VT_VPS_SSH" '
    mkdir -p ~/.hermes/logs
    systemctl is-active --quiet hermes-gateway.service 2>/dev/null || \
        sudo systemd-run --unit=hermes-gateway --uid=bruno \
            --property="Restart=on-failure" --property="RestartSec=5" \
            --setenv=HOME=/home/bruno \
            /home/bruno/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main gateway run
    sleep 3
    pgrep -f "[h]ermes_cli.main gateway run" >/dev/null && echo hermes-up || echo hermes-FALHOU'

echo "── [6/7] Daemon na VPS ──"
if [[ $START_NOW -eq 1 ]]; then
    ssh "$VT_VPS_SSH" 'export PATH="$HOME/.local/bin:$PATH"; cd /home/bruno/Projects/Vibe-Trading && PYTHONUNBUFFERED=1 nohup python3 core/vt_autotrader.py >> /tmp/vt_autotrader.log 2>&1 & sleep 3; pgrep -f vt_autotrader.py >/dev/null && echo daemon-up || echo daemon-FALHOU'
else
    echo "  daemon NÃO iniciado agora — o cron 09:00 (seg-sex) assume no próximo pregão."
fi

echo "── [7/7] Validação final ──"
bash "$VT_PROJECT/scripts/vps/30_validate_vps.sh" --hermes-test || true

echo
echo "==============================================================="
echo " CUTOVER CONCLUÍDO. Sistema único agora = VPS."
echo " Rollback (se necessário): scripts/vps/45_rollback_to_local.sh"
echo " Crontab local anterior: ~/Backups/crontab.local.pre_cutover.*"
echo "==============================================================="
