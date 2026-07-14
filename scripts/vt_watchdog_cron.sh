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

# Bruno 2026-07-08 (Wave 1C.4): filtra stdout pra SÓ emitir alerta critico.
# Cron a55449e2c025 tem mode=no-agent (script stdout delivered direto Telegram),
# entao precisamos suprimir TODA saida rotineira. So passa:
#   - WATCHDOG ALERTA (linhas com "🚨" ou "WATCHDOG ALERTA")
#   - ORFAO / FANTASMA / GHOST (anomalias reais)
#   - MODIFY/STATE_ERRO/CRITICAL (problemas de execucao)
#   - WATCHDOG com "ERRORS" (status != OK)
# Linhas tipo "[DRIFT OK]", "✅ WATCHDOG: OK", "state sync fix" sao descartadas.
cd /home/bruno/Projects/Vibe-Trading && python3 monitoring/vt_trade_watchdog.py 2>&1 \
  | grep -E "🚨|WATCHDOG ALERTA|ORFAO|FANTASMA|GHOST|STATE_ERRO|MODIFY ERROR|CRITICAL|ERRORS|RECONCILED|self-heal|status: critical|drift.*ALERTA" \
  | grep -v "drift.*R\$0\.00" \
  || true  # exit 0 mesmo se grep nao encontrar nada (silencio padrao)
