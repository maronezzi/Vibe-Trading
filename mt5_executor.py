#!/usr/bin/env python3
"""
Vibe-Trading Executor — controla MT5 direto via Python.
Envia ordens com SL sempre. Roda dentro do Wine (chamado via wine python.exe).

Uso:
    wine ~/.wine/drive_c/Python311/python.exe mt5_executor.py <comando> [args]

Comandos:
    status                  → Estado da conta, posições abertas
    buy <symbol> <vol> [sl_pts] [tp_pts]    → Compra com SL
    sell <symbol> <vol> [sl_pts] [tp_pts]   → Vende com SL
    close <symbol>         → Fecha todas posições do símbolo
    close_all              → Fecha tudo
    tick <symbol>          → Preço atual
    symbols [filter]       → Lista símbolos (opcional: WIN, WDO, DOL)
    info <symbol>          → Info completa de um símbolo

Garantias:
    - SEMPRE envia SL (mínimo ATR/2 ou parâmetro)
    - Logging em /tmp/vibetrading_exec.log
    - Confirma cada ordem com retcode
"""

import sys
import os
import json
import argparse
import time
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, r"C:\Python311\Lib\site-packages")
import MetaTrader5 as mt5

LOG_FILE = "/tmp/vibetrading_exec.log"


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line, file=sys.stderr)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def init():
    """Conecta ao MT5. Retorna True se OK."""
    if not mt5.initialize():
        log(f"mt5.initialize() falhou: {mt5.last_error()}", "ERROR")
        return False
    acc = mt5.account_info()
    if not acc:
        log("Conta não encontrada", "ERROR")
        return False
    if not acc.trade_allowed:
        log(f"Algo trading NÃO permitido na conta {acc.login}", "ERROR")
        return False
    log(f"Conectado: conta {acc.login} | server {acc.server} | balance R${acc.balance:,.2f}")
    return True


def get_min_sl_points(symbol):
    """Retorna o SL mínimo em pontos (do freeze level/stops level).

    WIN/WDO operam em ticks de 5 pontos (digits=0, mas stops precisam ser
    múltiplos de 5 com folga do bid/ask). Usamos mínimo conservador de 50
    para evitar rejeição 'Invalid stops' em condições voláteis.
    """
    info = mt5.symbol_info(symbol)
    if not info:
        return 50
    # stops_level em pontos + margem de segurança
    return max(50, info.trade_stops_level + 10)


def cmd_status():
    acc = mt5.account_info()
    if not acc:
        print(json.dumps({"error": "no account connected", "error_code": "NO_ACCOUNT"}))
        return

    positions = mt5.positions_get()
    pos_list = []
    if positions:
        for p in positions:
            pos_list.append({
                "ticket": p.ticket,
                "symbol": p.symbol,
                "type": "BUY" if p.type == 0 else "SELL",
                "volume": p.volume,
                "price_open": p.price_open,
                "price_current": p.price_current,
                "sl": p.sl,
                "tp": p.tp,
                "profit": p.profit,
                "swap": p.swap,
                "comment": p.comment,
                "time": str(p.time),
                "magic": p.magic,
                "identifier": p.identifier,
                "time_msc": p.time_msc,
                "reason": p.reason,
                "external_id": p.external_id,
            })

    orders = mt5.orders_get()
    ord_list = []
    if orders:
        for o in orders:
            ord_list.append({
                "ticket": o.ticket,
                "symbol": o.symbol,
                "type": o.type,
                "volume": o.volume_initial,
                "price": o.price_open,
            })

    out = {
        "account": {
            "login": acc.login,
            "server": acc.server,
            "balance": acc.balance,
            "equity": acc.equity,
            "margin": acc.margin,
            "free_margin": acc.margin_free,
            "leverage": acc.leverage,
            "trade_allowed": acc.trade_allowed,
            "currency": acc.currency,
        },
        "positions": pos_list,
        "n_positions": len(pos_list),
        "orders_pending": ord_list,
        "n_orders": len(ord_list),
    }
    print(json.dumps(out, indent=2, default=str))


