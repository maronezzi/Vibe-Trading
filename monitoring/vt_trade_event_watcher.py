#!/usr/bin/env python3
"""
Vibe-Trading Trade Event Watcher
=================================
Faz tail do CSV escrito pelo EA VibeTrading_TradeLogger.mq5
e ingere os eventos no SQLite (vt_trades.db → mt5_trade_events).

Uso:
    python3 monitoring/vt_trade_event_watcher.py              # foreground
    python3 monitoring/vt_trade_event_watcher.py --daemon     # background (nohup)
    python3 monitoring/vt_trade_event_watcher.py --once       # processa linhas existentes e sai

O CSV fica em:
    ~/.wine/drive_c/Program Files/MetaTrader 5 Terminal/MQL5/Files/vt_trade_events.csv

Pipe-delimited. Header na primeira linha. O EA faz FileFlush() a cada evento,
então inotify/polling de 1s é suficiente.
"""

import argparse
import csv
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

# ===== Config =====
MT5_FILES_DIR = os.path.expanduser(
    "~/.wine/drive_c/Program Files/MetaTrader 5 Terminal/MQL5/Files"
)
CSV_FILENAME = "vt_trade_events.csv"
CSV_PATH = os.path.join(MT5_FILES_DIR, CSV_FILENAME)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "vt_trades.db"

POLL_INTERVAL = 1.0  # segundos
BATCH_SIZE = 50       # commit a cada N linhas
REOPEN_CHECK = 30     # verifica se arquivo foi rotacionado a cada N segundos

# Colunas do CSV (ordem do header do EA)
CSV_COLUMNS = [
    "seq", "event_time", "trans_type", "order_ticket", "deal_ticket",
    "symbol", "order_type", "order_state", "volume", "price", "sl", "tp",
    "deal_type", "deal_entry", "deal_profit", "deal_commission", "deal_swap",
    "deal_price", "deal_volume", "position_ticket", "comment",
]

# Tipos para conversão
INT_FIELDS = {"seq", "order_ticket", "deal_ticket", "position_ticket"}
FLOAT_FIELDS = {"volume", "price", "sl", "tp", "deal_profit",
                "deal_commission", "deal_swap", "deal_price", "deal_volume"}

running = True


def signal_handler(sig, frame):
    global running
    print(f"\n[{now()}] Sinal {sig} recebido, encerrando...")
    running = False


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_line(line: str) -> dict | None:
    """Parse de uma linha pipe-delimited → dict com tipos corretos."""
    line = line.strip()
    if not line:
        return None

    parts = line.split("|")
    if len(parts) < len(CSV_COLUMNS):
        # Linha incompleta (EA ainda escrevendo?) — skip
        return None

    # Se tem campos extras (comment com pipe que escapou), junta o resto
    if len(parts) > len(CSV_COLUMNS):
        parts = parts[:len(CSV_COLUMNS) - 1] + ["|".join(parts[len(CSV_COLUMNS) - 1:])]

    row = {}
    for i, col in enumerate(CSV_COLUMNS):
        val = parts[i].strip()
        if col in INT_FIELDS:
            try:
                row[col] = int(val) if val else None
            except ValueError:
                row[col] = None
        elif col in FLOAT_FIELDS:
            try:
                row[col] = float(val) if val else None
            except ValueError:
                row[col] = None
        else:
            row[col] = val if val else None

    return row


