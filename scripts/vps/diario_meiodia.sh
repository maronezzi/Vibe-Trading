#!/bin/bash
# =====================================================================
# diario_meiodia.sh — checagem de meio-dia (roda NA VPS, cron 13:35 seg-sex)
# Pós-AGI 12:00: confere se o AGI aplicou algo (sanidade de config), saúde do
# daemon/walker/MT5 e posição corrente. Envia resumo ao Telegram.
# =====================================================================
set -u
PROJECT=/home/bruno/Projects/Vibe-Trading
TARGET="telegram:-1004284773048"
HERMES="$HOME/.local/bin/hermes"
MSG="☀️ MEIO-DIA VPS $(date '+%d/%m %H:%M')\n"

# 1. AGI 12:00 de hoje — aplicou algo?
AGI_LOG=$(ls -t /tmp/vi_agi_v4_*.log /tmp/vt_agi_v4_1[12]*.log 2>/dev/null | head -1)
if [ -n "$AGI_LOG" ] && grep -q "$(date +%Y%m%d)" <<< "$(basename "$AGI_LOG" 2>/dev/null)"; then
    APPLIED=$(grep -oE "applied=[0-9]+" "$AGI_LOG" | tail -1)
    MSG="${MSG}AGI 12h: ${APPLIED:-sem linha} ($(basename "$AGI_LOG"))\n"
fi
VERSAO=$(python3 -c "import json; print(json.load(open('$PROJECT/vt_config.json')).get('_version'))" 2>/dev/null)
MSG="${MSG}Config: v${VERSÃO:-?} (se volume/params mudarem, conferir diff do snapshot)\n"

# 2. Daemon + posição
if pgrep -f "vt_autotrader.py" >/dev/null 2>&1; then
    SYNC=$(grep "MT5_SYNC" /tmp/vt_autotrader.log 2>/dev/null | tail -1 | cut -c1-105)
    MSG="${MSG}Daemon: ✅ | ${SYNC}\n"
else
    MSG="${MSG}Daemon: ❌ MORTO às $(date '+%H:%M') (self-heal deveria cuidar — investigar)\n"
fi

# 3. Walker + cooldown
if pgrep -f "forward_walker.py" >/dev/null 2>&1; then
    SIMS=$(sqlite3 "$PROJECT/vt_trades.db" "SELECT count(*) FROM forward_sim_trades WHERE entry_time LIKE \"$(date +%Y-%m-%d)%\";" 2>/dev/null)
    CB=$(grep -c "\[COOLDOWN\]" /tmp/vt_forward_walker.log 2>/dev/null)
    MSG="${MSG}Walker: ✅ | sims hoje: ${SIMS:-?} | bloqueios cooldown: ${CB:-0}\n"
else
    MSG="${MSG}Walker: ❌ MORTO\n"
fi

# 4. MT5 + gateway
AUTH=$(iconv -f UTF-16LE -t UTF-8 "$HOME/.wine/drive_c/Program Files/MetaTrader 5 Terminal/logs/$(date +%Y%m%d).log" 2>/dev/null | grep -i "authorized" | tail -1 | grep -oE "ping: [0-9.]+ ms")
MSG="${MSG}MT5: $([ -n "$AUTH" ] && echo "✅ ($AUTH)" || echo '❌ sem login')\n"
GS=$(python3 -c "import json; s=json.load(open('$HOME/.hermes/gateway_state.json')); print(s['gateway_state'], s['platforms']['telegram']['state'])" 2>/dev/null)
MSG="${MSG}Gateway: ${GS:-?}\n"

echo -e "$MSG"
$HERMES send -t "$TARGET" "$(echo -e "$MSG" | tr -d '\\n' | head -c 3500)" >/dev/null 2>&1 || echo "[aviso] hermes send falhou"