def _get_filling_type(symbol):
    """Retorna o tipo de preenchimento correto para o símbolo (XP usa IOC)."""
    info = mt5.symbol_info(symbol)
    if not info:
        return mt5.ORDER_FILLING_IOC  # default safe
    filling_mode = getattr(info, 'filling_mode', 3)
    if filling_mode & 2:
        return mt5.ORDER_FILLING_IOC
    elif filling_mode & 1:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


def _try_send(symbol: str, side: str, volume: float, sl_pts: int, tp_pts, comment: str) -> dict:
    """
    Tenta enviar ordem com retry automático. Se der 'Invalid stops' (preço
    andou e o SL ficou muito perto), dobra o SL e tenta de novo até 3x.

    Regras XP (BVMF):
      - filling_mode=3 (FOK+IOC), sem RETURN
      - trade_tick_size determina a grade de preços (WIN=5, WDO=0.5)
      - SL deve estar alinhado ao tick_size
    Retorna o JSON com status final.
    """
    info = mt5.symbol_info(symbol)
    if not info:
        return {"error": f"símbolo {symbol} não encontrado"}
    if not info.visible:
        if not mt5.symbol_select(symbol, True):
            return {"error": f"falha ao selecionar {symbol}"}

    point = info.point
    digits = info.digits
    tick_size = getattr(info, 'trade_tick_size', point)
    if tick_size == 0:
        tick_size = point

    # Filling mode dinâmico (XP usa IOC)
    type_filling = _get_filling_type(symbol)

    last_err = None
    cur_sl_pts = sl_pts

    for attempt in range(4):  # 1 original + 3 retries
        tick = mt5.symbol_info_tick(symbol)
        if not tick or tick.ask == 0 or tick.bid == 0:
            return {"error": f"sem tick para {symbol}"}

        if side == "BUY":
            price = tick.ask
            raw_sl = price - cur_sl_pts * point
        else:
            price = tick.bid
            raw_sl = price + cur_sl_pts * point

        # Alinhar SL ao tick_size (grade de preços do símbolo)
        sl_price = round(raw_sl / tick_size) * tick_size
        sl_price = round(sl_price, digits)

        # Validação: SL deve estar afastado do preço (evitar SL em cima do preço)
        if side == "BUY" and sl_price >= price:
            sl_price = round(price - tick_size, digits)
        elif side == "SELL" and sl_price <= price:
            sl_price = round(price + tick_size, digits)

        if tp_pts:
            if side == "BUY":
                tp_price = round(price + tp_pts * point, digits)
            else:
                tp_price = round(price - tp_pts * point, digits)
        else:
            tp_price = 0

        # Ajustar volume
        vol = max(info.volume_min, round(volume / info.volume_step) * info.volume_step)
        vol = min(vol, info.volume_max)

        # Margem
        acc = mt5.account_info()
        margin_required = info.margin_initial * vol
        if margin_required > acc.margin_free:
            return {"error": f"margem insuficiente: precisa R${margin_required:,.2f}, tem R${acc.margin_free:,.2f}"}

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": vol,
            "type": mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL,
            "price": price,
            "sl": sl_price,
            "deviation": 10,
            "magic": 555501,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": type_filling,
        }
        if tp_price > 0:
            request["tp"] = tp_price

        log(f"{side} {symbol} vol={vol} price={price} sl={sl_price} ({cur_sl_pts}pts) [tentativa {attempt+1}]")
        result = mt5.order_send(request)

        if not result:
            last_err = mt5.last_error()
            log(f"order_send retornou None: {last_err}", "ERROR")
            return {"error": "order_send retornou None", "mt5_error": last_err}

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            log(f"✅ {side} executado | ticket={result.order} @ {result.price}")
            return {
                "retcode": result.retcode,
                "comment": result.comment,
                "ticket": result.order,
                "price": result.price,
                "volume": result.volume,
                "sl": sl_price,
                "tp": tp_price,
                "status": "FILLED",
            }

        # Se foi "Invalid stops", aumentar SL e tentar de novo
        if "Invalid stops" in str(result.comment):
            last_err = result.comment
            log(f"⚠️ {side} {symbol} Invalid stops (sl={sl_price} cur_sl_pts={cur_sl_pts}). Dobrando...", "WARN")
            cur_sl_pts = min(cur_sl_pts * 2, sl_pts * 3)  # Cap at 3x original SL
            time.sleep(0.1)
            continue

        # Outros erros, retornar
        return {
            "retcode": result.retcode,
            "comment": result.comment,
            "ticket": result.order,
            "price": result.price,
            "volume": result.volume,
            "sl": sl_price,
            "tp": tp_price,
            "status": "REJECTED",
        }

    return {"error": f"todas as tentativas falharam: {last_err}", "status": "REJECTED"}


