#!/bin/bash
# mt5_show.sh — Mostra MT5 na tela (x11vnc + vncviewer)
#
# Wave 10 (2026-06-26, Bruno): MT5 sempre em background, mas tu pode ver.
#
# Arquitetura:
#   1. Xvfb display :99 já está rodando (invisível)
#   2. MT5 (terminal64.exe) já está rodando nele
#   3. x11vnc expõe display :99 via protocolo VNC
#   4. Cliente VNC conecta (vncviewer, Remmina, etc.)
#
# Uso:
#   bash scripts/mt5_show.sh              # inicia x11vnc e abre cliente VNC
#   bash scripts/mt5_show.sh --no-open    # só inicia x11vnc (sem abrir cliente)
#   bash scripts/mt5_show.sh --stop       # para x11vnc
#   bash scripts/mt5_show.sh --port 5900  # usa porta customizada
#
# Conexão: vnc://localhost:5900 (senha = VIBE_VNC_PWD ou "vibe")

set -e

# Defaults
PORT="${VIBE_VNC_PORT:-5900}"
PASSWORD="${VIBE_VNC_PWD:-vibe}"
DISPLAY_NUM=99
LOG="/tmp/mt5_show.log"

# Parse args
ACTION="start"
OPEN_CLIENT=true
while [ $# -gt 0 ]; do
    case "$1" in
        --no-open)
            OPEN_CLIENT=false
            shift
            ;;
        --stop)
            ACTION="stop"
            shift
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --status)
            ACTION="status"
            shift
            ;;
        --help|-h)
            echo "Uso: $0 [--no-open] [--stop] [--port 5900] [--status]"
            echo ""
            echo "Inicia x11vnc para expor display :99 (MT5) via VNC."
            echo "Conecte com qualquer cliente VNC (Remmina, vncviewer, TigerVNC)"
            echo "em vnc://localhost:$PORT"
            exit 0
            ;;
        *)
            echo "Arg desconhecido: $1 (use --help)"
            exit 1
            ;;
    esac
done

case "$ACTION" in
    stop)
        if pgrep -f "x11vnc.*:$DISPLAY_NUM" >/dev/null; then
            pkill -f "x11vnc.*:$DISPLAY_NUM" || true
            echo "✅ x11vnc parado"
        else
            echo "⚪ x11vnc não estava rodando"
        fi
        exit 0
        ;;

    status)
        echo "📊 Status MT5 View:"
        echo "   Xvfb:     $(pgrep -f "Xvfb :$DISPLAY_NUM" >/dev/null && echo '✅ rodando' || echo '❌ parado')"
        echo "   MT5:      $(pgrep -f 'terminal64.exe' >/dev/null && echo '✅ rodando' || echo '❌ parado')"
        echo "   x11vnc:   $(pgrep -f "x11vnc.*:$DISPLAY_NUM" >/dev/null && echo "✅ exposto na porta $PORT" || echo '⚪ não exposto')"
        echo "   Cliente:  vnc://localhost:$PORT (senha: $PASSWORD)"
        exit 0
        ;;

    start)
        # 1) Verifica se Xvfb está rodando
        if ! pgrep -f "Xvfb :$DISPLAY_NUM" >/dev/null; then
            echo "❌ Xvfb display :$DISPLAY_NUM não está rodando!"
            echo "   Inicie com: bash scripts/start_mt5linux.sh"
            exit 1
        fi

        # 2) Verifica se MT5 está rodando
        if ! pgrep -f 'terminal64.exe' >/dev/null; then
            echo "⚠️  MT5 não está rodando. Iniciando em background..."
            bash "$(dirname "$0")/start_mt5linux.sh" >> "$LOG" 2>&1
            sleep 5
        fi

        # 3) Verifica se x11vnc está instalado
        if ! command -v x11vnc >/dev/null 2>&1; then
            echo "❌ x11vnc não instalado!"
            echo "   Instale com: sudo apt install -y x11vnc"
            exit 1
        fi

        # 4) Se x11vnc já está rodando nessa porta, só mostra
        if pgrep -f "x11vnc.*:$DISPLAY_NUM" >/dev/null; then
            echo "✅ x11vnc já está exposto em vnc://localhost:$PORT"
        else
            # Mata instância anterior (se houver)
            pkill -f "x11vnc.*:$DISPLAY_NUM" 2>/dev/null || true
            sleep 1

            # Inicia x11vnc em background
            echo "🔓 Iniciando x11vnc exp..."
            DISPLAY=:$DISPLAY_NUM x11vnc \
                -display :$DISPLAY_NUM \
                -forever \
                -shared \
                -rfbport $PORT \
                -passwd "$PASSWORD" \
                -bg \
                -o "$LOG" 2>&1 | tail -5
            sleep 2
        fi

        echo ""
        echo "============================================================"
        echo "✅ MT5 exposto via VNC!"
        echo "============================================================"
        echo ""
        echo "📺 Para ver MT5 na tela:"
        echo "   • URL:     vnc://localhost:$PORT"
        echo "   • Senha:   $PASSWORD"
        echo ""
        echo "🖥️  Clientes VNC sugeridos:"
        echo "   • Remmina:    remmina -c vnc://localhost:$PORT"
        echo "   • vncviewer:  vncviewer localhost:$PORT"
        echo "   • TigerVNC:   vncviewer localhost:$PORT"
        echo "   • NoTuMeer (browser): http://localhost:6080  # requer novnc"
        echo ""

        if $OPEN_CLIENT; then
            # Tenta abrir cliente VNC automaticamente
            echo "🚀 Tentando abrir cliente VNC automaticamente..."
            if command -v remmina >/dev/null 2>&1; then
                remmina -c vnc://localhost:$PORT &
            elif command -v vncviewer >/dev/null 2>&1; then
                vncviewer localhost:$PORT &
            elif command -v vncviewer64 >/dev/null 2>&1; then
                vncviewer64 localhost:$PORT &
            elif command -x xdg-open >/dev/null 2>&1; then
                xdg-open vnc://localhost:$PORT &
            else
                echo "⚠️  Nenhum cliente VNC detectado. Conecte manualmente."
            fi
        fi

        echo ""
        echo "💡 Para parar x11vnc: bash scripts/mt5_show.sh --stop"
        ;;
esac
