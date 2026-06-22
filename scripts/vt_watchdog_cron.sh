#!/bin/bash
# Trade Watchdog - só roda durante horário de operação (09:00-16:45 BRT, seg-sex)
HOUR=$(date +%H)
MIN=$(date +%M)
DOW=$(date +%u)  # 1=Monday, 7=Sunday

# Só roda de segunda a sexta, entre 09:00 e 16:44
if [ "$DOW" -gt 5 ] || [ "$HOUR" -lt 9 ] || [ "$HOUR" -ge 17 ]; then
    exit 0  # Silent outside trading hours
fi
# Para às 16:45 (EOD = 16:45, não precisa mais monitorar)
if [ "$HOUR" -eq 16 ] && [ "$MIN" -ge 45 ]; then
    exit 0
fi

cd /home/bruno/Projects/Vibe-Trading && python3 monitoring/vt_trade_watchdog.py 2>&1 | grep -v "^✅"
