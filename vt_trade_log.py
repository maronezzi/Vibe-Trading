"""
Vibe-Trading Trade Log — SQLite para registro de operações.
Gera dados para o Imposto de Renda (Receita Federal).

Regras IR Brasil (Day Trade):
- Alíquota: 20% sobre o lucro líquido de day trades
- Compensação: prejuízos de day trade podem ser deduzidos de lucros futuros
- Isenção: não há isenção para day trade (pago até R$ 20.000/mês é IR normal)

Campos necessários:
- Data/hora entrada e saída
- Ativo (WINQ26, WDOQ26)
- Quantidade
- Preço entrada/saída
- Taxas (emolumentos, corretagem)
- PnL real (com taxas)
"""

import sqlite3
import json
from datetime import datetime, date
from pathlib import Path
from typing import Optional

DB_PATH = Path("/home/bruno/Projects/Vibe-Trading/vt_trades.db")
NOTIFICATION_FILE = Path("/tmp/vt_notifications.jsonl")  # fila de notificações


def get_db() -> sqlite3.Connection:
    """Retorna conexão com o banco."""
    conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Cria as tabelas se não existirem."""
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            -- Identificação MT5
            entry_ticket TEXT,
            exit_ticket TEXT,
            magic_number INTEGER DEFAULT 555501,

            -- Dados da operação
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL CHECK(direction IN ('BUY', 'SELL')),
            volume REAL NOT NULL,
            timeframe TEXT DEFAULT 'M5',

            -- Entrada
            entry_time TEXT NOT NULL,
            entry_price REAL NOT NULL,
            entry_sl REAL,

            -- Saída
            exit_time TEXT,
            exit_price REAL,
            exit_reason TEXT,        -- 'TRAILING', 'SL', 'EOD_16:45', 'SIGNAL', 'MANUAL'
            exit_sl_price REAL,     -- SL no momento da saída

            -- Financeiro
            gross_pnl REAL DEFAULT 0,    -- PnL bruto (antes taxas)
            fees REAL DEFAULT 0,         -- Taxas estimadas
            swap REAL DEFAULT 0,         -- Swap cobrado
            net_pnl REAL DEFAULT 0,      -- PnL líquido (com taxas)

            -- Classificação IR
            is_day_trade INTEGER DEFAULT 1,  -- 1=day trade, 0=swing
            asset_type TEXT DEFAULT 'FUTURE', -- FUTURE, MINI

            -- Multiplicador financeiro (R$ por ponto)
            multiplier REAL DEFAULT 0.20,   -- WIN$=R$0.20/pt, WDO$=R$10.00/pt

            -- Metadados
            strategy TEXT DEFAULT 'VWAP',
            signal_detail TEXT,       -- JSON: VWAP, ATR, thresholds
            raw_entry_json TEXT,       -- JSON completo da entrada
            raw_exit_json TEXT,        -- JSON completo da saída
            notes TEXT,

            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT DEFAULT (datetime('now', 'localtime'))
        );

        CREATE TABLE IF NOT EXISTS daily_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            n_trades INTEGER DEFAULT 0,
            n_winners INTEGER DEFAULT 0,
            n_losers INTEGER DEFAULT 0,
            gross_pnl REAL DEFAULT 0,
            fees REAL DEFAULT 0,
            net_pnl REAL DEFAULT 0,
            max_win REAL DEFAULT 0,
            max_loss REAL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(date, symbol)
        );

        CREATE TABLE IF NOT EXISTS trade_history_from_mt5 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket TEXT NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT,
            volume REAL,
            price_open REAL,
            price_close REAL,
            price_current REAL,
            sl REAL,
            tp REAL,
            profit REAL,
            swap REAL,
            commission REAL,
            comment TEXT,
            magic INTEGER,
            time_open TEXT,
            time_close TEXT,
            reason TEXT,
            fetched_at TEXT DEFAULT (datetime('now', 'localtime'))
        );

        CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
        CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(entry_time);
        CREATE INDEX IF NOT EXISTS idx_trades_exit_reason ON trades(exit_reason);
        CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_summary(date);
    """)
    conn.commit()
    conn.close()


