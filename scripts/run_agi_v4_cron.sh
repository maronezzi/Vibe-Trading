#!/bin/bash
# run_agi_v4_cron.sh — Wrapper para cron 17:10 do AGI v4 (Wave 13.5+).
#
# Replica o workflow manual do Bruno (Wave 13.5/14.1):
#   1. Snapshot do config antes de qualquer coisa (rollback de segurança)
#   2. Roda AGI v4 em background com --days 7 --max-iterations 2
#   3. Log diário em /tmp/vt_agi_v4_YYYYMMDD.log (substitui o append eterno)
#   4. AGI v4 LIVE (sem --dry-run): Stage 5 auto-aplica candidatos que
#      passam regra1 (candidate_pnl > baseline_pnl). É exatamente o
#      comportamento que o cron já fazia — agora com args corretos.
#
# Por que async (nohup ... &)?
#   - AGI v4 com grids expandidos leva ~30-60min (Wave 14.1: 1h31m)
#   - Cron subsequente às 17:30 (loser_replay) e 19:00 (rescan) não
#     conflitam com AGI em andamento
#   - Cron não bloqueia esperando o AGI terminar
#
# Uso:
#   bash scripts/run_agi_v4_cron.sh           # run normal (LIVE)
#   bash scripts/run_agi_v4_cron.sh --dry-run # só análise (não aplica)
#
# Crontab (crontab.txt):
#   10 17 * * 1-5 bash /home/bruno/Projects/Vibe-Trading/scripts/run_agi_v4_cron.sh

set -euo pipefail

PROJECT_ROOT="/home/bruno/Projects/Vibe-Trading"
RUNNER="${PROJECT_ROOT}/optimization/agi_v4/runner.py"
CONFIG="${PROJECT_ROOT}/vt_config.json"
TS=$(date +%Y%m%d_%H%M%S)
LOG="/tmp/vt_agi_v4_${TS}.log"
LATEST_LINK="/tmp/vt_agi_v4_latest.log"
PID_FILE="/tmp/vt_agi_v4.pid"

# Args (replica Wave 13.5/14.1)
DRY_RUN=""
if [ "${1:-}" = "--dry-run" ]; then
  DRY_RUN="--dry-run"
fi

# Lock guard (evita rodar 2x em paralelo se cron disparar 2 vezes)
if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "[$(date)] AGI v4 já em execução (PID $OLD_PID) — abortando nova run" >> "$LOG"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

# Snapshot do config (rollback de segurança — Bruno sempre fez manual)
SNAPSHOT="${CONFIG}.snapshot_pre_cron_${TS}"
cp "$CONFIG" "$SNAPSHOT"
echo "[$(date)] Snapshot: $SNAPSHOT" > "$LOG"

# Wave 882 (Bruno 04/08): cron das 12h roda durante o pregão (mercado aberto
# 09h-18h). O autotrader + MT5 + watchdog precisam de CPU headroom. Limita
# workers a 1 (vs 2 do noturno) e encurta o deadline para 2h (vs 8h do 17h10)
# — o cron do meio-dia não tem a madrugada e não pode monopolizar a CPU.
# O cron das 17h10 (pós-close, 16:45) roda sem essas restrições.
# Wave AGI-backfill (Bruno 16/08): backfill_intel (calibração autônoma de
# time_blocks por replay histórico) está em modo ANÁLISE-ONLY — o auto-apply
# é OFF por padrão no código. Decisão após walk-forward out-of-sample
# INCONCLUSIVO (+R$87 prometido na descoberta, +R$7 entregue na validação
# cega). Para reativar o auto-apply, descomente a linha abaixo:
# export VT_BACKFILL_INTEL=1
# Análise manual (sempre dry-run): python3 optimization/agi_v4/backfill_intel.py
HOUR=$(date +%H)
if [ "$HOUR" -ge 11 ] && [ "$HOUR" -le 14 ]; then
  export VT_MAX_WORKERS=1
  export VT_AGI_DEADLINE_MINS=120
  echo "[$(date)] Cron diurno (hora=$HOUR): VT_MAX_WORKERS=1, deadline=120min (CPU limitada p/ não atrapalhar pregão)" >> "$LOG"
else
  echo "[$(date)] Cron noturno (hora=$HOUR): sem limite de CPU (pós-close, deadline=480min)" >> "$LOG"
fi

# Run async
#
# Wave LLM-AGI (Bruno 17/07): removido o cap --max-iterations 2. O AGI agora
# roda com o MÁXIMO de iterações possível — para por CONVERGÊNCIA (todo par
# lucrativo), ESTAGNAÇÃO (3 iterações sem progresso) ou DEADLINE de 90 min
# (teto de segurança no pipeline). Antes o cap artificial cortava precoce:
# com 4+ pares failing, 2 iterações não davam tempo do LLM gerar estratégias.
# Os crons seguintes (loser_replay 17:30, rescan 19:00) não leem/escrevem o
# config, então não há race condition — só sobreposição de CPU (aceitável).
#
# Wave 880.C4/noturno-generoso (Bruno 01/08): --mode auto detecta horário.
# AMBOS os crons (12:00 e 17:10) rodam o loop de convergência COMPLETO.
# Bruno 01/08: "tempo não é problema" — deadline interno subiu para 8h
# (VT_AGI_DEADLINE_MINS=480) e estagnação para 3 iterações. O AGI roda às
# 17:10 com a madrugada toda para testar exaustivamente e gerar estratégias.
{
  echo "[$(date)] AGI v4 cron start | args: --days 7 --mode auto $DRY_RUN (deadline 8h, estagnação 3 it, max-iterations 1000)"
  cd "$PROJECT_ROOT"
  export PYTHONPATH="$PROJECT_ROOT"
  /home/bruno/Projects/Vibe-Trading/.venv/bin/python3 "$RUNNER" --days 7 --mode auto $DRY_RUN
  EXIT_CODE=$?
  echo "[$(date)] AGI v4 cron end | exit=$EXIT_CODE | snapshot=$SNAPSHOT"
  rm -f "$PID_FILE"
} >> "$LOG" 2>&1 &

AGI_PID=$!
echo "$AGI_PID" > "$PID_FILE"
ln -sf "$LOG" "$LATEST_LINK"
echo "[$(date)] AGI v4 PID=$AGI_PID log=$LOG snapshot=$SNAPSHOT"