def cmd_buy(symbol, volume, sl_pts=None, tp_pts=None, comment="VibeTrading"):
    """Ordem BUY com SL obrigatório e retry automático."""
    if sl_pts is None:
        if "WIN" in symbol:
            sl_pts = 200
        elif "WDO" in symbol:
            sl_pts = 200
        else:
            sl_pts = 200
    result = _try_send(symbol, "BUY", volume, sl_pts, tp_pts, comment)
    print(json.dumps(result, indent=2, default=str))
    return result.get("status") == "FILLED"


def cmd_sell(symbol, volume, sl_pts=None, tp_pts=None, comment="VibeTrading"):
    """Ordem SELL com SL obrigatório e retry automático."""
    if sl_pts is None:
        if "WIN" in symbol:
            sl_pts = 200
        elif "WDO" in symbol:
            sl_pts = 200
        else:
            sl_pts = 200
    result = _try_send(symbol, "SELL", volume, sl_pts, tp_pts, comment)
    print(json.dumps(result, indent=2, default=str))
    return result.get("status") == "FILLED"


def cmd_close(symbol):
    """Fecha todas posições abertas de um símbolo."""
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        print(json.dumps({"info": f"sem posições abertas em {symbol}"}))
        return True

    closed = 0
    results = []
    for pos in positions:
        tick = mt5.symbol_info_tick(pos.symbol)
        if not tick:
            log(f"sem tick para {pos.symbol}", "ERROR")
            continue

        if pos.type == mt5.ORDER_TYPE_BUY:
            price = tick.bid
            order_type = mt5.ORDER_TYPE_SELL
        else:
            price = tick.ask
            order_type = mt5.ORDER_TYPE_BUY

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": pos.volume,
            "type": order_type,
            "position": pos.ticket,
            "price": price,
            "deviation": 10,
            "magic": 555501,
            "comment": "VibeTrading-Close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": _get_filling_type(pos.symbol),
        }

        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            log(f"✅ Fechou {pos.symbol} ticket={pos.ticket} | PnL: R${pos.profit:+.2f}")
            closed += 1
            results.append({
                "ticket": pos.ticket,
                "symbol": pos.symbol,
                "type": "BUY" if pos.type == 0 else "SELL",
                "volume": pos.volume,
                "entry_price": pos.price_open,
                "close_price": result.price,
                "profit": pos.profit,
                "swap": pos.swap,
                "magic": pos.magic,
            })
        else:
            log(f"❌ Falha ao fechar {pos.ticket}: {result.comment if result else 'sem resultado'}", "ERROR")
            results.append({
                "ticket": pos.ticket,
                "symbol": pos.symbol,
                "error": result.comment if result else "sem resultado",
            })

    print(json.dumps({"status": "ok", "closed": closed, "total": len(positions), "details": results}))
    return closed == len(positions)


