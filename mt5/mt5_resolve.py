#!/usr/bin/env python3
"""
Vibe-Trading Auto-Symbol — descobre o vencimento certo dinamicamente.
WINQ26 = Jun/2026 (letra do mês + 2 últimos dígitos do ano)
"""

import sys
import json
import subprocess
from datetime import datetime

sys.path.insert(0, r"C:\Python311\Lib\site-packages")
import MetaTrader5 as mt5

# Letras B3 para mês (vencimento de futuros)
# https://www.b3.com.br/pt_br/solucoes/custodias/indicadores/letras-do-vencimento.htm
MONTH_LETTERS = {
    1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
    7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z"
}


def get_year_letters():
    """Retorna [Q26, M27, V27, ...] etc (próximos 4 vencimentos)."""
    today = datetime.now()
    months_ahead = []
    for i in range(6):  # próximos 6 meses
        m = ((today.month - 1 + i) % 12) + 1
        y = today.year + ((today.month - 1 + i) // 12)
        # Pega a primeira letra daquele mês
        letter = MONTH_LETTERS[m]
        yy = str(y)[-2:]
        months_ahead.append(f"{letter}{yy}")
    return months_ahead


def find_active_symbols(root="WIN"):
    """Retorna lista de símbolos WIN/WDO que estão com trade_mode=FULL."""
    candidates = []
    for code in get_year_letters():
        sym = f"{root}{code}"
        info = mt5.symbol_info(sym)
        if info and info.trade_mode == 4:  # SYMBOL_TRADE_MODE_FULL
            tick = mt5.symbol_info_tick(sym)
            # Volume real de ticks (não volume_max)
            real_volume = 0
            if tick:
                # Usa o volume do tick como proxy de liquidez
                real_volume = tick.volume if hasattr(tick, 'volume') else 0
            
            candidates.append({
                "name": sym,
                "code": code,
                "path": info.path,
                "volume_max": info.volume_max,
                "real_volume": real_volume,
                "margin": info.margin_initial,
                "bid": tick.bid if tick else 0,
                "ask": tick.ask if tick else 0,
                "spread": (tick.ask - tick.bid) if tick else 999,
            })
    return candidates


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "uso: mt5_resolve.py <WIN|WDO>"}))
        sys.exit(1)

    root = sys.argv[1].upper()
    if not mt5.initialize():
        print(json.dumps({"error": f"init falhou: {mt5.last_error()}"}))
        sys.exit(1)

    try:
        candidates = find_active_symbols(root)
        # Ordena por volume real (mais líquido primeiro)
        # Se volume real for 0, usa spread menor como fallback
        candidates.sort(key=lambda x: (-x["real_volume"], x["spread"]))

        out = {
            "root": root,
            "month_letters": MONTH_LETTERS,
            "candidates": candidates,
            "best": candidates[0] if candidates else None,
        }
        print(json.dumps(out, indent=2, default=str))
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
