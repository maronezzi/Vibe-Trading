"""
Fetch data from MetaTrader 5 running in Wine.
Runs INSIDE Wine Python:  wine python.exe mt5_fetch.py

Outputs CSV to stdout for Linux to consume.
Usage:
  wine python.exe mt5_fetch.py ticks WIN$ 20
  wine python.exe mt5_fetch.py rates WIN$ D1 100
  wine python.exe mt5_fetch.py rates WDO$ H1 200
  wine python.exe mt5_fetch.py info
"""

import sys
import csv
import io
import MetaTrader5 as mt5
from datetime import datetime


def main():
    ok = mt5.initialize()
    if not ok:
        print(f"ERROR: MT5 initialize failed: {mt5.last_error()}")
        sys.exit(1)

    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "info"

    if cmd == "info":
        info = mt5.terminal_info()
        acc = mt5.account_info()
        ver = mt5.version()
        print("=== MT5 INFO ===")
        print(f"terminal={info.name} build={info.build}")
        print(f"company={info.company}")
        print(f"connected={info.connected} trade_allowed={info.trade_allowed}")
        print(f"server={acc.server} login={acc.login} balance={acc.balance} {acc.currency}")
        print(f"version={ver}")
        symbols = [s.name for s in mt5.symbols_get() if "WIN" in s.name or "WDO" in s.name]
        print(f"futures={','.join(sorted(symbols))}")
        sys.exit(0)

    elif cmd == "ticks":
        symbol = sys.argv[2] if len(sys.argv) > 2 else "WIN$"
        count = int(sys.argv[3]) if len(sys.argv) > 3 else 10
        from datetime import datetime as _dt, timedelta as _td
        ticks = mt5.copy_ticks_from(symbol, _dt.now() - _td(hours=1), count, mt5.COPY_TICKS_ALL)
        if ticks is None or len(ticks) == 0:
            print(f"ERROR: No ticks for {symbol}")
            sys.exit(1)
        # numpy structured array -> CSV
        buf = io.StringIO()
        writer = csv.writer(buf)
        names = ticks.dtype.names
        writer.writerow(names)
        for row in ticks:
            writer.writerow([str(row[n]) for n in names])
        print(buf.getvalue())

    elif cmd == "rates":
        symbol = sys.argv[2] if len(sys.argv) > 2 else "WIN$"
        tf_str = sys.argv[3] if len(sys.argv) > 3 else "D1"
        count = int(sys.argv[4]) if len(sys.argv) > 4 else 100

        tf_map = {
            "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
            "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
            "D1": mt5.TIMEFRAME_D1, "W1": mt5.TIMEFRAME_W1,
            "MN": mt5.TIMEFRAME_MN1,
        }
        tf = tf_map.get(tf_str, mt5.TIMEFRAME_D1)
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            print(f"ERROR: No rates for {symbol} {tf_str}")
            sys.exit(1)
        buf = io.StringIO()
        writer = csv.writer(buf)
        names = rates.dtype.names
        writer.writerow(names)
        for row in rates:
            writer.writerow([str(row[n]) for n in names])
        print(buf.getvalue())

    elif cmd == "symbols":
        group = sys.argv[2] if len(sys.argv) > 2 else None
        syms = mt5.symbols_get(group)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["name", "description", "currency_base", "currency_profit",
                         "currency_margin", "trade_contract_size", "digits", "bid", "ask"])
        for s in syms:
            writer.writerow([s.name, s.description or "", s.currency_base or "",
                             s.currency_profit or "", s.currency_margin or "",
                             s.trade_contract_size, s.digits, s.bid, s.ask])
        print(buf.getvalue())

    else:
        print(f"Unknown command: {cmd}")
        print("Use: info, ticks, rates, symbols")
        sys.exit(1)

    mt5.shutdown()


if __name__ == "__main__":
    main()
