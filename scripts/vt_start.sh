#!/bin/bash
# Vibe-Trading startup script — chamado pelo cron às 9:00 AM weekdays.
# Garante MT5 está rodando, depois inicia o autotrader.

PROJECT="/home/bruno/Projects/Vibe-Trading"
VENV="$PROJECT/agent/venv"
LOG="/tmp/vt_autotrader.log"
MT5_DIR="$HOME/.wine/drive_c/Program Files/MetaTrader 5 Terminal"

echo "[$(date)] === Vibe-Trading Startup ===" >> "$LOG"

# 1. Garantir MT5 está rodando
if ! pgrep -f "terminal64.exe" > /dev/null 2>&1; then
    echo "[$(date)] Iniciando MT5..." >> "$LOG"
    cd "$MT5_DIR"
    DISPLAY=:99 xvfb-run -a wine terminal64.exe /portable >> "$LOG" 2>&1 &
    sleep 20
fi

# 2. Testar conexão MT5
WINE_PY="$HOME/.wine/drive_c/Python311/python.exe"
wine "$WINE_PY" -c "
import sys; sys.path.insert(0, r'C:\Python311\Lib\site-packages')
import MetaTrader5 as mt5
mt5.initialize()
info = mt5.account_info()
if info:
    print(f'OK conta={info.login} saldo={info.balance}')
else:
    print('ERRO sem conta')
mt5.shutdown()
" >> "$LOG" 2>&1

# 3. Verificar se MT5 está conectado
if ! grep -q "OK conta=" "$LOG" | tail -1; then
    echo "[$(date)] ERRO: MT5 não conectou. Abortando." >> "$LOG"
    hermes send -t telegram "❌ Vibe-Trading: MT5 não conseguiu conectar. Verifique o terminal." 2>/dev/null
    exit 1
fi

# 4. Iniciar autotrader
echo "[$(date)] Iniciando autotrader..." >> "$LOG"
cd "$PROJECT"
PYTHONPATH="$PROJECT/agent" "$VENV/bin/python" "$PROJECT/vt_autotrader.py" >> "$LOG" 2>&1 &
AUTO_PID=$!
echo "[$(date)] Autotrader PID: $AUTO_PID" >> "$LOG"

# 5. Notificar
hermes send -t telegram "🚀 Vibe-Trading iniciado (PID $AUTO_PID)" 2>/dev/null
