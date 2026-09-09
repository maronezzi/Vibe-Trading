#!/bin/bash
# =====================================================================
# test_prd_login.sh — teste bidirecional de credencial PRD (roda NA VPS).
# Uso: bash scripts/vps/test_prd_login.sh   (FORA do pregão; sem posições)
# Sequência: salva estado demo → restaura backup PRD → boot frio → verifica
# login PRD via orchestrator → restaura demo → boot frio → verifica demo.
# NÃO envia ordens. Relatório via Telegram.
# =====================================================================
set -u
CFGDIR="$HOME/.wine/drive_c/Program Files/MetaTrader 5 Terminal/Config"
TARGET="telegram:-1004284773048"
HERMES="$HOME/.local/bin/hermes"
BAK_PRD="$CFGDIR/accounts.dat.bak_prd_20260805"
BAK_DEMO="$CFGDIR/accounts.dat.bak_demo_$(date +%Y%m%d)"
UNIT="mt5-order.service"
say() { echo "$1"; $HERMES send -t "$TARGET" "$1" >/dev/null 2>&1; }

[ -f "$BAK_PRD" ] || { say "❌ [PRD-TEST] backup PRD não encontrado: $BAK_PRD"; exit 1; }
DEMO_LOGIN="52257579"

# Estado do MT5 no momento: precisa sem posição
POS=$(python3 -c "
import sqlite3
print(sqlite3.connect('$HOME/Projects/Vibe-Trading/vt_trades.db').execute(
    \"SELECT count(*) FROM trades WHERE exit_time IS NULL\").fetchone()[0])" 2>/dev/null)
[ "${POS:-0}" = "0" ] || { say "❌ [PRD-TEST] há posição aberta registrada — abortado"; exit 1; }

say "🧪 [PRD-TEST] 1/4 salvando estado demo atual..."
sudo systemctl stop "$UNIT" 2>/dev/null
sleep 3
cp -a "$CFGDIR/accounts.dat" "$BAK_DEMO"

say "🧪 [PRD-TEST] 2/4 restaurando credencial PRD (bak_prd_20260805) + boot frio..."
cp -a "$BAK_PRD" "$CFGDIR/accounts.dat"
sudo systemctl start "$UNIT"
PRD_OK=0
for i in $(seq 1 30); do
    sleep 10
    AUTH=$(iconv -f UTF-16LE -t UTF-8 "$HOME/.wine/drive_c/Program Files/MetaTrader 5 Terminal/logs/$(date +%Y%m%d).log" 2>/dev/null | grep -i authorized | tail -1)
    if echo "$AUTH" | grep -qiE "XPMT5-PRD|PRD"; then PRD_OK=1; break; fi
    echo "$AUTH" | grep -qi "authorized" && break   # logou mas em outra conta/server
done
LOGIN_VISTO=$(echo "$AUTH" | grep -oE "'[0-9]+'" | tr -d "'" | head -1)
if [ "$PRD_OK" -eq 1 ]; then
    say "✅ [PRD-TEST] login PRD funcionou ($LOGIN_VISTO, ping: $(echo "$AUTH" | grep -oE 'ping: [0-9.]+ ms')). Conta real acessável da VPS."
else
    say "⚠️ [PRD-TEST] login PRD NÃO confirmado (visto: ${LOGIN_VISTO:-nada}). Pode precisar de login manual via VNC (scripts/mt5_show.sh) ou credencial expirada. Restaurando demo..."
fi

say "🧪 [PRD-TEST] 3/4 restaurando login DEMO (estado original)..."
sudo systemctl stop "$UNIT" 2>/dev/null
sleep 3
cp -a "$BAK_DEMO" "$CFGDIR/accounts.dat"
sudo systemctl start "$UNIT"
DEMO_OK=0
for i in $(seq 1 30); do
    sleep 10
    AUTH=$(iconv -f UTF-16LE -t UTF-8 "$HOME/.wine/drive_c/Program Files/MetaTrader 5 Terminal/logs/$(date +%Y%m%d).log" 2>/dev/null | grep -i authorized | tail -1)
    echo "$AUTH" | grep -q "$DEMO_LOGIN" && { DEMO_OK=1; break; }
done
say "🧪 [PRD-TEST] 4/4 demo restaurada: $([ $DEMO_OK -eq 1 ] && echo '✅ login 52257579 ativo' || echo '❌ VERIFICAR via scripts/mt5_show.sh')"
say "📋 [PRD-TEST] Resultado: PRD=$([ $PRD_OK -eq 1 ] && echo ACESSÁVEL || echo 'REQUER AÇÃO MANUAL') | Demo restaurada=$([ $DEMO_OK -eq 1 ] && echo SIM || echo NÃO)"
[ $DEMO_OK -eq 1 ] || exit 1
