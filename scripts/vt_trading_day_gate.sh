#!/bin/bash
# vt_trading_day_gate.sh — Gate de dia útil B3 para crons (Wave 892, 2026-09-07).
#
# Uso no crontab (prefixo nas linhas seg-sex dependentes de pregão):
#   55 8 * * 1-5 /home/bruno/Projects/Vibe-Trading/scripts/vt_trading_day_gate.sh && /usr/bin/python3 ...
#
# Exit 0 = dia útil B3 (o job do cron roda).
# Exit 1 = fim de semana/feriado B3 (o job é pulado em silêncio — sem log, sem Telegram).
#
# Fonte de verdade: core/vt_calendar.py (mesmo calendário que o daemon usa para
# bloquear entradas em feriado). Cron roda com PATH=/usr/bin:/bin —
# interpretador explícito, sem depender de venv.
/usr/bin/python3 - <<'EOF'
import sys
from datetime import date
sys.path.insert(0, "/home/bruno/Projects/Vibe-Trading/core")
from vt_calendar import is_trading_day
sys.exit(0 if is_trading_day(date.today())[0] else 1)
EOF
