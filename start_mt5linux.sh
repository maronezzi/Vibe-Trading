#!/bin/bash
# start_mt5linux.sh — Inicia o bridge mt5linux (Wine + RPyC + MT5)
#
# Arquitetura:
#   1. Xvfb display :99 (servidor X virtual)
#   2. Wine: MT5 (terminal64.exe) + Python Windows (python.exe) com MetaTrader5
#   3. RPyC server: roda o rpyc_classic.py via Python Windows no Wine
#   4. mt5linux client (Python Linux): conecta em localhost:5001
#
# Uso:
#   bash start_mt5linux.sh [login] [senha] [servidor]
#   ex: bash start_mt5linux.sh 12345678 minhasenha "Rico-Investidor"
#
# Deixa rodando em background. Pra parar: pkill -f Xvfb ; pkill -f terminal64

set -e
export WINEPREFIX="$HOME/.wine64"
export WINEARCH=win64
export DISPLAY=:99
export WINEDEBUG=-all
export PYTHONUNBUFFERED=1

MT5_PATH="$WINEPREFIX/drive_c/Program Files/MetaTrader 5/terminal64.exe"
PYWIN="$WINEPREFIX/drive_c/Program Files/Python311/python.exe"
RPYC_PORT="${RPYC_PORT:-5001}"

# 1) Inicia Xvfb (se não estiver rodando)
if ! pgrep -f "Xvfb :99" >/dev/null; then
    echo "🖥️  Iniciando Xvfb display :99..."
    Xvfb :99 -screen 0 1024x768x24 &
    sleep 2
fi

# 2) Inicia MT5 (se não estiver rodando)
if ! pgrep -f "terminal64.exe" >/dev/null; then
    echo "📈 Iniciando MetaTrader 5..."
    wine "$MT5_PATH" &
    sleep 8
fi

# 3) Login (se credenciais fornecidas)
if [ $# -ge 3 ]; then
    LOGIN="$1"
    PASSWORD="$2"
    SERVER="$3"
    echo "🔐 Fazendo login: $LOGIN @ $SERVER..."

    # Pequeno script Python que faz login
    LOGIN_SCRIPT=$(cat <<EOF
import MetaTrader5 as mt5
mt5.initialize()
ok = mt5.login(login=int("$LOGIN"), password="$PASSWORD", server="$SERVER")
if ok:
    acc = mt5.account_info()
    print(f"✅ Logado: {acc.login} @ {acc.server}, saldo {acc.balance}")
else:
    print(f"❌ Login falhou: {mt5.last_error()}")
    exit(1)
EOF
)
    echo "$LOGIN_SCRIPT" > /tmp/mt5_login.py
    wine "$PYWIN" /tmp/mt5_login.py 2>&1 | tail -3
    sleep 2
else
    echo "ℹ️  Sem credenciais — você precisa logar manualmente no MT5"
    echo "   Uso: bash start_mt5linux.sh <login> <senha> <servidor>"
fi

# 4) Inicia RPyC server (bridge pro Linux)
if ! pgrep -f "rpyc_classic" >/dev/null; then
    echo "🌉 Iniciando bridge RPyC na porta $RPYC_PORT..."
    # Server é um Python module — usar Python do Windows no Wine
    wine "$PYWIN" -m mt5linux --port "$RPYC_PORT" &
    sleep 3
fi

# 5) Testa conexão do lado Linux
echo ""
echo "🧪 Testando conexão do Python Linux..."
PYTHONPATH=./agent ./agent/venv/bin/python -c "
import sys
sys.path.insert(0, './agent')
try:
    from backtest.loaders.mt5_loader import account_info
    acc = account_info(port=$RPYC_PORT)
    if acc:
        print(f'✅ Conectado: {acc[\"login\"]} @ {acc[\"server\"]}, saldo {acc[\"balance\"]} {acc[\"currency\"]}')
    else:
        print('❌ Não conectado')
except Exception as e:
    print(f'❌ Erro: {e}')
"

echo ""
echo "============================================================"
echo "✅ MT5 + RPyC rodando!"
echo "============================================================"
echo "Próximos passos:"
echo "  1. cd ~/Projects/Vibe-Trading"
echo "  2. PYTHONPATH=./agent ./agent/venv/bin/python backtest_futures.py WIN M5 sma"
echo "  3. Para parar tudo: pkill -f Xvfb ; pkill -f terminal64 ; pkill -f rpyc_classic"
echo ""
echo "📊 Status atual:"
echo "   Xvfb:     $(pgrep -f 'Xvfb :99' >/dev/null && echo '✅ rodando' || echo '❌ parado')"
echo "   MT5:      $(pgrep -f 'terminal64.exe' >/dev/null && echo '✅ rodando' || echo '❌ parado')"
echo "   RPyC:     $(pgrep -f 'rpyc_classic' >/dev/null && echo '✅ rodando' || echo '❌ parado')"
echo "   Porta:    $RPYC_PORT"
