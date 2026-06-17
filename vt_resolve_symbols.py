#!/usr/bin/env python3
"""
Vibe-Trading Symbol Resolver — verifica e loga o contrato mais líquido.
Roda às 08:55 (antes do pregão) pra garantir que estamos operando o contrato certo.

Lógica: usa vt_calendar.resolve_symbol que compara spread real do MT5
e mantém o contrato vigente até < 3 dias úteis do vencimento.

Uso:
    python3 vt_resolve_symbols.py
"""

import sys
import json
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from vt_calendar import resolve_symbol


# Load config
with open(Path(__file__).parent / "vt_config.json") as f:
    CONFIG = json.load(f)


def main():
    print(f"=== Resolução de Símbolos — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===\n")

    for root in CONFIG.get("symbols", ["WIN", "BIT", "DOL", "IND", "WSP", "WDO"]):
        try:
            symbol = resolve_symbol(root)
            if symbol:
                print(f"{root}: {symbol}")
            else:
                print(f"{root}: ERRO na resolução!")
        except Exception as e:
            print(f"{root}: ERRO — {e}")
        print()


if __name__ == "__main__":
    main()
