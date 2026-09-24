#!/bin/bash
# =====================================================================
# Vibe-Trading — RETORNO: VPS → LOCAL (o switch reverso do 40_cutover)
# Roda NA MÁQUINA LOCAL. Garante o invariante: EXATAMENTE 1 sistema ativo.
#
#   1. Guarda de horário: recusa em pregão (seg-sex 08:30–18:30 BRT) sem --force
#   2. VPS vira cold standby: crontab -r PRIMEIRO (mata agendadores/self-heal/
#      watchdog que reiniciariam o resto), depois processos longos, depois
#      unidades systemd stop+disable — hermes-gateway/mt5-order têm
#      Restart=always: só morrem de verdade via systemctl, nunca por pkill
#   3. Delta final VPS → local: DB + config + strategies (sem --delete: o dev
#      local sobrevive e o git status mostra o que a VPS trouxe) + relatórios
#      + overrides do copilot (decisão ativa da VPS manda)
#   4. Hermes com os DOIS gateways mortos (verificados, não presumidos):
#      backup local → rsync do estado da VPS — o MESMO conjunto que o 21_sync
#      levou para lá (.env, channel_directory, cron, skills, memories, memory,
#      scripts, hooks, plugins, bin) + state.db — lição 409 do §6: o gateway
#      da VPS para ANTES do local subir
#   5. Crontab local ← crontab.txt do REPOSITÓRIO (o 45 errava ao restaurar o
#      backup "mais novo" do cutover, que era o crontab de dev — ver §8)
#   6. MT5 local: unidades systemd se houver sudo sem senha; senão, modo
#      direto (réplica inline do ExecStart do mt5-order — o legado
#      start_mt5linux.sh aponta para o wine64 inexistente e NÃO é usado)
#   7. Gateway hermes local: systemctl --user enable --now hermes-gateway
#   8. Daemon sobe pelo cron 09:00 do próximo pregão (ou --start-now)
#
# ATENÇÃO pkill/pgrep em bloco ssh: o argv do bash -c remoto CONTÉM o texto
# deste script — todo padrão dentro de bloco remoto usa o truque [x]
# (ex. "[v]t_autotrader"), senão o shell se mata/se auto-casa no meio do
# desligamento (BLOCKER da revisão do retorno, 2026-09-24).
#
# Uso: bash 70_cutover_to_local.sh [--start-now] [--force] [--dry-run] [--execute]
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

