#!/usr/bin/env python3
"""
Vibe-Trading Symbol Resolver — verifica e loga o contrato mais líquido.
Roda às 08:55 (antes do pregão) pra garantir que estamos operando o contrato certo.

Uso:
    python3 vt_resolve_symbols.py
"""

import sys
import json
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from mt5_orchestrator import resolve_symbol, tick

def main():
    print(f"=== Resolução de Símbolos — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===\n")
    
    for root in ["WIN", "WDO"]:
        symbol = resolve_symbol(root)
        if symbol:
            t = tick(symbol)
            spread = t.get("ask", 0) - t.get("bid", 0) if t.get("bid", 0) > 0 else "N/A"
            print(f"{root}: {symbol}")
            print(f"  Bid: {t.get('bid', 0)} | Ask: {t.get('ask', 0)} | Spread: {spread}")
            print(f"  Volume: {t.get('volume', 0)}")
        else:
            print(f"{root}: ERRO na resolução!")
        print()
    
    # Comparar com contratos alternativos
    print("=== Comparação de Contratos ===")
    from mt5_orchestrator import _run_wine, EXECUTOR_WIN
    
    # WDO
    wdo_contracts = ["WDOM26", "WDON26", "WDOQ26"]
    print("\nWDO:")
    for sym in wdo_contracts:
        t = tick(sym)
        if "error" not in t and t.get("bid", 0) > 0:
            spread = t.get("ask", 0) - t.get("bid", 0)
            print(f"  {sym}: bid={t['bid']} spread={spread} vol={t.get('volume', 0)}")
    
    # WIN
    win_contracts = ["WINM26", "WINN26", "WINQ26"]
    print("\nWIN:")
    for sym in win_contracts:
        t = tick(sym)
        if "error" not in t and t.get("bid", 0) > 0:
            spread = t.get("ask", 0) - t.get("bid", 0)
            print(f"  {sym}: bid={t['bid']} spread={spread} vol={t.get('volume', 0)}")

if __name__ == "__main__":
    main()
