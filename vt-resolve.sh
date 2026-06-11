#!/bin/bash
# Wrapper para mt5_resolve.py
WINE_PYTHON=~/.wine/drive_c/Python311/python.exe
WIN_SCRIPT="Z:\\home\\bruno\\Projects\\Vibe-Trading\\mt5_resolve.py"
wine "$WINE_PYTHON" "$WIN_SCRIPT" "$@" 2>/dev/null