def cmd_close_all():
    """Fecha TODAS as posições."""
    positions = mt5.positions_get()
    if not positions:
        print(json.dumps({"status": "ok", "info": "nenhuma posição aberta"}))
        return True

    total = len(positions)
    closed = 0
    seen_symbols = set()
    for pos in positions:
        if pos.symbol in seen_symbols:
            continue  # já fechado
        seen_symbols.add(pos.symbol)
        if cmd_close(pos.symbol):
            closed += 1

    print(json.dumps({"status": "ok", "closed": closed, "total": total}))


def cmd_symbol_info(symbol):
    """Contract specs: point, digits, tick_size, tick_value, volume limits, margin, stops."""
    info = mt5.symbol_info(symbol)
    if not info:
        print(json.dumps({"error": f"símbolo {symbol} não encontrado"}))
        return
    print(json.dumps({
        "name": info.name,
        "point": info.point,
        "digits": info.digits,
        "tick_size": getattr(info, 'trade_tick_size', info.point),
        "tick_value": getattr(info, 'trade_tick_value', 0),
        "contract_size": info.trade_contract_size,
        "volume_min": info.volume_min,
        "volume_max": info.volume_max,
        "volume_step": info.volume_step,
        "margin_initial": info.margin_initial,
        "margin_maintenance": info.margin_maintenance,
        "trade_stops_level": info.trade_stops_level,
        "trade_freeze_level": info.trade_freeze_level,
        "swap_long": info.swap_long,
        "swap_short": info.swap_short,
        "currency_base": info.currency_base,
        "currency_profit": info.currency_profit,
        "trade_mode": info.trade_mode,
    }, indent=2))


def cmd_book(symbol):
    """Market depth (DOM / Level 2)."""
    book = mt5.market_book_get(symbol)
    if not book:
        print(json.dumps({"error": f"sem book para {symbol}"}))
        return
    depth = []
    for entry in book:
        depth.append({
            "type": entry.type,
            "price": entry.price,
            "volume": entry.volume,
        })
    print(json.dumps({"symbol": symbol, "book": depth}))


def cmd_orders():
    """Pending orders with full details."""
    orders = mt5.orders_get()
    if not orders:
        print(json.dumps({"orders": [], "count": 0}))
        return
    ord_list = []
    for o in orders:
        ord_list.append({
            "ticket": o.ticket,
            "symbol": o.symbol,
            "type": o.type,
            "type_time": o.type_time,
            "state": o.state,
            "volume_initial": o.volume_initial,
            "volume_current": o.volume_current,
            "price_open": o.price_open,
            "sl": o.sl,
            "tp": o.tp,
            "price_current": o.price_current,
            "magic": o.magic,
            "comment": o.comment,
            "time_setup": str(o.time_setup),
            "time_expiration": str(o.time_expiration) if o.time_expiration else None,
            "time_done": str(o.time_done) if o.time_done else None,
            "reason": o.reason,
            "position_id": o.position_id,
        })
    print(json.dumps({"orders": ord_list, "count": len(ord_list)}))


