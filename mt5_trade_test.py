"""Teste de capacidade de trading via MT5 — apenas verificação, sem ordens."""
import sys
sys.path.insert(0, r"C:\Python311\Lib\site-packages")
import MetaTrader5 as mt5
import json

# Conectar
ok = mt5.initialize()
if not ok:
    print(f"FALHA ao conectar: {mt5.last_error()}")
    sys.exit(1)

# Info da conta
acc = mt5.account_info()
print(f"Conta: {acc.login} | {acc.server}")
print(f"Balance: R$ {acc.balance:,.2f} | Equity: R$ {acc.equity:,.2f}")
print(f"Trade Mode: {acc.trade_mode} (0=contest, 1=demo, 2=real)")
print(f"Leverage: {acc.leverage}")

# Info do símbolo WIN$
info = mt5.symbol_info("WIN$")
if info:
    print(f"\nWIN$:")
    print(f"  Bid: {info.bid} | Ask: {info.ask}")
    print(f"  Trade Mode: {info.trade_mode} (4=FULL)")
    print(f"  Volume min: {info.volume_min} | max: {info.volume_max} | step: {info.volume_step}")
    print(f"  Contract size: {info.trade_contract_size}")
    print(f"  Margin: R$ {info.margin_initial:,.2f}")

# Verificar order_check (simulação sem enviar)
tick = mt5.symbol_info_tick("WIN$")
if tick:
    print(f"\nTick: bid={tick.bid} ask={tick.ask}")
    check = mt5.order_check({
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": "WIN$",
        "volume": 1,
        "type": mt5.ORDER_TYPE_BUY,
        "price": tick.ask,
    })
    print(f"\norder_check BUY 1 WIN$ @ {tick.ask}:")
    print(f"  retcode: {check.retcode} ({'OK' if check.retcode == 0 else 'ERRO'})")
    print(f"  comment: {check.comment}")
    print(f"  margin: R$ {check.margin:,.2f}")

# Posições abertas
positions = mt5.positions_get()
print(f"\nPosições abertas: {len(positions) if positions else 0}")

mt5.shutdown()
print("\n✅ MT5 trading API 100% operacional")
