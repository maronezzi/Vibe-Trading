#!/bin/bash
# fw_collect_day.sh — coletor read-only do forward walker (VPS-M4, 2026-09-03).
# Substitui o wrapper em background do job hermes (morria quando o scope do
# job era recolhido — lição cgroups). Cron 17:05 seg-sex: 4 min após o
# deadline do walker (17:01), coleta o dia para o output-dir do job
# (id estável 03b055c13731) e espelha em data/forward_journal/.
set -u
PROJECT=/home/bruno/Projects/Vibe-Trading
D=$(date +%Y%m%d)
COLLECT=/tmp/fw_collect_$D.py
OUTDIR=/home/bruno/.hermes/cron/output/03b055c13731

[ -f "$COLLECT" ] || { echo "[$(date '+%F %T')] coletor $COLLECT ainda não gerado — nada a fazer"; exit 0; }

mkdir -p "$OUTDIR" "$PROJECT/data/forward_journal"
cd "$PROJECT"
"$PROJECT/.venv/bin/python" "$COLLECT" "$(date +%F)" > "$OUTDIR/collect_$D.json" 2> "$OUTDIR/collect_err.txt"
rc=$?
cp -a "$OUTDIR/collect_$D.json" "$PROJECT/data/forward_journal/collect_$D.json" 2>/dev/null || true
echo "[$(date '+%F %T')] coletor exit=$rc"
exit $rc