def cmd_modify(symbol, ticket, new_sl_pts):
    """Modifica o Stop Loss de uma posição aberta."""
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        print(json.dumps({"error": f"sem posições abertas em {symbol}"}))
        return False

    # Encontrar a posição pelo ticket
    target_pos = None
    for pos in positions:
        if pos.ticket == int(ticket):
            target_pos = pos
            break

    if not target_pos:
        print(json.dumps({"error": f"ticket {ticket} não encontrado em {symbol}"}))
        return False

    # Calcular novo preço de SL
    sym_info = mt5.symbol_info(symbol)
    point = sym_info.point
    tick_size = getattr(sym_info, 'trade_tick_size', None) or point
    digits = getattr(sym_info, 'digits', 5)
    if target_pos.type == mt5.ORDER_TYPE_BUY:
        # BUY: SL abaixo do preço
        new_sl_price = target_pos.price_open - (int(new_sl_pts) * point)
    else:
        # SELL: SL acima do preço
        new_sl_price = target_pos.price_open + (int(new_sl_pts) * point)

    # Alinhar SL ao trade_tick_size (evita "Invalid stops" no servidor)
    if tick_size > 0:
        new_sl_price = round(new_sl_price / tick_size) * tick_size
        new_sl_price = round(new_sl_price, digits)
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": target_pos.symbol,
        "position": target_pos.ticket,
        "sl": new_sl_price,
        "tp": target_pos.tp,  # Manter TP existente
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": _get_filling_type(target_pos.symbol),
    }

    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        log(f"✅ SL modificado: {symbol} ticket={ticket} → SL={new_sl_price:.5f}")
        print(json.dumps({"status": "ok", "ticket": ticket, "new_sl": new_sl_price}))
        return True
    else:
        error_msg = result.comment if result else "sem resultado"
        log(f"❌ Falha modify SL {ticket}: {error_msg}", "ERROR")
        print(json.dumps({"error": error_msg}))
        return False


def cmd_tick(symbol):
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        print(json.dumps({"error": f"sem tick para {symbol}"}))
        return
    print(json.dumps({
        "symbol": symbol,
        "bid": tick.bid,
        "ask": tick.ask,
        "last": tick.last,
        "volume": tick.volume,
        "volume_real": getattr(tick, 'volume_real', 0),
        "time": str(tick.time),
        "time_msc": getattr(tick, 'time_msc', 0),
        "flags": tick.flags,
    }, indent=2))


def cmd_symbols(filter_str=None):
    symbols = mt5.symbols_get()
    if filter_str:
        symbols = [s for s in symbols if filter_str.upper() in s.name.upper()]

    out = []
    for s in symbols:
        if s.trade_mode == mt5.SYMBOL_TRADE_MODE_FULL:
            tick = mt5.symbol_info_tick(s.name)
            out.append({
                "name": s.name,
                "path": s.path,
                "bid": tick.bid if tick else 0,
                "ask": tick.ask if tick else 0,
                "volume_min": s.volume_min,
                "volume_max": s.volume_max,
                "margin_initial": s.margin_initial,
                "point": s.point,
                "digits": s.digits,
            })
    print(json.dumps(out, indent=2))


def cmd_info(symbol):
    info = mt5.symbol_info(symbol)
    if not info:
        print(json.dumps({"error": f"símbolo {symbol} não encontrado"}))
        return
    tick = mt5.symbol_info_tick(symbol)
    print(json.dumps({
        "name": info.name,
        "path": info.path,
        "currency_base": info.currency_base,
        "currency_profit": info.currency_profit,
        "digits": info.digits,
        "point": info.point,
        "trade_contract_size": info.trade_contract_size,
        "volume_min": info.volume_min,
        "volume_max": info.volume_max,
        "volume_step": info.volume_step,
        "margin_initial": info.margin_initial,
        "margin_maintenance": info.margin_maintenance,
        "trade_mode": info.trade_mode,
        "trade_stops_level": info.trade_stops_level,
        "swap_long": info.swap_long,
        "swap_short": info.swap_short,
        "bid": tick.bid if tick else 0,
        "ask": tick.ask if tick else 0,
        "spread": (tick.ask - tick.bid) if tick else 0,
    }, indent=2))


def cmd_bars(symbol, tf_str="M5", count=50):
    """Busca barras OHLCV do MT5."""
    tf_map = {
        "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
    }
    tf = tf_map.get(tf_str, mt5.TIMEFRAME_M5)

    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None or len(rates) == 0:
        print(json.dumps({"error": f"sem dados para {symbol} {tf_str}"}))
        return

    bars = []
    for r in rates:
        bars.append({
            "time": int(r["time"]),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": int(r["tick_volume"]),
            "real_volume": int(r["real_volume"]),
        })
    print(json.dumps({"symbol": symbol, "timeframe": tf_str, "bars": bars}))


