#!/bin/bash
# Wrapper Linux para o mt5_executor.py
# Facilita o uso: vt buy WIN$ 1 200

WINE_PYTHON=~/.wine/drive_c/Python311/python.exe
SCRIPT=~/Projects/Vibe-Trading/mt5_executor.py

if [ ! -f "$WINE_PYTHON" ]; then
    echo "❌ Wine Python não encontrado: $WINE_PYTHON" >&2
    exit 1
fi

if [ ! -f "$SCRIPT" ]; then
    echo "❌ Script não encontrado: $SCRIPT" >&2
    exit 1
fi

# Converte path Linux para Wine (Z:\home\bruno\...)
WIN_SCRIPT="Z:\\home\\bruno\\Projects\\Vibe-Trading\\mt5_executor.py"

wine "$WINE_PYTHON" "$WIN_SCRIPT" "$@"