def get_multiplier(symbol: str) -> float:
    """Retorna o multiplicador R$ por ponto — lê de vt_config.json contract_specs."""
    try:
        from vt_config_loader import load_config
        _cfg = load_config()
        specs = _cfg.get("contract_specs", {})
        # Tenta match por root$ (ex: "WIN$", "WDO$", "BIT$")
        for root, spec in specs.items():
            if root.rstrip("$") in symbol:
                return spec.get("mult", 1.0)
    except Exception:
        pass
    # Fallback hardcoded (caso config indisponível) — valores REAIS confirmados via PnLs
    _mults = {"WIN": 0.20, "WDO": 10.00, "DOL": 1.00, "IND": 1.0, "BIT": 1.0, "WSP": 1.0}
    for root, mult in _mults.items():
        if root in symbol:
            return mult
    return 1.0


def calc_fees(volume: float, entry_price: float, exit_price: float) -> float:
    """
    Estima taxas B3 para mini contrato.
    Emolumentos B3: ~R$ 1,20 por contrato (mini índice)
    Emolumentos B3: ~R$ 0,60 por contrato (mini dólar)
    Corretagem XP: R$ 0 (zero para mini)
    """
    return volume * 1.20  # estimativa conservadora


def log_entry(symbol: str, direction: str, volume: float,
              entry_price: float, entry_sl: float,
              entry_ticket: str, timeframe: str = "M5",
              strategy: str = "VWAP", signal_detail: dict = None,
              raw_json: dict = None) -> int:
    """Registra ABERTURA de uma posição."""
    conn = get_db()
    multiplier = get_multiplier(symbol)
    cur = conn.execute("""
        INSERT INTO trades (symbol, direction, volume, entry_time, entry_price,
                           entry_sl, entry_ticket, timeframe, strategy,
                           signal_detail, raw_entry_json, multiplier)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        symbol, direction, volume,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        entry_price, entry_sl, str(entry_ticket),
        timeframe, strategy,
        json.dumps(signal_detail, default=str) if signal_detail else None,
        json.dumps(raw_json, default=str) if raw_json else None,
        multiplier,
    ))
    trade_id = cur.lastrowid
    conn.commit()
    conn.close()

    _queue_notification("ENTRY", symbol, direction, volume, entry_price, entry_sl, entry_ticket)
    return trade_id


def log_exit(trade_id: int, exit_price: float, exit_reason: str,
             exit_ticket: str = None, exit_sl_price: float = None,
             swap: float = 0, notes: str = None, raw_json: dict = None):
    """Registra FECHAMENTO de uma posição e calcula PnL."""
    conn = get_db()
    row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    if not row:
        conn.close()
        return None

    symbol = row["symbol"]
    volume = row["volume"]
    entry_price = row["entry_price"]
    multiplier = row["multiplier"]

    # Calcular PnL
    if row["direction"] == "BUY":
        gross_pts = exit_price - entry_price
    else:  # SELL
        gross_pts = entry_price - exit_price

    fees = calc_fees(volume, entry_price, exit_price)
    gross_pnl = gross_pts * multiplier * volume
    net_pnl = gross_pnl - fees + swap

    conn.execute("""
        UPDATE trades SET
            exit_time = ?,
            exit_price = ?,
            exit_reason = ?,
            exit_ticket = ?,
            exit_sl_price = ?,
            swap = ?,
            gross_pnl = ?,
            fees = ?,
            net_pnl = ?,
            notes = ?,
            raw_exit_json = ?,
            updated_at = datetime('now', 'localtime')
        WHERE id = ?
    """, (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        exit_price, exit_reason, str(exit_ticket) if exit_ticket else None,
        exit_sl_price, swap, gross_pnl, fees, net_pnl,
        notes,
        json.dumps(raw_json, default=str) if raw_json else None,
        trade_id,
    ))

    # Atualizar resumo diário
    today = datetime.now().strftime("%Y-%m-%d")
    _update_daily_summary(conn, today, symbol, gross_pnl, fees, net_pnl)

    conn.commit()
    conn.close()

    _queue_notification("EXIT", symbol, row["direction"], volume, exit_price,
                        exit_reason, net_pnl, gross_pnl, fees, gross_pts, trade_id)
    return {"net_pnl": net_pnl, "gross_pnl": gross_pnl, "fees": fees, "points": gross_pts}


def _update_daily_summary(conn, today: str, symbol: str,
                          gross_pnl: float, fees: float, net_pnl: float):
    """Atualiza resumo diário."""
    existing = conn.execute(
        "SELECT * FROM daily_summary WHERE date = ? AND symbol = ?", (today, symbol)
    ).fetchone()

    if existing:
        conn.execute("""
            UPDATE daily_summary SET
                n_trades = n_trades + 1,
                n_winners = n_winners + CASE WHEN ? > 0 THEN 1 ELSE 0 END,
                n_losers = n_losers + CASE WHEN ? <= 0 THEN 1 ELSE 0 END,
                gross_pnl = gross_pnl + ?,
                fees = fees + ?,
                net_pnl = net_pnl + ?,
                max_win = MAX(max_win, ?),
                max_loss = MIN(max_loss, ?)
            WHERE date = ? AND symbol = ?
        """, (net_pnl, net_pnl, gross_pnl, fees, net_pnl, net_pnl, net_pnl, today, symbol))
    else:
        conn.execute("""
            INSERT INTO daily_summary (date, symbol, n_trades, n_winners, n_losers,
                                     gross_pnl, fees, net_pnl, max_win, max_loss)
            VALUES (?, ?, 1, CASE WHEN ? > 0 THEN 1 ELSE 0 END,
                   CASE WHEN ? <= 0 THEN 1 ELSE 0 END, ?, ?, ?, ?, ?)
        """, (today, symbol, net_pnl, net_pnl, gross_pnl, fees, net_pnl, net_pnl, net_pnl))


def _queue_notification(event: str, symbol: str, direction: str, volume: float,
                        price: float, reason_or_sl, *args):
    """Escreve notificação em fila para o autotrader enviar."""
    import json as j
    notif = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "event": event,
        "symbol": symbol,
        "direction": direction,
        "volume": volume,
        "price": price,
    }
    if event == "ENTRY":
        notif["sl"] = reason_or_sl
        notif["ticket"] = str(args[0]) if args else ""
    elif event == "EXIT":
        notif["reason"] = reason_or_sl
        # args = (net_pnl, gross_pnl, fees, gross_pts, trade_id) — alinhado com log_exit L243
        notif["net_pnl"] = args[0] if len(args) > 0 else 0
        notif["gross_pnl"] = args[1] if len(args) > 1 else 0
        notif["fees"] = args[2] if len(args) > 2 else 0
        notif["points"] = args[3] if len(args) > 3 else 0

    try:
        with open(NOTIFICATION_FILE, "a") as f:
            f.write(j.dumps(notif, ensure_ascii=False) + "\n")
    except Exception:
        pass


def import_mt5_history(positions_data: list):
    """
    Importa posições fechadas do MT5 para o histórico.
    positions_data: lista de dicts com campos da posição MT5.
    """
    conn = get_db()
    for pos in positions_data:
        ticket = str(pos.get("ticket", ""))
        symbol = pos.get("symbol", "")
        conn.execute("""
            INSERT OR REPLACE INTO trade_history_from_mt5
            (ticket, symbol, direction, volume, price_open, price_close,
             price_current, sl, tp, profit, swap, commission, comment,
             magic, time_open, time_close, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            ticket, symbol,
            "BUY" if pos.get("type") == 0 else "SELL",
            pos.get("volume", 0), pos.get("price_open", 0),
            pos.get("price_close", 0), pos.get("price_current", 0),
            pos.get("sl", 0), pos.get("tp", 0),
            pos.get("profit", 0), pos.get("swap", 0),
            pos.get("commission", 0), pos.get("comment", ""),
            pos.get("magic", 0),
            str(pos.get("time_open", "")),
            str(pos.get("time_close", "")),
            "MT5_IMPORTED",
        ))
    conn.commit()
    conn.close()


