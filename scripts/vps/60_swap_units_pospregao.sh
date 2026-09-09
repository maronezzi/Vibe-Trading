#!/bin/bash
# =====================================================================
# Vibe-Trading — VPS-M3 (etapa 2): troca transiente → permanente das
# unidades systemd (xvfb99, mt5-order) na VPS. Rode APÓS o pregão
# (depois das 18:30) — derruba o MT5 por ~60s durante a troca.
# Idempotente: pode rodar mais de uma vez.
# Uso: bash scripts/vps/60_swap_units_pospregao.sh
# =====================================================================
set -uo pipefail
cd "$(dirname "$0")"
. ./vps.conf

echo "== 1. Troca das unidades (MT5 cai e sobe) =="
ssh "$VT_VPS_SSH" '
    sudo systemctl stop mt5-order.service 2>/dev/null
    sudo systemctl stop xvfb99.service 2>/dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable --now xvfb99.service mt5-order.service
    sleep 45
    echo "— serviços (esperado: active active):"
    systemctl is-active xvfb99 mt5-order
    echo "— terminal64:"
    pgrep -f "[t]erminal64" >/dev/null && echo vivo || echo MORTO
'

echo "== 2. Login MT5 (esperado: authorized XPMT5-DEMO, ping ~3ms) =="
ssh "$VT_VPS_SSH" 'iconv -f UTF-16LE -t UTF-8 ~/.wine/drive_c/"Program Files"/"MetaTrader 5 Terminal"/logs/$(date +%Y%m%d).log 2>/dev/null | grep -i authorized | tail -2'

echo "== 3. Alavanca (esperado: 'MT5 vivo', rc=0, <1s) =="
ssh "$VT_VPS_SSH" 'bash ~/Projects/Vibe-Trading/scripts/start_mt5linux.sh; echo rc=$?'

echo "== 4. Linha @reboot do MT5 no crontab vira redundante — removendo =="
ssh "$VT_VPS_SSH" 'crontab -l 2>/dev/null | grep -v "systemctl start xvfb99" | crontab - && echo "@reboot do MT5 removido (o do gateway hermes foi mantido)"'

echo "== 5. Aposentando o legado wine64: rpyc órfão da porta 5001 =="
# O heal falho da manhã (09:0x) deixou um rpyc_classic vivo dentro do wine64
# (WINEPREFIX antigo). wineserver -k mata SÓ os processos do wine64.
ssh "$VT_VPS_SSH" 'WINEPREFIX=/home/bruno/.wine64 /usr/bin/wineserver -k 2>/dev/null; sleep 3; ss -ltn | grep -q :5001 && echo "5001 AINDA ativo (verificar)" || echo "5001 limpo — legado aposentado"'

echo "== Concluído. Se algo quebrou: =="
echo "  revert: ssh vps 'sudo rm /etc/systemd/system/{xvfb99,mt5-order}.service && sudo systemctl daemon-reload'"
