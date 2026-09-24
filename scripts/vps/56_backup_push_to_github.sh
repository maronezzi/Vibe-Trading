#!/bin/bash
# =====================================================================
# Vibe-Trading — Backup noturno LOCAL → GITHUB (perna "nuvem" da regra do
# Bruno: cópia na máquina E na nuvem). Sucessor do 55 (era VPS) a partir do
# retorno ao PC — Wave 896. Roda NO PC (cron 19:40 seg-sex no crontab.txt).
#
# Repo privado: git@github.com:maronezzi/vibe-trading-backup.git
# Clone de trabalho local: ~/Backups/github-runtime-backup
#
#   VAI:   vt_trades.db (snapshot sqlite consistente, mesmo com daemon vivo),
#          vt_config.json, monitoring/reports/, hermes/config.yaml (as chaves
#          são refs ${ENV} — o segredo vive no .env), hermes/cron/jobs.json,
#          hermes/SOUL.md
#   NÃO VAI: hermes/auth.json e hermes/.env (SEGREDOS — ficam só no PC,
#          cobertos pelos backups locais em ~/Backups), hermes/state.db
#          (1,75 GB — acima do limite de 100 MB/arquivo do GitHub), data/
#          (1,3 GB de cache regenerável)
#
# Código NÃO é backup deste repo — o espelho de código continua sendo o
# repo principal (maronezzi/Vibe-Trading), empurrado nos Waves.
#
# Crescimento controlado: commit diário; ao passar de 45 commits, a história
# é compactada num único snapshot e forçada (repo dedicado, force-push seguro).
#
# Tolerante a falhas: avisa no log e sai 0 (tenta de novo amanhã).
# Uso: bash 56_backup_push_to_github.sh [--dry-run]
# =====================================================================
set -uo pipefail
cd "$(dirname "$0")"

DRY=""
case "${1:-}" in
    --dry-run) DRY="--dry-run" ;;
    "") ;;
    *) echo "flags: --dry-run"; exit 1 ;;
esac

PROJECT=/home/bruno/Projects/Vibe-Trading
BACKUP_REPO="git@github.com:maronezzi/vibe-trading-backup.git"
WORK="$HOME/Backups/github-runtime-backup"
SNAP="/tmp/vt_trades_backup_push.db"
LOG_STAMP=$(date '+%F %T')

echo "[$LOG_STAMP] == Backup LOCAL → GitHub (vibe-trading-backup) ${DRY:-} =="

if [[ -n "$DRY" ]]; then
    echo "  [dry-run] snapshot do DB + rsync para $WORK + commit/push"
    exit 0
fi

# ── 1. Clone de trabalho (recria se sumiu) ────────────────────────────
if [[ ! -d "$WORK/.git" ]]; then
    git clone -q "$BACKUP_REPO" "$WORK" 2>/dev/null \
        || { echo "[$LOG_STAMP] ⚠️ clone do repo de backup falhou (ssh?)"; exit 0; }
fi

# ── 2. Snapshot consistente do DB (daemon vivo não é obstáculo) ───────
if python3 - "$PROJECT/vt_trades.db" "$SNAP" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
d = sqlite3.connect(dst)
s.backup(d)
d.close(); s.close()
PY
then
    install -m 644 "$SNAP" "$WORK/vt_trades.db"
else
    echo "[$LOG_STAMP] ⚠️ snapshot sqlite falhou — usando o DB direto"
    install -m 644 "$PROJECT/vt_trades.db" "$WORK/vt_trades.db"
fi
rm -f "$SNAP"
install -m 644 "$PROJECT/vt_config.json" "$WORK/vt_config.json"

# ── 3. Relatórios (pequenos) + estado essencial do hermes ────────────
# install/rsync não criam diretórios-pai (lição da 1ª execução, 24/09)
mkdir -p "$WORK/monitoring/reports" "$WORK/hermes"
rsync -a --delete "$PROJECT/monitoring/reports/" "$WORK/monitoring/reports/"
install -m 600 "$HOME/.hermes/config.yaml"  "$WORK/hermes/config.yaml"   2>/dev/null || true
install -m 600 "$HOME/.hermes/SOUL.md"      "$WORK/hermes/SOUL.md"       2>/dev/null || true
install -m 600 "$HOME/.hermes/cron/jobs.json" "$WORK/hermes/jobs.json"   2>/dev/null || true
# auth.json, .env e state.db NUNCA vão (segredos / >100 MB do GitHub)

# ── 4. Commit + push (com compactação de história > 45 commits) ──────
cd "$WORK" || { echo "[$LOG_STAMP] ⚠️ sem acesso a $WORK"; exit 0; }
git add -A
if git diff --cached --quiet; then
    echo "[$LOG_STAMP] nada mudou desde o último backup"
    exit 0
fi
git commit -q -m "backup $(date '+%F %H:%M') — db $(du -h vt_trades.db | cut -f1), config _version $(python3 -c "import json;print(json.load(open('vt_config.json')).get('_version','?'))" 2>/dev/null || echo '?')"
# Clone de repo vazio pode nascer em master — padroniza em main
git branch -M main

N_COMMITS=$(git rev-list --count HEAD 2>/dev/null || echo 0)
if [[ "$N_COMMITS" -gt 45 ]]; then
    # Compacta: um único snapshot do estado atual (repo dedicado; force seguro)
    git checkout -q --orphan snapshot-tmp
    git add -A
    git commit -q -m "snapshot $(date '+%F %H:%M') — história compactada (>45 backups diários)"
    git branch -M snapshot-tmp main
    git push -q --force origin main 2>/dev/null \
        || echo "[$LOG_STAMP] ⚠️ force-push da história compactada falhou"
else
    git push -q origin main 2>/dev/null \
        || echo "[$LOG_STAMP] ⚠️ push falhou (ssh/rede? tenta amanhã)"
fi

echo "[$LOG_STAMP] == Backup concluído (commit $N_COMMITS→$(git rev-list --count HEAD 2>/dev/null || echo '?')) =="