def get_daily_summary(date_str: str = None) -> dict:
    """
    Retorna resumo do dia: trades fechados, posições abertas (do MT5),
    PnL acumulado, melhor/pior trade.
    date_str: 'YYYY-MM-DD' ou None (hoje).
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    conn = get_db()
    try:
        # Trades fechados hoje
        trades = conn.execute("""
            SELECT id, symbol, direction, volume, timeframe,
                   entry_time, entry_price, entry_sl,
                   exit_time, exit_price, exit_reason,
                   gross_pnl, fees, net_pnl
            FROM trades
            WHERE date(entry_time) = ?
            ORDER BY id
        """, (date_str,)).fetchall()

        # Resumo diário por símbolo
        summaries = conn.execute("""
            SELECT * FROM daily_summary
            WHERE date = ?
        """, (date_str,)).fetchall()

        total_trades = len(trades)
        closed_trades = [t for t in trades if t["exit_time"] is not None]
        open_trades_db = [t for t in trades if t["exit_time"] is None]

        gross = sum(t["gross_pnl"] or 0 for t in closed_trades)
        fees = sum(t["fees"] or 0 for t in closed_trades)
        net = sum(t["net_pnl"] or 0 for t in closed_trades)
        wins = sum(1 for t in closed_trades if (t["net_pnl"] or 0) > 0)
        losses = sum(1 for t in closed_trades if (t["net_pnl"] or 0) <= 0)

        best = max((t["net_pnl"] or 0 for t in closed_trades), default=0)
        worst = min((t["net_pnl"] or 0 for t in closed_trades), default=0)

        return {
            "date": date_str,
            "total_trades": total_trades,
            "closed": len(closed_trades),
            "open_db": len(open_trades_db),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / max(len(closed_trades), 1) * 100, 1),
            "gross_pnl": round(gross, 2),
            "fees": round(fees, 2),
            "net_pnl": round(net, 2),
            "best_trade": round(best, 2),
            "worst_trade": round(worst, 2),
            "trades": [dict(t) for t in trades],
            "summaries": [dict(s) for s in summaries],
        }
    finally:
        conn.close()


def get_tax_report(month: int = None, year: int = None) -> dict:
    """
    Gera relatório para Imposto de Renda.
    Se month/year None, usa mês atual.
    """
    if month is None:
        month = datetime.now().month
    if year is None:
        year = datetime.now().year

    conn = get_db()

    # Trades fechados no mês
    rows = conn.execute("""
        SELECT * FROM trades
        WHERE exit_time IS NOT NULL
        AND strftime('%Y-%m', exit_time) = ?
        ORDER BY exit_time ASC
    """, (f"{year:04d}-{month:02d}",)).fetchall()

    trades = []
    total_gross = 0
    total_fees = 0
    total_net = 0
    total_wins = 0
    total_losses = 0

    for r in rows:
        gross = r["gross_pnl"]
        net = r["net_pnl"]
        total_gross += gross
        total_fees += r["fees"]
        total_net += net
        if net > 0:
            total_wins += 1
        else:
            total_losses += 1

        trades.append({
            "id": r["id"],
            "symbol": r["symbol"],
            "direction": r["direction"],
            "volume": r["volume"],
            "entry_time": r["entry_time"],
            "exit_time": r["exit_time"],
            "entry_price": r["entry_price"],
            "exit_price": r["exit_price"],
            "exit_reason": r["exit_reason"],
            "gross_pnl": round(gross, 2),
            "fees": round(r["fees"], 2),
            "swap": round(r["swap"], 2),
            "net_pnl": round(net, 2),
            "is_day_trade": r["is_day_trade"],
        })

    # Prejuízos acumulados de meses anteriores (para compensação)
    prev_losses = conn.execute("""
        SELECT COALESCE(SUM(CASE WHEN net_pnl < 0 THEN net_pnl ELSE 0 END), 0) as total
        FROM trades
        WHERE exit_time IS NOT NULL
        AND strftime('%Y-%m', exit_time) < ?
    """, (f"{year:04d}-{month:02d}",)).fetchone()["total"]

    # Resumo diário do mês
    daily = conn.execute("""
        SELECT * FROM daily_summary
        WHERE strftime('%Y-%m', date) = ?
        ORDER BY date ASC
    """, (f"{year:04d}-{month:02d}",)).fetchall()
    daily_list = [dict(d) for d in daily]

    conn.close()

    # Cálculo IR
    ir_rate = 0.20  # 20% para day trade
    ir_due = max(0, total_net * ir_rate) if total_net > 0 else 0
    compensable_loss = min(abs(prev_losses), total_net) if total_net > 0 else 0
    remaining_loss = abs(prev_losses) - compensable_loss

    return {
        "period": f"{month:02d}/{year}",
        "n_trades": len(trades),
        "n_winners": total_wins,
        "n_losers": total_losses,
        "total_gross_pnl": round(total_gross, 2),
        "total_fees": round(total_fees, 2),
        "total_net_pnl": round(total_net, 2),
        "win_rate": round(total_wins / len(trades) * 100, 1) if trades else 0,
        "ir_rate": f"{ir_rate*100:.0f}%",
        "ir_due": round(ir_due, 2),
        "prev_losses_compensable": round(abs(prev_losses), 2),
        "compensated_this_month": round(compensable_loss, 2),
        "remaining_loss_carry": round(remaining_loss, 2),
        "trades": trades,
        "daily_summary": daily_list,
    }


def export_csv(month: int = None, year: int = None, filepath: str = None) -> str:
    """Exporta trades em CSV para planilha/IR."""
    import csv
    report = get_tax_report(month, year)
    if not filepath:
        filepath = f"/tmp/vt_ir_{report['period'].replace('/', '-')}.csv"

    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow([
            "ID", "Ativo", "Direção", "Qtd", "Data Entrada", "Preço Entrada",
            "Data Saída", "Preço Saída", "Motivo Saída",
            "PnL Bruto", "Taxas", "Swap", "PnL Líquido", "Day Trade?"
        ])
        for t in report["trades"]:
            writer.writerow([
                t["id"], t["symbol"], t["direction"], t["volume"],
                t["entry_time"], t["entry_price"],
                t["exit_time"], t["exit_price"], t["exit_reason"],
                t["gross_pnl"], t["fees"], t["swap"], t["net_pnl"],
                "SIM" if t["is_day_trade"] else "NÃO"
            ])
    return filepath


if __name__ == "__main__":
    init_db()
    print("Trade log inicializado em", DB_PATH)

    # Teste
    tid = log_entry("WINQ26", "BUY", 1.0, 175000.0, 174800.0, "12345",
                     signal_detail={"vwap": 174500, "atr": 246})
    print(f"Trade #{tid} registrado")

    log_exit(tid, 175500.0, "TRAILING", exit_ticket="12346",
             swap=0.5, notes="Teste")
    print("Trade fechado")

    report = get_tax_report()
    print(f"\nRelatório IR: {report['n_trades']} trades, PnL líquido R$ {report['total_net_pnl']:.2f}")
    print(f"IR devido (20%): R$ {report['ir_due']:.2f}")

    csv_path = export_csv()
    print(f"CSV exportado: {csv_path}")
