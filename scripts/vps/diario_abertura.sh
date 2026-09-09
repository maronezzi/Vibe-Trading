#!/bin/bash
# =====================================================================
# diario_abertura.sh — checagem de abertura (roda NA VPS, cron 09:20 seg-sex)
# Verifica pre-flight/daemon/MT5/walker/gateway e envia resumo ao Telegram.
# Read-only: não opera, não muda config. Reinicia o walker apenas se houver
# traceback no código novo de cooldown.
# =====================================================================
set -u
PROJECT=/home/bruno/Projects/Vibe-Trading
TARGET="telegram:-1004284773048"
HERMES="$HOME/.local/bin/hermes"
MSG="🌅 ABERTURA VPS $(date '+%d/%m %H:%M')\n"

# 1. Pre-flight
PF=$(tail -8 /tmp/vt_pre_flight.log 2>/dev/null | grep -c "PRE-FLIGHT OK")
MSG="${MSG}Pre-flight: $([ "$PF" -ge 1 ] && echo '✅ OK' || echo '❌ sem OK no log')\n"

# 2. Daemon
if pgrep -f "vt_autotrader.py" >/dev/null 2>&1; then
    SYNC=$(grep "MT5_SYNC" /tmp/vt_autotrader.log 2>/dev/null | tail -1 | cut -c1-100)
    ERRS=$(grep -ciE "traceback" /tmp/vt_autotrader.log 2>/dev/null)
    MSG="${MSG}Daemon: ✅ vivo | ${SYNC}\n"
    MSG="${MSG}Tracebacks no log: ${ERRS}\n"
else
    MSG="${MSG}Daemon: ❌ MORTO às $(date '+%H:%M')\n"
fi

# 3. MT5 sessão
AUTH=$(iconv -f UTF-16LE -t UTF-8 "$HOME/.wine/drive_c/Program Files/MetaTrader 5 Terminal/logs/$(date +%Y%m%d).log" 2>/dev/null | grep -i "authorized" | tail -1 | grep -oE "ping: [0-9.]+ ms" )
MSG="${MSG}MT5: $([ -n "$AUTH" ] && echo "✅ autorizado ($AUTH)" || echo '❌ sem autorização hoje')\n"

# 4. Walker (código de cooldown novo — primeiro dia)
if pgrep -f "forward_walker.py" >/dev/null 2>&1; then
    CB=$(grep -c "\[COOLDOWN\]" /tmp/vt_forward_walker.log 2>/dev/null)
    MSG="${MSG}Walker: ✅ vivo | bloqueios cooldown: ${CB:-0}\n"
else
    MSG="${MSG}Walker: ❌ MORTO\n"
fi

# 5. Gateway
GS=$(python3 -c "import json; s=json.load(open('$HOME/.hermes/gateway_state.json')); print(s['gateway_state'], s['platforms']['telegram']['state'])" 2>/dev/null)
MSG="${MSG}Gateway hermes: ${GS:-indisponível}\n"

# Ação corretiva automática: walker morto SEM traceback de código → relança
if ! pgrep -f "forward_walker.py" >/dev/null 2>&1; then
    if ! grep -qiE "traceback|_sim_cooldown" /tmp/vt_forward_walker.log 2>/dev/null; then
        bash "$PROJECT/scripts/start_forward_walker.sh" >> /tmp/vt_forward_walker.log 2>&1
        MSG="${MSG}🔧 walker relançado automaticamente\n"
    fi
fi

echo -e "$MSG"
$HERMES send -t "$TARGET" "$(echo -e "$MSG" | tr -d '\\n' | head -c 3500)" >/dev/null 2>&1 || echo "[aviso] hermes send falhou"
