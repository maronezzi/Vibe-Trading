#!/bin/bash
# start_forward_walker.sh — Lança o forward_walker (shadow validator live).
#
# O forward_walker replica a lógica de entrada/SL/TP/trailing do autotrader
# contra as barras LIVE do MT5, em memória, SEM enviar ordens. Grava numa
# tabela isolada (forward_sim_trades). O AGI do meio-dia (12:00) lê essa
# tabela como sinal SHADOW do pregão atual ("como o config atual está indo
# hoje até agora"). NÃO substitui a otimização (que é por replay backtest).
#
# Cron: roda 09:01 seg-sex (1 min depois do autotrader 09:00, para o
# autotrader reivindicar o snapshot de starting-balance primeiro e evitar
# side-effect de import em vt_autotrader:709). O --duration-min 480 cobre
# a sessão 09:01→17:01; ao atingir o deadline ele fecha as posições SIM
# e emite relatório final.
#
# Idempotente: se já há um walker vivo (pgrep), não inicia outro.
# Log: /tmp/vt_forward_walker.log (append).
set -euo pipefail

PROJECT_ROOT="/home/bruno/Projects/Vibe-Trading"
LOG="/tmp/vt_forward_walker.log"
PYTHON="${PROJECT_ROOT}/.venv/bin/python3"

# Rode apenas em dia útil dentro do pregão (B3 seg-sex 09-17h).
DOW=$(date +%u)
HOUR=$(date +%H)
if [ "$DOW" -gt 5 ] || [ "$HOUR" -lt 9 ] || [ "$HOUR" -ge 17 ]; then
    echo "[$(date)] fora do pregão (DOW=$DOW HOUR=$HOUR) — não inicia walker" >> "$LOG"
    exit 0
fi

# Idempotência: não inicia se já há um walker vivo.
if pgrep -f "forward_walker.py" > /dev/null; then
    echo "[$(date)] forward_walker já em execução — nada a fazer" >> "$LOG"
    exit 0
fi

cd "$PROJECT_ROOT"
# Duração até ~17:01 (480 min desde 09:01). M5+M15 são os TFs mais ativos e
# já cobrem a maioria dos pares. --no-telegram para evitar spam no início.
echo "[$(date)] iniciando forward_walker (PID a seguir)…" >> "$LOG"
PYTHONUNBUFFERED=1 nohup "$PYTHON" "${PROJECT_ROOT}/optimization/forward_walker.py" \
    --duration-min 480 --tfs M5 M15 \
    >> "$LOG" 2>&1 &
echo "[$(date)] forward_walker PID=$!" >> "$LOG"
disown
