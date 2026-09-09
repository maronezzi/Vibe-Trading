#!/bin/bash
# =====================================================================
# diario_fechamento.sh — raio-X pós-pregão (roda NA VPS, cron 18:35 seg-sex)
# Confere coletor 17:05, minera as sims do dia (SL-only/MFE/cooldowns),
# compara forward vs live e envia o resumo ao Telegram.
# =====================================================================
set -u
PROJECT=/home/bruno/Projects/Vibe-Trading
TARGET="telegram:-1004284773048"
HERMES="$HOME/.local/bin/hermes"
DB="$PROJECT/vt_trades.db"
D=$(date +%Y-%m-%d)
MSG="🌙 FECHAMENTO VPS $(date '+%d/%m %H:%M')\n"

# 1. Coletor 17:05 (primeiro dia: 2026-09-04)
COL="$HOME/.hermes/cron/output/03b055c13731/collect_$(date +%Y%m%d).json"
if [ -f "$COL" ]; then
    MSG="${MSG}Coletor 17:05: ✅ $(wc -c < "$COL") bytes\n"
else
    MSG="${MSG}Coletor 17:05: ❌ sem $COL (ver /tmp/vt_fw_collect.log)\n"
fi

# 2. Live do dia
LIVE=$(sqlite3 "$DB" "SELECT count(*)||' trades, '||sum(CASE WHEN net_pnl>0 THEN 1 ELSE 0 END)||'W, R$'||round(sum(net_pnl),2) FROM trades WHERE date(entry_time)='$D';" 2>/dev/null)
MSG="${MSG}Live: ${LIVE:-sem dados}\n"

# 3. Sims do dia
SIMS=$(sqlite3 "$DB" "SELECT count(*)||' sims' FROM forward_sim_trades WHERE entry_time LIKE '$D%';" 2>/dev/null)
CB=$(grep -c "\[COOLDOWN\]" /tmp/vt_forward_walker.log 2>/dev/null)
MSG="${MSG}Walker: ${SIMS:-0} | bloqueios cooldown: ${CB:-0}\n"

# 4. Alinhamento forward vs live por par
ALIGN=$(sqlite3 -separator ' | ' "$DB" "
SELECT 'sim '||substr(symbol,1,3)||': '||count(*)||' / R$'||round(sum(net_pnl),1)
FROM forward_sim_trades WHERE entry_time LIKE '$D%' GROUP BY substr(symbol,1,3);
" 2>/dev/null | tr '\n' '; ')
LV=$(sqlite3 -separator '; ' "$DB" "
SELECT 'live '||substr(symbol,1,3)||': '||count(*)||' / R$'||round(sum(net_pnl),1)
FROM trades WHERE date(entry_time)='$D' GROUP BY substr(symbol,1,3);
" 2>/dev/null)
MSG="${MSG}Forward: ${ALIGN}\nLive:    ${LV}\n"

# 5. Daemon pós-EOD
if pgrep -f "vt_autotrader.py" >/dev/null 2>&1; then
    MSG="${MSG}Daemon: ainda vivo (EOD o encerra; self-heal supervisiona)\n"
else
    MSG="${MSG}Daemon: encerrou pós-EOD (normal)\n"
fi

echo -e "$MSG"
$HERMES send -t "$TARGET" "$(echo -e "$MSG" | tr -d '\\n' | head -c 3500)" >/dev/null 2>&1 || echo "[aviso] hermes send falhou"

# Mineração detalhada fica no arquivo (não cabe no Telegram)
{
    echo "== Mineracao $D =="
    sqlite3 -header -column "$DB" "SELECT symbol, timeframe, strategy, direction, exit_reason, round(net_pnl_brl,2) AS brl FROM forward_sim_trades WHERE entry_time LIKE '$D%' ORDER BY symbol, entry_time;"
} >> "$PROJECT/data/forward_journal/mineracao_$(date +%Y%m%d).md" 2>&1
