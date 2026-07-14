#!/bin/bash
# start_mt5linux.sh — Inicia o bridge mt5linux (Wine + RPyC + MT5)
#
# Arquitetura:
#   1. Xvfb display :99 (servidor X virtual) — invisível, sempre background
#   2. Wine: MT5 (terminal64.exe) + Python Windows (python.exe) com MetaTrader5
#   3. RPyC server: roda o rpyc_classic.py via Python Windows no Wine
#   4. mt5linux client (Python Linux): conecta em localhost:5001
#
# Uso:
#   bash start_mt5linux.sh [login] [senha] [servidor]
#   bash start_mt5linux.sh --background  # Wave 10: modo background (default)
#   bash scripts/mt5_show.sh             # ver MT5 (VNC)
#
# Deixa rodando em background. Pra parar: pkill -f Xvfb ; pkill -f terminal64
#
# Wave 10 (2026-06-26, Bruno): MT5 SEMPRE em background (Xvfb invisível).
# Para visualizar, use scripts/mt5_show.sh que inicia x11vnc.

set -e
export WINEPREFIX="$HOME/.wine64"
export WINEARCH=win64
export DISPLAY=:99
export WINEDEBUG=-all
export PYTHONUNBUFFERED=1

MT5_PATH="$WINEPREFIX/drive_c/Program Files/MetaTrader 5/terminal64.exe"
PYWIN="$WINEPREFIX/drive_c/Program Files/Python311/python.exe"
RPYC_PORT="${RPYC_PORT:-5001}"

# Wave 10: parse flags. --background é default (compatibilidade retroativa)
MODE="background"
LOGIN=""
PASSWORD=""
SERVER=""
while [ $# -gt 0 ]; do
    case "$1" in
        --background|--bg)
            MODE="background"
            shift
            ;;
        --show|--gui)
            # Mantido por compat — em Wave 10 sempre background. Use mt5_show.sh
            MODE="background"
            echo "ℹ️  --show deprecated. Use: bash scripts/mt5_show.sh"
            shift
            ;;
        --help|-h)
            echo "Uso: $0 [--background] [login senha servidor]"
            echo ""
            echo "MT5 sempre roda em background (Xvfb invisível)."
            echo "Para visualizar: bash scripts/mt5_show.sh"
            exit 0
            ;;
        *)
            # Primeiro arg é login, segundo senha, terceiro servidor
            if [ -z "$LOGIN" ]; then
                LOGIN="$1"
            elif [ -z "$PASSWORD" ]; then
                PASSWORD="$1"
            elif [ -z "$SERVER" ]; then
                SERVER="$1"
            fi
            shift
            ;;
    esac
done

# 1) Inicia Xvfb (se não estiver rodando)
if ! pgrep -f "Xvfb :99" >/dev/null; then
    echo "🖥️  Iniciando Xvfb display :99 (invisível)..."
    # Bruno 09/07: resolução subiu 1280x800 → 1920x1080 (Full HD).
    # 1280x800 deixava MT5 descalibrado no noVNC (paineis pequenos, texto
    # truncado, botoes sobrepostos). 1920x1080 é o padrao MT5 desktop.
    # Xvfb +xinerama +render permite compositor completo (sem warnings).
    Xvfb :99 -screen 0 1920x1080x24 +xinerama -ac &
fi

# 2) Inicia MT5 (se não estiver rodando)
if ! pgrep -f "terminal64.exe" >/dev/null; then
    echo "📈 Iniciando MetaTrader 5 em background..."
    wine "$MT5_PATH" /portable &
    sleep 8
else
    echo "✅ MT5 já está rodando (PID $(pgrep -f 'terminal64.exe'))"
fi

# 3) Login (se credenciais fornecidas)
if [ -n "$LOGIN" ] && [ -n "$PASSWORD" ] && [ -n "$SERVER" ]; then
    echo "🔐 Fazendo login: $LOGIN @ $SERVER..."
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
    echo "   Para ver: bash scripts/mt5_show.sh"
fi

# 4) Inicia RPyC server (bridge pro Linux)
if ! pgrep -f "rpyc_classic" >/dev/null; then
    echo "🌉 Iniciando bridge RPyC na porta $RPYC_PORT..."
    wine "$PYWIN" -m mt5linux --port "$RPYC_PORT" &
    sleep 3
else
    echo "✅ RPyC já está rodando (PID $(pgrep -f 'rpyc_classic'))"
fi

echo ""
echo "============================================================"
echo "✅ MT5 + RPyC rodando em BACKGROUND (Xvfb :99 invisível)"
echo "============================================================"
echo ""
echo "Próximos passos:"
echo "  • Ver MT5 na tela: bash scripts/mt5_show.sh"
echo "  • Parar tudo: pkill -f Xvfb ; pkill -f terminal64 ; pkill -f rpyc_classic"
echo ""
echo "📊 Status atual:"
echo "   Xvfb:     $(pgrep -f 'Xvfb :99' >/dev/null && echo '✅ rodando' || echo '❌ parado')"
echo "   MT5:      $(pgrep -f 'terminal64.exe' >/dev/null && echo '✅ rodando' || echo '❌ parado')"
echo "   RPyC:     $(pgrep -f 'rpyc_classic' >/dev/null && echo '✅ rodando' || echo '❌ parado')"
echo "   VNC:      $(pgrep -f 'x11vnc' >/dev/null && echo '✅ exposto (mt5_show)' || echo '⚪ não exposto (use mt5_show)')"
echo "   Porta:    $RPYC_PORT"