def insert_event(conn: sqlite3.Connection, row: dict) -> bool:
    """INSERT OR IGNORE (dedup pela UNIQUE constraint)."""
    try:
        conn.execute(
            """INSERT OR IGNORE INTO mt5_trade_events
               (seq, event_time, trans_type, order_ticket, deal_ticket,
                symbol, order_type, order_state, volume, price, sl, tp,
                deal_type, deal_entry, deal_profit, deal_commission, deal_swap,
                deal_price, deal_volume, position_ticket, comment)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["seq"], row["event_time"], row["trans_type"],
                row["order_ticket"], row["deal_ticket"],
                row["symbol"], row["order_type"], row["order_state"],
                row["volume"], row["price"], row["sl"], row["tp"],
                row["deal_type"], row["deal_entry"],
                row["deal_profit"], row["deal_commission"], row["deal_swap"],
                row["deal_price"], row["deal_volume"],
                row["position_ticket"], row["comment"],
            ),
        )
        return conn.total_changes > 0
    except sqlite3.Error as e:
        print(f"[{now()}] ERRO SQLite: {e}")
        return False


def process_existing(conn: sqlite3.Connection) -> int:
    """Processa todas as linhas existentes no CSV. Retorna nº de linhas novas."""
    if not os.path.exists(CSV_PATH):
        print(f"[{now()}] CSV não encontrado: {CSV_PATH}")
        return 0

    count = 0
    with open(CSV_PATH, "r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i == 0 and line.startswith("seq|"):
                continue  # header
            row = parse_line(line)
            if row and insert_event(conn, row):
                count += 1
    conn.commit()
    return count


def tail_watch(conn: sqlite3.Connection):
    """Loop principal: tail do CSV com polling."""
    global running

    print(f"[{now()}] Watcher iniciado")
    print(f"[{now()}] CSV: {CSV_PATH}")
    print(f"[{now()}] DB:  {DB_PATH}")
    print(f"[{now()}] Poll: {POLL_INTERVAL}s | Batch: {BATCH_SIZE}")

    # Processa backlog inicial
    n = process_existing(conn)
    if n > 0:
        print(f"[{now()}] Backlog: {n} eventos novos ingeridos")

    # Posição de leitura
    try:
        file_size = os.path.getsize(CSV_PATH) if os.path.exists(CSV_PATH) else 0
    except OSError:
        file_size = 0

    last_reopen_check = time.time()
    batch_count = 0
    total_ingested = n

    while running:
        try:
            # Verifica se arquivo existe / foi rotacionado
            if not os.path.exists(CSV_PATH):
                time.sleep(POLL_INTERVAL)
                continue

            current_size = os.path.getsize(CSV_PATH)

            # Rotação: arquivo menor que nossa posição → reabre do início
            if current_size < file_size:
                print(f"[{now()}] CSV rotacionado ({file_size} → {current_size}), relendo")
                file_size = 0

            if current_size > file_size:
                with open(CSV_PATH, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(file_size)
                    new_data = f.read()
                    file_size = f.tell()

                lines = new_data.split("\n")
                for line in lines:
                    line = line.strip()
                    if not line or line.startswith("seq|"):
                        continue
                    row = parse_line(line)
                    if row:
                        if insert_event(conn, row):
                            batch_count += 1
                            total_ingested += 1

                if batch_count >= BATCH_SIZE:
                    conn.commit()
                    print(f"[{now()}] +{batch_count} eventos (total: {total_ingested})")
                    batch_count = 0

            # Commit periódico mesmo com batch pequeno
            if batch_count > 0:
                conn.commit()
                batch_count = 0

            # Log de status periódico
            if time.time() - last_reopen_check > REOPEN_CHECK:
                last_reopen_check = time.time()
                # Verifica se o inode mudou (rotação por rename)
                try:
                    st = os.stat(CSV_PATH)
                    # Se o arquivo é novo (inode diferente), reset
                    # Simples: se size < file_size, já tratamos acima
                except OSError:
                    pass

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[{now()}] ERRO: {e}")
            time.sleep(5)

    # Flush final
    conn.commit()
    print(f"[{now()}] Watcher encerrado. Total ingerido: {total_ingested}")


def main():
    parser = argparse.ArgumentParser(description="Vibe-Trading Trade Event Watcher")
    parser.add_argument("--once", action="store_true",
                        help="Processa linhas existentes e sai")
    parser.add_argument("--daemon", action="store_true",
                        help="Modo daemon (printa PID)")
    parser.add_argument("--csv", type=str, default=None,
                        help="Override caminho do CSV")
    parser.add_argument("--db", type=str, default=None,
                        help="Override caminho do DB")
    args = parser.parse_args()

    global CSV_PATH, DB_PATH
    if args.csv:
        CSV_PATH = args.csv
    if args.db:
        DB_PATH = Path(args.db)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")

    try:
        if args.once:
            n = process_existing(conn)
            print(f"[{now()}] --once: {n} eventos novos")
        else:
            if args.daemon:
                print(f"[{now()}] PID: {os.getpid()}")
            tail_watch(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
