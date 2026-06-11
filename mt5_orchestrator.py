"""
Vibe-Trading Orchestrator (Linux side).
Interface Python que eu (Hermes) uso para enviar ordens ao MT5.
Chama o mt5_executor.py via Wine subprocess.

Uso típico (dentro de uma sessão Hermes):
    from mt5_orchestrator import mt5
    mt5.status()
    mt5.buy('WINQ26', volume=1, sl_pts=200)
    mt5.sell('WDOQ26', volume=1, sl_pts=50)
    mt5.close_all()
"""

import subprocess
import json
import os
from pathlib import Path
from typing import Optional, Union

PROJECT = Path("/home/bruno/Projects/Vibe-Trading")
WINE_PYTHON = os.path.expanduser("~/.wine/drive_c/Python311/python.exe")
EXECUTOR_WIN = "Z:\\home\\bruno\\Projects\\Vibe-Trading\\mt5_executor.py"
RESOLVE_WIN = "Z:\\home\\bruno\\Projects\\Vibe-Trading\\mt5_resolve.py"


def _run_wine(script: str, *args, timeout=30) -> dict:
    """Roda um script Python dentro do Wine e retorna o JSON do stdout."""
    cmd = ["wine", WINE_PYTHON, script, *args]
    env = {**os.environ, "WINEDEBUG": "-all"}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    except Exception as e:
        return {"error": str(e)}

    out = r.stdout.strip()
    err = r.stderr.strip()

    # stderr tem os logs; descarta-os
    # stdout tem o JSON
    # Pega do primeiro "{" ao último "}" (multi-line JSON)
    if "{" in out:
        start = out.find("{")
        end = out.rfind("}")
        if end > start:
            candidate = out[start:end+1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
    # Se não tem JSON, retorna raw
    return {"raw_stdout": out[-500:] if out else "", "raw_stderr": err[-500:] if err else "", "returncode": r.returncode}


def _log(msg):
    """Log silencioso pro /tmp."""
    with open("/tmp/vt_orchestrator.log", "a") as f:
        from datetime import datetime
        f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")


def resolve_symbol(root: str) -> Optional[str]:
    """Retorna o símbolo de maior liquidez (ex: 'WINQ26' ou 'WDOQ26')."""
    r = _run_wine(RESOLVE_WIN, root)
    if "best" in r and r["best"]:
        return r["best"]["name"]
    return None


def status() -> dict:
    return _run_wine(EXECUTOR_WIN, "status")


def tick(symbol: str) -> dict:
    return _run_wine(EXECUTOR_WIN, "tick", symbol)


def info(symbol: str) -> dict:
    return _run_wine(EXECUTOR_WIN, "info", symbol)


def buy(symbol_or_root: str, volume: float = 1.0, sl_pts: Optional[int] = None,
        tp_pts: Optional[int] = None, auto_resolve: bool = True) -> dict:
    """
    Compra com SL obrigatório.
    symbol_or_root: 'WINQ26' (específico) ou 'WIN' (resolve o mais líquido)
    """
    sym = symbol_or_root
    if auto_resolve and len(symbol_or_root) <= 4:  # 'WIN', 'WDO'
        resolved = resolve_symbol(symbol_or_root)
        if resolved:
            sym = resolved
    args = ["buy", sym, str(volume)]
    if sl_pts is not None:
        args.append(str(sl_pts))
    if tp_pts is not None:
        args.append(str(tp_pts))
    result = _run_wine(EXECUTOR_WIN, *args)
    _log(f"BUY {sym} vol={volume} sl={sl_pts} → {result.get('status', result.get('error', '?'))}")
    return result


def sell(symbol_or_root: str, volume: float = 1.0, sl_pts: Optional[int] = None,
         tp_pts: Optional[int] = None, auto_resolve: bool = True) -> dict:
    sym = symbol_or_root
    if auto_resolve and len(symbol_or_root) <= 4:
        resolved = resolve_symbol(symbol_or_root)
        if resolved:
            sym = resolved
    args = ["sell", sym, str(volume)]
    if sl_pts is not None:
        args.append(str(sl_pts))
    if tp_pts is not None:
        args.append(str(tp_pts))
    result = _run_wine(EXECUTOR_WIN, *args)
    _log(f"SELL {sym} vol={volume} sl={sl_pts} → {result.get('status', result.get('error', '?'))}")
    return result


def close(symbol_or_root: str, auto_resolve: bool = True) -> dict:
    sym = symbol_or_root
    if auto_resolve and len(symbol_or_root) <= 4:
        resolved = resolve_symbol(symbol_or_root)
        if resolved:
            sym = resolved
    return _run_wine(EXECUTOR_WIN, "close", sym)


def close_all() -> dict:
    return _run_wine(EXECUTOR_WIN, "close_all")


def modify_sl(symbol_or_root: str, ticket: int, new_sl_pts: int, auto_resolve: bool = True) -> dict:
    """
    Modifica o Stop Loss de uma posição aberta.
    symbol_or_root: 'WINQ26' ou 'WIN' (resolve automaticamente)
    ticket: ticket da posição no MT5
    new_sl_pts: novo SL em pontos
    """
    sym = symbol_or_root
    if auto_resolve and len(symbol_or_root) <= 4:
        resolved = resolve_symbol(symbol_or_root)
        if resolved:
            sym = resolved
    result = _run_wine(EXECUTOR_WIN, "modify", sym, str(ticket), str(new_sl_pts))
    _log(f"MODIFY_SL {sym} ticket={ticket} new_sl={new_sl_pts} → {result.get('status', result.get('error', '?'))}")
    return result


if __name__ == "__main__":
    # CLI de teste
    import sys
    if len(sys.argv) < 2:
        print("Uso: python mt5_orchestrator.py <comando>")
        print("Comandos: status, tick WINQ26, info WINQ26, buy WINQ26 1 200, sell WINQ26 1 200, close WINQ26, close_all, resolve WIN")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "status":
        print(json.dumps(status(), indent=2))
    elif cmd == "tick":
        print(json.dumps(tick(sys.argv[2]), indent=2))
    elif cmd == "info":
        print(json.dumps(info(sys.argv[2]), indent=2))
    elif cmd == "buy":
        sym = sys.argv[2]
        vol = float(sys.argv[3])
        sl = int(sys.argv[4]) if len(sys.argv) > 4 else None
        tp = int(sys.argv[5]) if len(sys.argv) > 5 else None
        print(json.dumps(buy(sym, vol, sl, tp), indent=2))
    elif cmd == "sell":
        sym = sys.argv[2]
        vol = float(sys.argv[3])
        sl = int(sys.argv[4]) if len(sys.argv) > 4 else None
        tp = int(sys.argv[5]) if len(sys.argv) > 5 else None
        print(json.dumps(sell(sym, vol, sl, tp), indent=2))
    elif cmd == "close":
        print(json.dumps(close(sys.argv[2]), indent=2))
    elif cmd == "close_all":
        print(json.dumps(close_all(), indent=2))
    elif cmd == "resolve":
        print(f"Best {sys.argv[2]}: {resolve_symbol(sys.argv[2])}")
