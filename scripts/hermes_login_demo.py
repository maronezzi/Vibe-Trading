#!/usr/bin/env python3
"""Login na conta demo XPMT5-DEMO 52257579 — usado pelo Hermes para trocar a conta do MT5."""
import sys
import time

import MetaTrader5 as mt5

sys.stdout.reconfigure(encoding="utf-8")

LOGIN = 52257579
PASSWORD = "T7@nQ4&wR"
SERVER = "XPMT5-DEMO"


def main():
    ok = mt5.initialize()
    if not ok:
        print(f"initialize falhou: {mt5.last_error()}", flush=True)
        return 1
    time.sleep(2)
    r = mt5.login(login=LOGIN, password=PASSWORD, server=SERVER)
    if not r:
        print(f"login demo falhou: {mt5.last_error()}", flush=True)
        return 2
    time.sleep(2)
    acc = mt5.account_info()
    if acc:
        print(
            f"OK conta={acc.login} server={acc.server} balance={acc.balance} "
            f"trade_allowed={acc.trade_allowed}",
            flush=True,
        )
    else:
        print("OK login mas account_info None", flush=True)
    mt5.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
