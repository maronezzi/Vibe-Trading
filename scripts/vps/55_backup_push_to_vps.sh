#!/bin/bash
# =====================================================================
# Vibe-Trading — Backup noturno LOCAL → VPS (o reverso do espelho 50)
# Roda NO PC (cron 19:40 seg-sex no crontab.txt, a partir do retorno 70).
# Regra do Bruno: cópia do runtime na máquina E na nuvem. Após o retorno
# (70_cutover_to_local.sh), quem manda no runtime é o PC e a VPS vira
# cold standby + destino do backup (~/Backups/local-mirror/ na VPS).
# Nada de trading roda lá — p/ voltar à VPS, o kit 20/30/40 segue intacto.
#   - DB: snapshot consistente via sqlite3 (python stdlib) antes do push —
#     funciona mesmo com o daemon vivo (o 50 aceitava rsync cru do DB vivo)
#   - config/strategies/reports/hermes-essencial: rsync direto (PC manda)
#   - state.db do hermes NÃO vai (pesado e churn alto — mesma escolha do 50)
# Tolerante a VPS indisponível: avisa no log e sai 0 (backup tenta de novo amanhã).
# Uso: bash 55_backup_push_to_vps.sh [--dry-run]
# =====================================================================
set -uo pipefail
cd "$(dirname "$0")"
. ./vps.conf

DRY=""
case "${1:-}" in
    --dry-run) DRY="--dry-run" ;;
    "") ;;
    *) echo "flags: --dry-run"; exit 1 ;;
esac

LOG_STAMP=$(date '+%F %T')
SNAP="/tmp/vt_trades_backup_push.db"
DEST_ROOT="Backups/local-mirror"

echo "[$LOG_STAMP] == Backup LOCAL → $VT_VPS_SSH:$DEST_ROOT ${DRY:-} =="

# Prepara destino (ignora falha de ssh — os rsyncs abaixo avisam por si)
ssh -o ConnectTimeout=10 "$VT_VPS_SSH" "mkdir -p ~/$DEST_ROOT/hermes" 2>/dev/null \
    || echo "[$LOG_STAMP] ⚠️ VPS inalcançável agora (backup pulado; tenta amanhã)"

# ── 1. DB: snapshot consistente (daemon vivo não é obstáculo) ────────
PUSH_DB=""
if python3 - "$VT_PROJECT/vt_trades.db" "$SNAP" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
d = sqlite3.connect(dst)
s.backup(d)
d.close(); s.close()
PY
then
    PUSH_DB="$SNAP"
else
    echo "[$LOG_STAMP] ⚠️ snapshot sqlite falhou — usando o DB direto (mesmo risco aceito no 50)"
    PUSH_DB="$VT_PROJECT/vt_trades.db"
fi
rsync -a ${DRY} "$PUSH_DB" "${VT_VPS_SSH}:${DEST_ROOT}/vt_trades.db" 2>/dev/null \
    || echo "[$LOG_STAMP] ⚠️ push do DB falhou (VPS desligada? ssh?)"
rm -f "$SNAP"

# ── 2. Runtime do repo (PC manda: --delete aqui é semântica de espelho) ─
rsync -a --delete ${DRY} "$VT_PROJECT/vt_config.json" "${VT_VPS_SSH}:${DEST_ROOT}/" 2>/dev/null
rsync -a --delete ${DRY} --exclude '_pending/' \
    "$VT_PROJECT/strategies/"          "${VT_VPS_SSH}:${DEST_ROOT}/strategies/" 2>/dev/null
rsync -a --delete ${DRY} \
    "$VT_PROJECT/monitoring/reports/"  "${VT_VPS_SSH}:${DEST_ROOT}/reports/" 2>/dev/null \
    || echo "[$LOG_STAMP] ⚠️ parte do repo-runtime falhou"

# ── 3. Hermes essencial (config/auth/persona/jobs — pequeno, diário) ──
rsync -a ${DRY} --exclude 'gateway.*' --exclude '*.sock' --exclude '*.pid' --exclude '*.lock' \
    "$HOME/.hermes/config.yaml" "$HOME/.hermes/auth.json" "$HOME/.hermes/SOUL.md" \
    "${VT_VPS_SSH}:${DEST_ROOT}/hermes/" 2>/dev/null \
  && rsync -a --delete ${DRY} "$HOME/.hermes/cron/" "${VT_VPS_SSH}:${DEST_ROOT}/hermes/cron/" 2>/dev/null \
  || echo "[$LOG_STAMP] ⚠️ backup do hermes falhou"

echo "[$LOG_STAMP] == Backup concluído =="
