#!/bin/bash
# Vibe-Trading autotrader starter — chamado pelo cron às 09:00 weekdays.
# Garante: MT5 up + autotrader rodando limpo (pkill primeiro pra evitar duplicatas).
# Idempotente: pode rodar múltiplas vezes sem efeito colateral.

set -e

PROJECT="/home/bruno/Projects/Vibe-Trading"
LOG="/tmp/vt_autotrader.log"
HERMES_BIN="$HOME/.local/bin/hermes"

ts() { date "+%Y-%m-%d %H:%M:%S"; }
log_line() { echo "[$(ts)] $1" | tee -a "$LOG"; }

# === 1. Mate instância anterior (idempotência) ===
if pgrep -f "vt_autotrader.py" > /dev/null; then
    log_line "🛑 Autotrader antigo rodando — matando"
    pkill -f "vt_autotrader.py" || true
    sleep 2
fi

# === 2. Verifique MT5 (sem reiniciar — quem cuida é o start_mt5linux.sh) ===
if ! pgrep -f "terminal64.exe" > /dev/null; then
    log_line "⚠️ MT5 não está rodando — tentando iniciar"
    bash "$PROJECT/scripts/start_mt5linux.sh" >> "$LOG" 2>&1 || true
    sleep 10
fi

# === 3. Limpa state file stale (defesa) ===
if [ -f /tmp/vt_autotrader_state.json ]; then
    STATE_DAY=$(python3 -c "import json; print(json.load(open('/tmp/vt_autotrader_state.json')).get('current_day',''))" 2>/dev/null || echo "")
    TODAY=$(date +%Y-%m-%d)
    if [ "$STATE_DAY" != "$TODAY" ]; then
        log_line "📅 State de $STATE_DAY ≠ hoje $TODAY — backup + reset"
        cp /tmp/vt_autotrader_state.json /tmp/vt_autotrader_state.json.bak.$(ts | tr ' :' '_') || true
    fi
fi

# === 4. Iniciar autotrader ===
log_line "🚀 Iniciando autotrader..."
cd "$PROJECT"
PYTHONUNBUFFERED=1 nohup python3 vt_autotrader.py >> "$LOG" 2>&1 &
AUTO_PID=$!
disown
log_line "✅ Autotrader PID: $AUTO_PID"

# === 5. Notificação Telegram ===
sleep 3
if [ -x "$HERMES_BIN" ]; then
    "$HERMES_BIN" send -t telegram "🚀 Vibe-Trading iniciado (PID $AUTO_PID) | $(date '+%H:%M')" 2>/dev/null || true
fi

exit 0
