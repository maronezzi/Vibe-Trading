#!/bin/bash
# =============================================================================
# vt_restart_when_flat.sh — Reinicia o autotrader quando TODAS as posições fecharem.
#
# Wave 10/08 (Bruno 2026-08-10): os fixes de código do EMERGENCY CLOSE falso
# (No changes = sucesso em mt5_error_recovery.py + skipped não-emergência em
# vt_emergency.py) só valem no próximo restart do daemon. Este watchdog:
#   1. Só roda em horário de operação (seg-sex 09:00-16:44 BRT)
#   2. Checa posições MT5 via orchestrator (fonte da verdade)
#   3. Tem posição aberta → exit 0 silencioso (cron no_agent não entrega nada)
#   4. Flat → dupla verificação (2 checagens com intervalo) → restart via
#      scripts/start_autotrader.sh (pkill + nohup + watcher) → valida novo PID
#   5. Emite stdout APENAS quando o restart aconteceu (delivery Telegram)
#
# USO (cron no_agent, a cada 5min):
#   */5 9-16 * * 1-5 /home/bruno/Projects/Vibe-Trading/scripts/vt_restart_when_flat.sh
# =============================================================================
set -u

PROJECT="/home/bruno/Projects/Vibe-Trading"
LOG="/tmp/vt_restart_when_flat.log"
HERMES_BIN="$HOME/.local/bin/hermes"
export PATH="$HOME/.local/bin:$PATH"

ts() { date "+%Y-%m-%d %H:%M:%S"; }
# IMPORTANTE: log_line escreve SÓ no arquivo (nunca no stdout). O cron no_agent
# entrega o stdout inteiro — qualquer linha aqui viraria spam no Telegram a cada
# tick. O stdout é reservado EXCLUSIVAMENTE para a mensagem de restart (passo 5).
log_line() { echo "[$(ts)] $1" >> "$LOG"; }

# === 1. Janela de operação (seg-sex 09:00-17:00) ===
# 17:00 inclusive: o EOD do autotrader é 16:45 (fecha posições sozinho) — a
# janela pós-EOD 16:45-17:00 é o momento IDEAL para o restart (tudo flat).
# Depois das 17:00 o mercado fechou e o próximo start_autotrader (amanhã 09:00
# via launcher) já reinicia com o código novo de qualquer forma.
HOUR=$(date +%H); MIN=$(date +%M); DOW=$(date +%u)
if [ "$DOW" -gt 5 ] || [ "$HOUR" -lt 9 ] || [ "$HOUR" -ge 17 ]; then
    exit 0
fi

# === 2. Conta posições MT5 (broker truth) ===
count_positions() {
    cd "$PROJECT" || return 99
    timeout 30 .venv/bin/python -c "
import sys; sys.path.insert(0, '.')
from mt5 import mt5_orchestrator as m
try:
    st = m.status()
    print(len(st.get('positions', []) or []))
except Exception as e:
    print(f'ERR:{e}')
" 2>/dev/null
}

N1=$(count_positions)
case "$N1" in
    ERR:*) log_line "⚠️ status() falhou ($N1) — tenta de novo na próxima tick"; exit 0 ;;
    ''|*[!0-9]*) log_line "⚠️ n_pos inválido ($N1) — tenta de novo na próxima tick"; exit 0 ;;
esac

if [ "$N1" -gt 0 ]; then
    # Ainda tem posição aberta — silêncio (cron no_agent não entrega nada)
    log_line "⏳ $N1 posição(ões) aberta(s) — restart adiado"
    exit 0
fi

# === 3. Flat? Dupla verificação (evita restart no meio de abertura) ===
sleep 20
N2=$(count_positions)
case "$N2" in
    ERR:*|''|*[!0-9]*) log_line "⚠️ 2ª checagem inválida ($N2) — aborta restart"; exit 0 ;;
esac
if [ "$N2" -gt 0 ]; then
    log_line "⏳ 2ª checagem: $N2 posição(ões) — restart abortado"
    exit 0
fi

# === 4. Flat confirmado → restart ===
log_line "🟢 Flat confirmado (2 checagens) — reiniciando autotrader p/ ativar fixes"
RESTART_OUT=$(bash "$PROJECT/scripts/start_autotrader.sh" 2>&1)
RESTART_RC=$?

sleep 5
NEW_PID=$(pgrep -f "core/vt_autotrader.py" | head -1 || echo "")

if [ "$RESTART_RC" -ne 0 ]; then
    MSG="⚠️ *Restart autotrader FALHOU* (rc=$RESTART_RC)
$(echo "$RESTART_OUT" | tail -5)"
else
    MSG="✅ *Autotrader reiniciado* (restart-when-flat)
• PID: ${NEW_PID:-?}
• Fixes ativos: No changes = sucesso + skipped não-emergência
• $(date '+%d/%m %H:%M')"
fi
log_line "Restart concluído: rc=$RESTART_RC pid=${NEW_PID:-?}"

# === 5. Entrega Telegram ===
# O cron no_agent entrega o stdout automaticamente no canal configurado (deliver).
# NÃO usar hermes send aqui — duplicaria a mensagem (dedup do hermes só cobre o
# mesmo target+thread; o deliver do cron é o grupo Vibe-Trading).
echo "$MSG"

exit 0
