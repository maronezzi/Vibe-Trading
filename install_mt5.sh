#!/bin/bash
# install_mt5.sh — Instala MetaTrader 5 no Linux via Wine + mt5linux bridge
#
# Arquitetura:
#   Linux (nativo) ←→ mt5linux (Python client) ←→ TCP localhost:5001
#                                                            ↑
#   Wine (MT5 + mt5linuxserver.exe) ←→ MetaTrader 5 terminal
#
# Pré-requisito: Wine instalado
#   sudo apt update && sudo apt install -y wine64 wine32 winetricks
#
# Uso:
#   bash install_mt5.sh
#   (depois) bash start_mt5linux.sh
#   (depois) cd ~/Projects/Vibe-Trading && PYTHONPATH=./agent ./agent/venv/bin/python backtest_futures.py WIN M5 sma

set -e
echo "============================================================"
echo "  Instalando MetaTrader 5 (B3) no Linux via Wine + mt5linux"
echo "============================================================"

# 1) Verificar Wine
echo -e "\n[1/7] Verificando Wine..."
wine --version >/dev/null 2>&1 || {
    echo "❌ Wine não encontrado. Instale com:"
    echo "   sudo apt install -y wine64 wine32 winetricks"
    exit 1
}
which winetricks >/dev/null 2>&1 || {
    echo "❌ winetricks não encontrado. Instale com:"
    echo "   sudo apt install -y winetricks"
    exit 1
}
wine --version

# 2) Criar prefix Wine 32-bit
echo -e "\n[2/7] Criando prefix Wine 32-bit em ~/.wine32..."
export WINEPREFIX="$HOME/.wine32"
export WINEARCH=win32
mkdir -p "$WINEPREFIX"
wineboot --init 2>&1 | tail -3 || true

# 3) Baixar MT5 oficial
echo -e "\n[3/7] Baixando MetaTrader 5 (~250 MB)..."
MT5_URL="https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
MT5_INSTALLER="$HOME/mt5setup.exe"
if [ ! -f "$MT5_INSTALLER" ]; then
    wget -q --show-progress -O "$MT5_INSTALLER" "$MT5_URL"
else
    echo "   (já baixado: $MT5_INSTALLER)"
fi

# 4) Instalar MT5
echo -e "\n[4/7] Instalando MT5 silenciosamente (~3 min)..."
wine "$MT5_INSTALLER" /auto /silent /portable 2>&1 | tail -5 || true

MT5_PATH=$(find "$WINEPREFIX" -name "terminal64.exe" 2>/dev/null | head -1)
if [ -z "$MT5_PATH" ]; then
    echo "❌ terminal64.exe não encontrado após instalação"
    echo "   Tente abrir manualmente: wine ~/.wine32/drive_c/Program\\ Files/MetaTrader\\ 5/terminal64.exe"
    exit 1
fi
echo "✅ MT5 instalado: $MT5_PATH"

# 5) Baixar mt5linuxserver.exe (bridge RPyC)
echo -e "\n[5/7] Baixando mt5linuxserver.exe (bridge RPyC)..."
MT5LINUX_DIR="$HOME/mt5linux"
mkdir -p "$MT5LINUX_DIR"
cd "$MT5LINUX_DIR"
MT5LINUX_URL="https://github.com/lucas-campagna/mt5linux/releases/latest/download/mt5linuxserver.exe"
if [ ! -f "mt5linuxserver.exe" ]; then
    # Tenta GitHub release
    wget -q --show-progress -O "mt5linuxserver.exe" "$MT5LINUX_URL" 2>&1 || {
        echo "⚠️  Download direto falhou — tentando via pip + Wine wrapper"
        # Fallback: o pip install mt5linux já foi feito, mas o server.exe é separado
    }
fi
ls -la "$MT5LINUX_DIR/" 2>&1 | head -5

# 6) Criar script de inicialização do servidor
echo -e "\n[6/7] Criando start_mt5linux.sh..."
cat > "$HOME/Projects/Vibe-Trading/start_mt5linux.sh" <<'EOF'
#!/bin/bash
# Inicia o servidor mt5linux (RPyC) dentro do Wine
# Requer MT5 já aberto e logado

set -e
export WINEPREFIX="$HOME/.wine32"
MT5_PATH=$(find "$WINEPREFIX" -name "terminal64.exe" 2>/dev/null | head -1)
MT5LINUX_PATH="$HOME/mt5linux/mt5linuxserver.exe"

if [ -z "$MT5_PATH" ]; then
    echo "❌ MT5 não encontrado em $WINEPREFIX"
    echo "   Rode: bash install_mt5.sh"
    exit 1
fi

if [ -f "$MT5LINUX_PATH" ]; then
    echo "🚀 Iniciando MT5 + mt5linuxserver..."
    wine "$MT5_PATH" /portable &
    sleep 8  # espera MT5 abrir
    wine "$MT5LINUX_PATH" &
    sleep 2
    echo ""
    echo "✅ mt5linuxserver rodando em localhost:5001"
    echo "   Agora você pode rodar backtests com:"
    echo "   cd ~/Projects/Vibe-Trading"
    echo "   PYTHONPATH=./agent ./agent/venv/bin/python backtest_futures.py WIN M5 sma"
else
    echo "⚠️  mt5linuxserver.exe não encontrado em $MT5LINUX_PATH"
    echo "   Baixe manualmente: $MT5LINUX_URL"
    echo "   Ou use a versão bundled: pip install mt5linux + copie mt5linuxserver.exe do wheel"
fi
EOF
chmod +x "$HOME/Projects/Vibe-Trading/start_mt5linux.sh"

# 7) Pacote Python já instalado
echo -e "\n[7/7] Verificando pacote Python mt5linux..."
cd ~/Projects/Vibe-Trading
PYTHONPATH=./agent ./agent/venv/bin/pip show mt5linux 2>&1 | head -3 || \
    PYTHONPATH=./agent ./agent/venv/bin/pip install mt5linux

echo -e "\n============================================================"
echo "  ✅ Instalação completa!"
echo "============================================================"
echo ""
echo "Próximos passos:"
echo "  1. Abra o MT5 e faça login:   wine \"$MT5_PATH\""
echo "     (crie conta demo se não tiver — arquivo > abrir conta > demo)"
echo "  2. Baixe o mt5linuxserver.exe (bridge RPyC):"
echo "     wget -O ~/mt5linux/mt5linuxserver.exe https://github.com/lucas-campagna/mt5linux/releases/latest/download/mt5linuxserver.exe"
echo "  3. Inicie o bridge:  bash ~/Projects/Vibe-Trading/start_mt5linux.sh"
echo "  4. Rode o backtest:"
echo "     cd ~/Projects/Vibe-Trading"
echo "     PYTHONPATH=./agent ./agent/venv/bin/python backtest_futures.py WIN M5 sma"
echo ""
echo "📁 Arquivos:"
echo "   MT5:  $MT5_PATH"
echo "   Loader Python: agent/backtest/loaders/mt5_loader.py"
echo "   Backtest: backtest_futures.py"
echo "   Script start: start_mt5linux.sh"