# ── Guarda de pregão (mesma janela do 40) ────────────────────────────
HOURMIN=$(TZ=America/Sao_Paulo date +%H%M)
DOW=$(TZ=America/Sao_Paulo date +%u)   # 1=seg ... 7=dom
if [[ $DOW -le 5 ]] && (( 10#$HOURMIN >= 10#$VT_GUARD_START && 10#$HOURMIN <= 10#$VT_GUARD_END )) && [[ $FORCE -eq 0 ]]; then
    echo "❌ PREGÃO ABERTO (${HOURMIN} BRT). Retorno só fora dele ou com --force (desaconselhado)."; exit 1
fi

# Alcance da VPS ANTES de qualquer ação (set -e aborta aqui se indisponível).
# ssh -n em TODOS os ssh do script: sem -n o ssh consome o stdin do script
# (engole a confirmação pipeada e o read seguinte morre em EOF sob set -e,
# lição da 1ª tentativa de 2026-09-24).
ssh -n -o ConnectTimeout=10 "$VT_VPS_SSH" 'true'

MT5_MODE=$(sudo -n true 2>/dev/null && echo "unidades systemd (xvfb99+mt5-order)" || echo "modo direto sem systemd (sem sudo -n)")

echo "==============================================================="
echo " RETORNO Vibe-Trading: VPS → LOCAL ($([ $DRY -eq 1 ] && echo SIMULAÇÃO || echo EXECUÇÃO REAL))"
echo "==============================================================="

if [[ $DRY -eq 1 ]]; then
    echo "Simulação: nenhuma ação será executada. Rode com --execute para aplicar."
    echo "  1. backups: crontab dev local + crontab VPS + DB/config/hermes locais → ~/Backups/*"
    echo "  2. VPS: crontab -r → pkill daemon/watcher/walker/self-heal →"
    echo "     systemctl stop+disable hermes-gateway mt5-order xvfb99 → conferência dura"
    echo "  3. delta VPS→local: vt_trades.db, vt_config.json, strategies (sem --delete),"
    echo "     monitoring/reports, overrides do copilot (se houver)"
    echo "  4. hermes: conjunto completo do 21 (.env, channel_directory, cron, skills,"
    echo "     memories, memory, scripts, hooks, plugins, bin) + state.db da VPS → local"
    echo "  5. crontab local ← crontab.txt do repositório"
    echo "  6. MT5 local: $MT5_MODE"
    echo "  7. gateway hermes local: systemctl --user enable --now hermes-gateway"
    echo "  8. $( [[ $START_NOW -eq 1 ]] && echo 'start_autotrader.sh AGORA' || echo 'daemon sobe pelo cron 09:00 do próximo pregão' )"
    echo "  9. validação dupla (local+VPS) + mensagem de teste no Telegram"
    exit 0
fi

read -r -p "Digite VOLTAR para confirmar o retorno (VPS PARA, LOCAL assume): " CONF || CONF=""
[[ "$CONF" == "VOLTAR" ]] || { echo "abortado."; exit 1; }

TS=$(ts)
mkdir -p "$HOME/Backups"

echo "── [1/9] Backups de segurança (crontabs + runtime local atual) ──"
crontab -l > "$HOME/Backups/crontab.local.dev.pre_retorno.$TS" 2>/dev/null || true
ssh -n "$VT_VPS_SSH" 'crontab -l' > "$HOME/Backups/crontab.vps.pre_retorno.$TS" 2>/dev/null || true
cp -a "$VT_PROJECT/vt_trades.db"   "$HOME/Backups/vt_trades.db.pre_retorno.$TS"
cp -a "$VT_PROJECT/vt_config.json" "$HOME/Backups/vt_config.json.pre_retorno.$TS"
echo "  backups em ~/Backups/*.$TS"

echo "── [2/9] VPS → cold standby (nada reinicia lá) ──"
ssh -n "$VT_VPS_SSH" '
    crontab -r 2>/dev/null && echo "  crontab-VPS-removido" || echo "  crontab-VPS-ja-vazio"
    # Truque [x] OBRIGATÓRIO: o argv deste bash -c contém os padrões abaixo
    pkill -f "[v]t_autotrader.py"            2>/dev/null || true
    pkill -f "[v]t_trade_event_watcher.py"   2>/dev/null || true
    pkill -f "[f]orward_walker"              2>/dev/null || true
    pkill -f "[v]t_self_heal.py"             2>/dev/null || true
    sleep 2
    # Restart=always exige stop/disable via systemd (pkill sozinho ressuscita)
    sudo -n systemctl stop    hermes-gateway.service mt5-order.service xvfb99.service 2>/dev/null || true
    sudo -n systemctl disable hermes-gateway.service mt5-order.service xvfb99.service 2>/dev/null || true
    sudo -n systemctl reset-failed hermes-gateway.service mt5-order.service xvfb99.service 2>/dev/null || true
    pkill -f "[h]ermes_cli.main gateway run" 2>/dev/null || true
    sleep 3
    pkill -f "[t]erminal64.exe" 2>/dev/null || true
    sleep 3
    pkill -f "[X]vfb :99" 2>/dev/null || true
    pkill -f "[w]ineserver" 2>/dev/null || true
    sleep 2
    echo "  — conferência VPS (tudo deve estar morto):"
    for pat in "[v]t_autotrader" "[v]t_trade_event_watcher" "[f]orward_walker" "[v]t_self_heal" "[h]ermes_cli.main gateway" "[t]erminal64" "[X]vfb :99"; do
        if pgrep -f "$pat" >/dev/null; then echo "    ⚠️ AINDA VIVO: $pat"; else echo "    ok morto: $pat"; fi
    done
'
# Checagem dura: QUALQUER sobrevivente de trading aborta ANTES de tocar no local
if ssh -n "$VT_VPS_SSH" 'pgrep -f "[v]t_autotrader|[h]ermes_cli.main gateway|[t]erminal64|[f]orward_walker|[v]t_self_heal|[v]t_trade_event_watcher|[X]vfb :99" >/dev/null'; then
    echo "❌ VPS: processo de trading sobreviveu — ABORTADO (nada foi alterado no local)."
    exit 1
fi
echo "  VPS fria e confirmada."

echo "── [3/9] Delta final VPS → LOCAL (o runtime do PC passa a mandar) ──"
rsync -a "${VT_VPS_SSH}:${VT_PROJECT}/vt_trades.db"   "$VT_PROJECT/vt_trades.db"
rsync -a "${VT_VPS_SSH}:${VT_PROJECT}/vt_config.json" "$VT_PROJECT/vt_config.json"
# Estratégias promovidas pela AGI na VPS vêm SEM --delete (dev local sobrevive)
rsync -a "${VT_VPS_SSH}:${VT_PROJECT}/strategies/"          "$VT_PROJECT/strategies/"
rsync -a "${VT_VPS_SSH}:${VT_PROJECT}/monitoring/reports/"  "$VT_PROJECT/monitoring/reports/"
if ssh -n "$VT_VPS_SSH" 'test -f /tmp/vt_copilot_overrides.json' 2>/dev/null; then
    rsync -a "${VT_VPS_SSH}:/tmp/vt_copilot_overrides.json" /tmp/vt_copilot_overrides.json
else
    rm -f /tmp/vt_copilot_overrides.json
fi
rm -f /tmp/vt_autotrader_state.json   # estado efêmero do dia NÃO migra
echo "  delta aplicado. Novidades trazidas da VPS (git status):"
git -C "$VT_PROJECT" status --porcelain -- strategies/ | head -20 || true

echo "── [4/9] Hermes: gateways mortos dos DOIS lados (verificado) → estado migra ──"
systemctl --user stop hermes-gateway.service 2>/dev/null || true
pkill -f "[h]ermes_cli.main gateway run" 2>/dev/null || true
sleep 2
if pgrep -f "[h]ermes_cli.main gateway run" >/dev/null; then
    echo "❌ gateway hermes LOCAL ainda vivo — abortado antes de tocar no state.db."
    exit 1
fi
HB="$HOME/Backups/hermes-state.pre_retorno.$TS"
mkdir -p "$HB"
cp -a "$HOME/.hermes/state.db" "$HB/" 2>/dev/null || true
cp -a "$HOME/.hermes/state.db-wal" "$HOME/.hermes/state.db-shm" "$HB/" 2>/dev/null || true
cp -a "$HOME/.hermes/config.yaml" "$HOME/.hermes/auth.json" "$HB/" 2>/dev/null || true
cp -a "$HOME/.hermes/.env" "$HOME/.hermes/SOUL.md" "$HOME/.hermes/channel_directory.json" "$HB/" 2>/dev/null || true
cp -a "$HOME/.hermes/cron" "$HB/cron" 2>/dev/null || true
rsync -a --exclude 'gateway.*' --exclude '*.sock' --exclude '*.pid' --exclude '*.lock' \
    "${VT_VPS_SSH}:/home/bruno/.hermes/config.yaml" \
    "${VT_VPS_SSH}:/home/bruno/.hermes/auth.json" \
    "${VT_VPS_SSH}:/home/bruno/.hermes/.env" \
    "${VT_VPS_SSH}:/home/bruno/.hermes/SOUL.md" \
    "${VT_VPS_SSH}:/home/bruno/.hermes/channel_directory.json" "$HOME/.hermes/"
chmod 600 "$HOME/.hermes/.env" 2>/dev/null || true
# Diretórios de runtime do agente — mesmo conjunto do 21_sync, espelhado
# (VPS manda: 3 semanas de memórias/jobs/skills ativos moram lá)
for DIR in cron skills memories memory scripts hooks plugins bin; do
    rsync -a --delete --exclude '.jobs.lock' \
        "${VT_VPS_SSH}:/home/bruno/.hermes/$DIR/" "$HOME/.hermes/$DIR/" 2>/dev/null \
        || echo "  ⚠️ dir hermes/$DIR não veio (não existe na VPS?)"
done
# state.db (~1,7 GB): histórico/continuidade do chat — gateway morto = cópia
# consistente. WAL/-shm locais velhos são removidos ANTES (se o fallback
# só-DB disparar, um WAL estranho seria recuperado pelo sqlite e enxertado
# no DB novo — achado N1 da re-review); fallback só-DB se -wal/-shm faltarem.
rm -f "$HOME/.hermes/state.db-wal" "$HOME/.hermes/state.db-shm"
rsync -a "${VT_VPS_SSH}:/home/bruno/.hermes/state.db" \
          "${VT_VPS_SSH}:/home/bruno/.hermes/state.db-wal" \
          "${VT_VPS_SSH}:/home/bruno/.hermes/state.db-shm" "$HOME/.hermes/" 2>/dev/null \
  || rsync -a "${VT_VPS_SSH}:/home/bruno/.hermes/state.db" "$HOME/.hermes/"
echo "  hermes migrado (backup prévio do estado local em $HB)."

echo "── [5/9] Crontab local ← crontab.txt do repositório ──"
crontab "$VT_PROJECT/crontab.txt" && echo "  crontab de produção instalado (fonte: repositório)."

echo "── [6/9] MT5 local ($MT5_MODE) ──"
if sudo -n true 2>/dev/null; then
    sudo -n install -m 644 "$VT_PROJECT/scripts/vps/systemd/xvfb99.service"    /etc/systemd/system/
    sudo -n install -m 644 "$VT_PROJECT/scripts/vps/systemd/mt5-order.service" /etc/systemd/system/
    sudo -n systemctl daemon-reload
    sudo -n systemctl enable --now xvfb99.service mt5-order.service
    sleep 12
else
    # Sem sudo: réplica inline do ExecStart do mt5-order.service. O fallback
    # "legado" do start_mt5linux.sh está stale (wine64 inexistente) — não usar.
    pkill -f "[X]vfb :99" 2>/dev/null || true
    rm -f /tmp/.X99-lock
    nohup /usr/bin/Xvfb :99 -screen 0 1920x1080x24 -ac >/tmp/xvfb.log 2>&1 &
    sleep 2
    DISPLAY=:99 WINEPREFIX=/home/bruno/.wine WINEDEBUG=-all HOME=/home/bruno \
        nohup /usr/bin/wine "/home/bruno/.wine/drive_c/Program Files/MetaTrader 5 Terminal/terminal64.exe" /portable \
        >/tmp/vt_mt5_direto.log 2>&1 &
    sleep 12
fi
pgrep -f "[t]erminal64" >/dev/null && echo "  MT5 vivo (terminal64)." || echo "  ⚠️ terminal64 NÃO subiu — ver /tmp/vt_mt5_direto.log e jurnal do wine."

echo "── [7/9] Gateway Hermes local (único do planeta agora) ──"
systemctl --user enable --now hermes-gateway.service >/dev/null 2>&1 || true
sleep 5
pgrep -f "[h]ermes_cli.main gateway run" >/dev/null \
    && echo "  gateway hermes vivo (unit tem Restart=always: se o Telegram ainda segurar a sessão da VPS, ele reinicia até vencer — lição 409)." \
    || echo "  ⚠️ gateway NÃO subiu — journalctl --user -u hermes-gateway -n 30"

echo "── [8/9] Daemon local ──"
if [[ $START_NOW -eq 1 ]]; then
    bash "$VT_PROJECT/scripts/start_autotrader.sh" >> /tmp/vt_start.log 2>&1
else
    echo "  daemon NÃO inicia agora — cron 09:00 (seg-sex) assume no próximo pregão."
fi

echo "── [9/9] Validação final dupla ──"
echo "  — LOCAL:"
pgrep -f "[t]erminal64" >/dev/null && echo "    MT5: vivo" || echo "    MT5: ⚠️ MORTO"
pgrep -f "[h]ermes_cli.main gateway run" >/dev/null && echo "    gateway hermes: vivo" || echo "    gateway hermes: ⚠️ MORTO"
if diff <(crontab -l) "$VT_PROJECT/crontab.txt" >/dev/null 2>&1; then
    echo "    crontab: idêntico ao repositório"
else
    echo "    crontab: ⚠️ difere do repositório"
fi
if pgrep -f "[v]t_autotrader.py" >/dev/null; then
    echo "    daemon: vivo (--start-now)"
else
    echo "    daemon: aguarda cron 09:00 (esperado sem --start-now)"
fi
echo "  — VPS (deve estar tudo morto/vazio):"
ssh -n "$VT_VPS_SSH" '
    echo "    crontab: $(crontab -l 2>/dev/null | wc -l) linhas"
    echo "    unidades: $(systemctl is-active hermes-gateway mt5-order xvfb99 2>/dev/null | tr "\n" " ")"
    echo "    processos de trading: $(pgrep -f "[v]t_autotrader|[t]erminal64|[h]ermes_cli.main gateway" 2>/dev/null | wc -l)"
'
# Mensagem de teste no Telegram (transport-only, mesmo padrão dos diários; best-effort)
"$HOME/.local/bin/hermes" send -t "telegram:-1004284773048" \
    "✅ Vibe-Trading de volta ao PC — sistema único = LOCAL. MT5+gateway ativos; daemon sobe 09:00 pelo cron. VPS em cold standby (backup 19:40)." \
    >/dev/null 2>&1 || echo "  ⚠️ mensagem de teste no Telegram falhou (best-effort)"

echo
echo "==============================================================="
echo " RETORNO CONCLUÍDO. Sistema único agora = LOCAL (PC)."
echo " VPS = cold standby + destino do backup noturno (55, 19:40)."
echo " Próximo pregão: pre-flight 08:55, daemon 09:00 (cron)."
echo " Para voltar à VPS um dia: kit 20/30/40 segue intacto."
echo "==============================================================="
