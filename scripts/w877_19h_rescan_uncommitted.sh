#!/usr/bin/env bash
# scripts/w877_19h_rescan_uncommitted.sh — Wave 15 cron 19h
#
# Detecta drift no repo Vibe-Trading (arquivos modificados/untracked que
# NÃO foram commitados no Wave 15) e entrega relatório via Telegram.
#
# Roda às 19h (após fechamento do mercado + AGI v4 17:10). Não escreve
# nada no repo sozinho — só reporta.
#
# Cron (instalar manualmente, ver crontab.txt):
#   00 19 * * 1-5 /home/bruno/Projects/Vibe-Trading/scripts/w877_19h_rescan_uncommitted.sh
#
# Requer: hermes CLI no PATH (para enviar Telegram). Se não tiver, cai
# no log local + stdout.

set -u
cd /home/bruno/Projects/Vibe-Trading

TS="$(date +%Y-%m-%d_%H%M%S)"
LOG=/tmp/w877_rescan_${TS}.log

# Contadores globais (acessíveis fora do subshell do tee)
MODIFIED_COUNT=0
UNTRACKED_COUNT=0

{
  echo "=========================================================="
  echo "  WAVE 15 RESCAN — $TS"
  echo "  Repo: $(pwd)"
  echo "=========================================================="
  echo ""

  echo "=== 1. Último commit ==="
  git log --oneline -1
  echo ""

  echo "=== 2. Arquivos modificados (não staged) ==="
  MODIFIED=$(git status --short | grep '^ M' | awk '{print $2}')
  if [ -z "$MODIFIED" ]; then
    echo "(nenhum)"
  else
    echo "$MODIFIED"
  fi
  echo ""

  echo "=== 3. Arquivos modificados (staged mas não commitados) ==="
  STAGED=$(git status --short | grep '^M' | awk '{print $2}')
  if [ -z "$STAGED" ]; then
    echo "(nenhum)"
  else
    echo "$STAGED"
  fi
  echo ""

  echo "=== 4. Arquivos untracked ==="
  UNTRACKED=$(git status --short | grep '^??' | awk '{print $2}')
  if [ -z "$UNTRACKED" ]; then
    echo "(nenhum)"
  else
    echo "$UNTRACKED"
  fi
  echo ""

  echo "=== 5. Resumo quantitativo ==="
  TOTAL_MOD=$(git status --short | grep -cE '^[ ]?M')
  TOTAL_UN=$(git status --short | grep -cE '^\?\?')
  MODIFIED_COUNT=$TOTAL_MOD
  UNTRACKED_COUNT=$TOTAL_UN
  echo "modified: $TOTAL_MOD"
  echo "untracked: $TOTAL_UN"
  echo ""

  echo "=== 6. Sugestões automáticas ==="
  # Heurística: agrupar untracked por diretório
  if [ -n "$UNTRACKED" ]; then
    echo "$UNTRACKED" | awk -F'/' '{print $1}' | sort -u | while read DIR; do
      COUNT=$(echo "$UNTRACKED" | grep -c "^$DIR/")
      echo "  → $DIR/ : $COUNT arquivo(s)"
    done
  fi
  echo ""

  echo "=== 7. Posição no autotrader ==="
  if pgrep -f "core/vt_autotrader.py" > /dev/null; then
    echo "autotrader: ATIVO (PID $(pgrep -f 'core/vt_autotrader.py'))"
  else
    echo "autotrader: PARADO"
  fi
  echo ""

  echo "=== 8. Drift DB ↔ MT5 (P&L realizado hoje) ==="
  /usr/bin/python3 << 'PYEOF' 2>/dev/null
import sqlite3, sys
sys.path.insert(0, "/home/bruno/Projects/Vibe-Trading")
from mt5 import mt5_orchestrator as o
st = o.status()
mt5_bal = st['account']['balance']
conn = sqlite3.connect("/home/bruno/Projects/Vibe-Trading/vt_trades.db")
c = conn.cursor()
# PnL líquido realizados hoje
pnl_today = c.execute("""
    SELECT COALESCE(SUM(net_pnl), 0.0)
    FROM trades
    WHERE DATE(entry_time)='2026-07-13' AND exit_time IS NOT NULL
""").fetchone()[0]
# Trades "abertas" no DB (exit_time NULL)
open_in_db = c.execute("""
    SELECT COUNT(*) FROM trades
    WHERE DATE(entry_time)='2026-07-13' AND exit_time IS NULL
""").fetchone()[0]
mt5_pos = st['n_positions']
print(f"  MT5 balance:   R$ {mt5_bal:.2f}")
print(f"  MT5 pos:       {mt5_pos}")
print(f"  DB open (sem exit_time): {open_in_db}")
print(f"  PnL realizado hoje (DB): R$ {pnl_today:+.2f}")
implied_balance = mt5_bal + pnl_today
# O diff real não é simples — depende do starting balance.
# Heurística: se open_in_db > 0 mas mt5_pos=0 → drift real
if open_in_db > 0 and mt5_pos == 0:
    print(f"  ⚠️  POSSÍVEL DRIFT: {open_in_db} trades abertas no DB mas MT5 está flat")
PYEOF
  echo ""

  echo "=== 9. Sanity check modify_sl Invalid stops (Wave 15d) ==="
  # Wave 15d (Bruno 2026-07-13 13:25): investigar padrão modify_sl falha +
  # _fix_invalid_stops_modify também falha + emergency_close + GHOST no DB.
  # Hoje: 8x 'Invalid stops', todas resultaram em close (não emergency).
  # Heurística: se ratio Invalid_stops/modify_sl_attempts > 30%, há bug.
  /usr/bin/python3 << 'PYEOF' 2>/dev/null
import subprocess
with open('/tmp/vt_autotrader.log') as f:
    log = f.read()
inv_stops = log.count('Invalid stops')
fix_attempts = log.count('MODIFY') + log.count('Fix padrão:')
em_cl = log.count('EMERGENCY_CLOSE') + log.count('emergency_closed')
ratio = (inv_stops / max(fix_attempts, 1)) * 100
print(f"  Invalid stops hoje:    {inv_stops}")
print(f"  MODIFY attempts:       {fix_attempts}")
print(f"  Emergency closes:      {em_cl}")
print(f"  Ratio Invalid/MODIFY:  {ratio:.1f}% (alerta > 30%)")
if ratio > 30:
    print(f"  ⚠️  ALTA TAXA DE INVALID STOPS — investigar")
    print(f"     Sugestão: revisar _fix_invalid_stops_modify em mt5_error_recovery.py")
    print(f"     (race condition: entry_price do fix vs SL já modificado)")
PYEOF
  echo ""

  echo "=========================================================="
  echo "  FIM DO RELATÓRIO"
  echo "  Log: $LOG"
  echo "=========================================================="
} | tee "$LOG"

# Enviar via Telegram (se hermes disponível)
if command -v hermes > /dev/null 2>&1; then
  MSG="🔍 *Wave 15 rescan 19h*

Modified: ${MODIFIED_COUNT}
Untracked: ${UNTRACKED_COUNT}

Log completo: \`$LOG\`"
  hermes send --to telegram:71802842 --message "$MSG" 2>/dev/null || \
    hermes send --message "$MSG" 2>/dev/null || \
    echo "(hermes send falhou, log local em $LOG)"
else
  echo "(hermes não está no PATH; relatório em $LOG)"
fi