def cmd_history(symbol=None, days=7):
    """Busca histórico de deals/posições do MT5 (últimos N dias)."""
    from datetime import datetime, timedelta
    since = datetime.now() - timedelta(days=days)
    timestamp = int(since.timestamp())

    if symbol:
        deals = mt5.history_deals_get(symbol=symbol, date_from=timestamp)
    else:
        deals = mt5.history_deals_get(date_from=timestamp)

    if not deals:
        print(json.dumps({"history": [], "info": f"sem deals desde {since.strftime('%d/%m/%Y')}"}))
        return

    history = []
    for d in deals:
        history.append({
            "ticket": d.ticket,
            "symbol": d.symbol,
            "type": "BUY" if d.type == 0 else "SELL",
            "deal_type": d.reason,
            "volume": d.volume,
            "price": d.price,
            "profit": d.profit,
            "swap": d.swap,
            "commission": d.commission,
            "fee": getattr(d, 'fee', 0.0),
            "comment": d.comment,
            "magic": d.magic,
            "time": str(d.time),
            "time_msc": d.time_msc,
            "position_id": d.position_id,
            "entry_id": d.position_id,
        })
    print(json.dumps({"history": history, "count": len(history)}))


def main():
    if not init():
        sys.exit(1)

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1].lower()

    try:
        if cmd == "status":
            cmd_status()
        elif cmd == "buy":
            symbol = sys.argv[2]
            volume = float(sys.argv[3])
            sl_pts = int(sys.argv[4]) if len(sys.argv) > 4 else None
            tp_pts = int(sys.argv[5]) if len(sys.argv) > 5 else None
            cmd_buy(symbol, volume, sl_pts, tp_pts)
        elif cmd == "sell":
            symbol = sys.argv[2]
            volume = float(sys.argv[3])
            sl_pts = int(sys.argv[4]) if len(sys.argv) > 4 else None
            tp_pts = int(sys.argv[5]) if len(sys.argv) > 5 else None
            cmd_sell(symbol, volume, sl_pts, tp_pts)
        elif cmd == "close":
            symbol = sys.argv[2]
            cmd_close(symbol)
        elif cmd == "close_all":
            cmd_close_all()
        elif cmd == "tick":
            symbol = sys.argv[2]
            cmd_tick(symbol)
        elif cmd == "symbols":
            filter_str = sys.argv[2] if len(sys.argv) > 2 else None
            cmd_symbols(filter_str)
        elif cmd == "info":
            symbol = sys.argv[2]
            cmd_info(symbol)
        elif cmd == "symbol_info":
            symbol = sys.argv[2]
            cmd_symbol_info(symbol)
        elif cmd == "book":
            symbol = sys.argv[2]
            cmd_book(symbol)
        elif cmd == "orders":
            cmd_orders()
        elif cmd == "bars":
            symbol = sys.argv[2]
            tf_str = sys.argv[3] if len(sys.argv) > 3 else "M5"
            count = int(sys.argv[4]) if len(sys.argv) > 4 else 50
            cmd_bars(symbol, tf_str, count)
        elif cmd == "history":
            sym = sys.argv[2] if len(sys.argv) > 2 else None
            days = int(sys.argv[3]) if len(sys.argv) > 3 else 7
            cmd_history(sym, days)
        elif cmd == "modify":
            symbol = sys.argv[2]
            ticket = sys.argv[3]
            new_sl_pts = int(sys.argv[4])
            cmd_modify(symbol, ticket, new_sl_pts)
        else:
            print(f"Comando desconhecido: {cmd}")
            print(__doc__)
            sys.exit(1)
    except Exception as e:
        log(f"Erro: {e}\n{traceback.format_exc()}", "ERROR")
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
