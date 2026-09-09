#!/bin/bash
# =====================================================================
# test_reboot.sh — teste de sobrevivência a reboot da VPS (roda NO DESKTOP).
# Uso: bash scripts/vps/test_reboot.sh   (FORA do pregão; sem posições)
# Reboota a VPS e valida que TUDO volta sozinho: unidades systemd (Xvfb, MT5),
# gateway hermes (@reboot), login XP — e que o daemon NÃO sobe (é cron 09:00).
# =====================================================================
set -u
VPS="${VT_VPS_SSH:-vps}"
TARGET="telegram:-1004284773048"
HERMES="$HOME/.local/bin/hermes"
say() { echo "$1"; }

say "🔄 [REBOOT-TEST] Rebootando a VPS..."
ssh "$VPS" 'sudo systemctl stop hermes-gateway mt5-order xvfb99 2>/dev/null; sudo systemctl reboot' 2>/dev/null || true
sleep 10

say "⏳ Aguardando a VPS voltar (até 5 min)..."
UP=0
for i in $(seq 1 30); do
    sleep 10
    if ssh -o ConnectTimeout=8 -o BatchMode=yes "$VPS" 'echo up' >/dev/null 2>&1; then UP=1; break; fi
done
[ $UP -eq 1 ] || { say "❌ [REBOOT-TEST] VPS não voltou em 5 min — acessar hPanel (console) URGENTE"; $HERMES send -t "$TARGET" "❌ [REBOOT-TEST] VPS não voltou após reboot — console do hPanel URGENTE" >/dev/null 2>&1; exit 1; }
say "✅ SSH de volta ($(date '+%H:%M'))"

say "⏳ Aguardando serviços + login MT5 (até 3 min)..."
LOGIN_OK=0
for i in $(seq 1 18); do
    sleep 10
    AUTH=$(ssh "$VPS" 'iconv -f UTF-16LE -t UTF-8 ~/.wine/drive_c/"Program Files"/"MetaTrader 5 Terminal"/logs/$(date +%Y%m%d).log 2>/dev/null | grep -i authorized | tail -1' 2>/dev/null)
    echo "$AUTH" | grep -qi "authorized" && { LOGIN_OK=1; break; }
done

SVCS=$(ssh "$VPS" 'systemctl is-active xvfb99 mt5-order hermes-gateway' 2>/dev/null | tr '\n' ' ')
GW=$(ssh "$VPS" 'python3 -c "import json; s=json.load(open(\"/home/bruno/.hermes/gateway_state.json\")); print(s[\"platforms\"][\"telegram\"][\"state\"])"' 2>/dev/null)
WALKER=$(ssh "$VPS" 'pgrep -f "forward_walker.py" >/dev/null && echo VIVO' 2>/dev/null)
AUTH_PING=$(echo "$AUTH" | grep -oE "ping: [0-9.]+ ms")

RES="📊 [REBOOT-TEST] Serviços (xvfb/mt5/gateway): $SVCS | Telegram: ${GW:-?} | MT5: $([ $LOGIN_OK -eq 1 ] && echo "logado ($AUTH_PING)" || echo 'sem login ainda') | Walker: ${WALKER:-morto (esperado)}"
say "$RES"
if echo "$SVCS" | grep -qv active || [ $LOGIN_OK -eq 0 ]; then
    say "⚠️ Algum componente não voltou — diagnóstico: ssh $VPS 'systemctl status xvfb99 mt5-order hermes-gateway'"
    $HERMES send -t "$TARGET" "$RES — ⚠️ ver diagnóstico" >/dev/null 2>&1
    exit 1
fi
$HERMES send -t "$TARGET" "$RES — ✅ sobreviveu ao reboot" >/dev/null 2>&1
exit 0
