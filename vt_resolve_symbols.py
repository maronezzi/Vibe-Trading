#!/usr/bin/env python3
"""
Vibe-Trading Symbol Resolver — verifica e loga o contrato mais líquido.
Roda às 08:55 (antes do pregão) pra garantir que estamos operando o contrato certo.

Uso:
    python3 vt_resolve_symbols.py
"""

import sys
import json

from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mt5_orchestrator import resolve_symbol, tick


# Load config
with open(Path(__file__).parent / "vt_config.json") as f:
    CONFIG = json.load(f)

def main():
    print(f"=== Resolução de Símbolos — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===\n")
    
    for root in CONFIG.get("symbols", ["WIN", "BIT", "DOL", "IND", "WSP"]):
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
    
    # Comparar contratos de cada ativo
    print("=== Comparação de Contratos ===")
    from mt5_orchestrator import _run_wine, EXECUTOR_WIN
    
    MONTH_LETTERS = {1:"F",2:"G",3:"H",4:"J",5:"K",6:"M",7:"N",8:"Q",9:"U",10:"V",11:"X",12:"Z"}
    today = datetime.now()
    
    for root in CONFIG.get("symbols", ["WIN", "BIT", "DOL", "IND", "WSP"]):
        contracts = []
        for i in range(4):
            m = ((today.month - 1 + i) % 12) + 1
            y = today.year + ((today.month - 1 + i) // 12)
            letter = MONTH_LETTERS[m]
            yy = str(y)[-2:]
            contracts.append(f"{root}{letter}{yy}")
        
        print(f"\n{root}:")
        for sym in contracts:
            t = tick(sym)
            if "error" not in t and t.get("bid", 0) > 0:
                spread = t.get("ask", 0) - t.get("bid", 0)
                print(f"  {sym}: bid={t['bid']} spread={spread} vol={t.get('volume', 0)}")

if __name__ == "__main__":
    main()
