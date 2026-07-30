#!/usr/bin/env python3
"""
vt_repair_trades_20260717.py
============================
Repair broker-truth para o dia 2026-07-17.

Problema detectado (DRY-RUN):
- DB vt_trades.db tem 23 linhas de trades hoje, PnL total = -R$ 1.603,50
- MT5 broker-truth diz PnL realizado do dia = -R$ 179,00 (saldo 1.001.518,95 vs
  inicial 1.001.697,95, diff = -R$ 179,00)
- Divergência = -R$ 1.424,50 (DB inflado por linhas órfãs + PnL divergente)

Plano de repair (NÃO EXECUTA — dry-run):
  1) Backup do DB → /home/bruno/Projects/Vibe-Trading/vt_trades.db.bak.pre_repair_20260717_HHMMSS
  2) Para CADA position do MT5 hoje (9 positions):
     - Inserir uma linha reconstruída por deal não-zero (close) usando profit/price do broker
     - entry_time e exit_time do MT5
     - close_source='MT5_BROKER_TRUTH_REPAIR_20260717'
  3) Marcar linhas do DB existentes como órfãs quando:
     - entry_ticket não existe no MT5 (14 linhas GHOST)
     OU
     - entry_ticket existe mas net_pnl diverge do total MT5 da position
       (4 linhas SL_SERVIDOR/BROKER_CLOSE com mult errado)
     → SET strategy = strategy + ' [ORPHAN_CLOSING]', notes += ' | ORPHAN_CLOSING'
  4) Recalcular daily_summary para 2026-07-17 baseado nas linhas reconstruídas
     (ou seja, com PnL=-179.00 em vez de -1.603,50)

Saída:
- Log em /tmp/vt_repair_20260717.log
- Imprime SQL que VAI rodar (mas não roda — dry-run)
- Resumo final: X linhas reconstruídas, Y marcadas órfãs, Z delete candidates

Uso:
  python3 scripts/vt_repair_trades_20260717.py --dry-run   # PADRÃO: mostra SQL
  python3 scripts/vt_repair_trades_20260717.py --execute    # EXECUTA de verdade (cuidado)
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import shutil
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path("/home/bruno/Projects/Vibe-Trading")
DB_PATH = PROJECT_ROOT / "vt_trades.db"
TRUTH_CACHE = Path("/tmp/vt_mt5_truth_20260717.pkl")
LOG_PATH = Path("/tmp/vt_repair_20260717.log")
TRUTH_DATE = "2026-07-17"
ORPHAN_MARKER = "[ORPHAN_CLOSING]"


def log(line: str):
    """Escreve no log e na tela."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = f"[{ts}] {line}"
    print(msg)
    with open(LOG_PATH, "a") as f:
        f.write(msg + "\n")


def backup_db() -> Path:
    """Copia o DB para .bak.pre_repair_20260717_HHMMSS ao lado do original."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = DB_PATH.with_suffix(f".db.bak.pre_repair_20260717_{ts}")
    shutil.copy2(str(DB_PATH), str(backup))
    return backup


def fetch_truth_from_cache():
    """Lê o cache gerado pelo agente repair-broker-truth."""
    if not TRUTH_CACHE.exists():
        raise FileNotFoundError(
            f"Cache de broker-truth não encontrado em {TRUTH_CACHE}. "
            "Rode primeiro a investigação MT5 (history position= por ticket)."
        )
    with open(TRUTH_CACHE, "rb") as f:
        return pickle.load(f)


def build_repair_plan(truth: dict) -> dict:
    """Constrói o plano de repair a partir do broker-truth.

    Retorna dict com:
      - new_rows: lista de dicts com colunas para INSERT (1 por deal com profit≠0)
      - orphan_ids: lista de trade.id do DB que devem virar ORPHAN_CLOSING
      - total_mt5_pnl, total_db_pnl, diverg
    """
    today_deals_by_pos = truth["today_deals_by_pos"]
    db_rows = truth["db_rows"]

    # Mapa entry_ticket → row do DB (pegar a primeira; podem ter várias com mesmo entry_ticket)
    db_by_entry = {}
    for r in db_rows:
        db_by_entry.setdefault(r["entry_ticket"], []).append(r)

    # 1) Constrói linhas reconstruídas: 1 linha por "close deal" (profit≠0)
    #    Se uma position tem múltiplos close-deals (cycles), gera 1 linha por ciclo
    #    Para simplificar: 1 linha POR POSITION com profit total, time=último exit
    #    Mas cada deal fechado tem seu próprio entry_ticket (MT5 deal ticket != position_id).
    #    Pra preservar IR: usa o deal_ticket como entry_ticket do trade reconstruído.
    new_rows = []
    for pos, deals in sorted(today_deals_by_pos.items()):
        # Pega o multiplier correto a partir do symbol
        symbols = set(d["symbol"] for d in deals)
        if len(symbols) > 1:
            log(f"  WARN position {pos} tem múltiplos symbols: {symbols}")
        symbol = next(iter(symbols))
        mult_map = {"WIN": 0.20, "WDO": 10.00, "DOL": 1.00, "IND": 1.0, "BIT": 1.0, "WSP": 0.01}
        multiplier = 1.0
        for root, m in mult_map.items():
            if root in symbol:
                multiplier = m
                break

        # Pra cada deal com profit≠0 (i.e. um close), cria uma linha
        # OU: agrupa por "ciclo" entry-exit?
        # Wave 14.5.1 BITN26 bug: broker-report vs multiplier do config.
        # Como o DB está 100x errado para BITN26 SELL (gravou 220 vs MT5 +2.20),
        # a fonte da verdade é o PROFIT DIRETO do MT5, não recálculo.
        for d in deals:
            if d["profit"] == 0:
                continue  # entry deal (não fechou nada) — não vira linha de trade
            direction = "SELL" if d["type"] == "BUY" else "BUY"  # close opposite
            # Espera — type é o TIPO DA ORDEM: BUY=compra, SELL=venda.
            # Pra fechar SELL precisa BUY, pra fechar BUY precisa SELL.
            # Mas o "profit" é realizado no close. Logo:
            # se open=SELL, close=BUY; se open=BUY, close=SELL.
            # Como temos múltiplos cycles, não dá pra saber open a partir do close sozinho.
            # Aproximação: olha o deal anterior com mesmo position_id
            # ... mas pra reconstrução IR basta registrar como close no position.
            # Solução pragmática: direction = mesma do entry deal daquela position
            entry_deals = [x for x in deals if x["profit"] == 0]
            if entry_deals:
                direction = entry_deals[0]["type"]
            else:
                direction = "BUY"  # fallback

            entry_price = entry_deals[0]["price"] if entry_deals else d["price"]
            entry_time_dt = entry_deals[0]["_dt_utc"] if entry_deals else d["_dt_utc"]
            exit_time_dt = d["_dt_utc"]

            # CONVENÇÃO: o vt_trade_log grava datetime.now() que é o horário
            # local do servidor (Linux em UTC). O DB existente para hoje
            # mostra entry_time=09:21:20 quando o MT5 deal epoch=UTC 09:21:xx.
            # Ou seja: o DB armazena UTC disfarçado de "local". Pra preservar
            # consistência com o resto do DB, salvamos o epoch UTC como string
            # "YYYY-MM-DD HH:MM:SS" (tz-naive). Não convertemos pra BRT.
            entry_time_str = entry_time_dt.strftime("%Y-%m-%d %H:%M:%S")
            exit_time_str = exit_time_dt.strftime("%Y-%m-%d %H:%M:%S")

            # Recalcula gross_pnl a partir do profit do MT5
            # MT5 profit já é líquido (com fees e swap inclusos); pra reconstruir:
            # gross_pnl = profit (sem fees, sem swap) — mas MT5 não separa fees_swap no report
            # Conservador: gross_pnl = profit, fees = 0, swap = 0 (replicar valor exato do broker)
            gross_pnl = float(d["profit"])
            fees = 0.0
            swap = float(d.get("swap", 0) or 0)
            net_pnl = gross_pnl + swap - fees

            new_rows.append({
                "entry_ticket": str(d["position_id"]),
                "exit_ticket": str(d["ticket"]),
                "symbol": symbol,
                "direction": direction,
                "volume": float(d["volume"]),
                "entry_time": entry_time_str,
                "entry_price": float(entry_price),
                "exit_time": exit_time_str,
                "exit_price": float(d["price"]),
                "exit_reason": "BROKER_TRUTH_REPAIR",
                "close_source": "MT5_BROKER_TRUTH_REPAIR_20260717",
                "gross_pnl": round(gross_pnl, 2),
                "fees": round(fees, 2),
                "swap": round(swap, 2),
                "net_pnl": round(net_pnl, 2),
                "multiplier": multiplier,
                "magic_number": int(d.get("magic", 555501)),
                "raw_exit_json": json.dumps(d, default=str),
                "notes": f"[broker-truth repair 2026-07-17] position_id={d['position_id']} deal_ticket={d['ticket']}",
            })

    # 2) Identifica orphan_ids: linhas do DB cujo entry_ticket NÃO está no MT5
    #    OU cujo entry_ticket está mas net_pnl diverge do total MT5
    all_mt5_positions = set(today_deals_by_pos.keys())
    mt5_pnl_by_pos = {p: sum(d["profit"] for d in deals) for p, deals in today_deals_by_pos.items()}
    orphan_ids = []
    for r in db_rows:
        et = r["entry_ticket"]
        if et not in all_mt5_positions:
            orphan_ids.append(r["id"])
        else:
            # Verifica divergência
            mt5_pnl = mt5_pnl_by_pos[et]
            db_pnl = r["net_pnl"]
            # Se DB net_pnl = 0 e MT5 != 0 → divergente (ghost no DB)
            # Se DB net_pnl != MT5 → divergente
            if abs(db_pnl - mt5_pnl) > 0.01:
                orphan_ids.append(r["id"])

    total_mt5_pnl = sum(mt5_pnl_by_pos.values())
    total_db_pnl = sum(r["net_pnl"] for r in db_rows)
    return {
        "new_rows": new_rows,
        "orphan_ids": sorted(set(orphan_ids)),
        "total_mt5_pnl": total_mt5_pnl,
        "total_db_pnl": total_db_pnl,
        "diverg": total_db_pnl - total_mt5_pnl,
        "n_db_rows": len(db_rows),
        "n_mt5_positions": len(today_deals_by_pos),
        "mt5_pnl_by_pos": mt5_pnl_by_pos,
        "today_deals_by_pos": today_deals_by_pos,
    }


def print_plan_and_sql(plan: dict, truth: dict, db_path: Path, dry_run: bool):
    """Imprime o plano e o SQL que SERIA executado."""
    log("")
    log("=" * 72)
    log(f"PLANO DE REPAIR — {TRUTH_DATE}")
    log("=" * 72)
    log(f"DB rows hoje           : {plan['n_db_rows']}")
    log(f"MT5 positions hoje     : {plan['n_mt5_positions']}")
    log(f"DB total net_pnl       : R$ {plan['total_db_pnl']:+.2f}")
    log(f"MT5 total net_pnl      : R$ {plan['total_mt5_pnl']:+.2f}")
    log(f"DIVERG                 : R$ {plan['diverg']:+.2f}")
    log(f"Linhas reconstruídas   : {len(plan['new_rows'])}")
    log(f"Linhas órfãs (marcadas): {len(plan['orphan_ids'])}")
    log("")
    log("TABELA CRUZADA (linha-a-linha, ticket | symbol | side | PnL_DB vs PnL_MT5 | match?):")
    log(f"{'ticket':<14} {'symbol':<10} {'side':<5} {'PnL_BD':>10} {'PnL_MT5':>10} {'match?':<8} {'Ação'}")
    log("-" * 75)
    # Pra cada db_row: encontrar pnl correspondente no MT5 (entry_ticket = position_id)
    db_rows = truth["db_rows"]
    matched_db_ids = set()
    for r in db_rows:
        et = r["entry_ticket"]
        db_pnl = r["net_pnl"]
        # MT5 pnl: se a position existe, soma; senão 0
        if et in plan["mt5_pnl_by_pos"]:
            mt5_total = plan["mt5_pnl_by_pos"][et]
            if et not in matched_db_ids:
                # Primeira linha: mostra pnl total do MT5
                mt5_for_line = mt5_total
                matched_db_ids.add(et)
            else:
                mt5_for_line = 0.0  # outras linhas: pnl já atribuído
            match = abs(db_pnl - mt5_for_line) < 0.5 or (db_pnl == 0 and mt5_for_line == 0)
            match_str = "SIM" if match else "NÃO"
            action = "OK" if match else "ÓRFÃ"
        else:
            mt5_for_line = 0.0
            match_str = "NÃO"
            action = "ORPHAN"
        log(f"{et:<14} {r['symbol']:<10} {r['direction']:<5} "
            f"{db_pnl:+10.2f} {mt5_for_line:+10.2f} {match_str:<8} {action}")

    log("")
    log("TABELA RESUMO POR POSITION:")
    log(f"{'Position':<14} {'MT5_PnL':<10} {'#deals_MT5':<12} {'#rows_DB':<10} {'DB_PnL':<10} {'Ação'}")
    log("-" * 70)
    for pos in sorted(set(plan["mt5_pnl_by_pos"].keys())):
        mt5_pnl = plan["mt5_pnl_by_pos"][pos]
        n_db_for_pos = sum(1 for r in db_rows if r["entry_ticket"] == pos)
        db_pnl_for_pos = sum(r["net_pnl"] for r in db_rows if r["entry_ticket"] == pos)
        action = "RECONSTRUIR"
        log(f"{pos:<14} {mt5_pnl:+8.2f}  {len(plan['today_deals_by_pos'][pos]):<12} "
            f"{n_db_for_pos:<10} {db_pnl_for_pos:+8.2f}  {action}")

    log("")
    log("LINHAS ÓRFÃS QUE SERÃO MARCADAS [ORPHAN_CLOSING]:")
    for oid in plan["orphan_ids"]:
        log(f"  trade.id={oid}")

    log("")
    log("SQL QUE SERIA EXECUTADO (dry-run — nada será escrito):")
    log("-" * 72)

    # 1) Backup (imprime o comando)
    log("-- (1) BACKUP — copiar DB para .bak.pre_repair_20260717_HHMMSS")
    log(f"--     shutil.copy2({DB_PATH!r}, <backup_path>)")
    log("")

    # 2) UPDATE órfãs
    log("-- (2) UPDATE linhas órfãs (strategy + [ORPHAN_CLOSING], notes += ' | ORPHAN_CLOSING')")
    for oid in plan["orphan_ids"]:
        log(
            f"UPDATE trades SET strategy = strategy || ' {ORPHAN_MARKER}', "
            f"notes = COALESCE(notes,'') || ' | ORPHAN_CLOSING', "
            f"updated_at = datetime('now','localtime') WHERE id = {oid};"
        )
    log("")

    # 3) INSERT linhas reconstruídas
    log("-- (3) INSERT linhas reconstruídas a partir do broker-truth")
    for r in plan["new_rows"]:
        cols = ["entry_ticket", "exit_ticket", "magic_number", "symbol",
                "direction", "volume", "timeframe", "entry_time", "entry_price",
                "exit_time", "exit_price", "exit_reason", "close_source",
                "gross_pnl", "fees", "swap", "net_pnl", "multiplier",
                "is_day_trade", "asset_type", "strategy", "signal_detail",
                "raw_exit_json", "notes"]
        vals = [r.get("entry_ticket"), r.get("exit_ticket"),
                r.get("magic_number", 555501), r.get("symbol"),
                r.get("direction"), r.get("volume"), "M5",
                r.get("entry_time"), r.get("entry_price"),
                r.get("exit_time"), r.get("exit_price"),
                r.get("exit_reason"), r.get("close_source"),
                r.get("gross_pnl"), r.get("fees", 0), r.get("swap", 0),
                r.get("net_pnl"), r.get("multiplier"),
                1, "FUTURE", "VWAP", None,
                r.get("raw_exit_json"), r.get("notes")]
        placeholders = ",".join(["?"] * len(cols))
        col_list = ",".join(cols)
        log(
            f"INSERT INTO trades ({col_list}) VALUES ({placeholders});"
        )
        # Loga os valores em comentário
        log(f"  -- vals: {vals}")
    log("")

    # 4) Rebuild daily_summary para 2026-07-17
    log("-- (4) RECONSTRUIR daily_summary para 2026-07-17")
    log(
        "DELETE FROM daily_summary WHERE date = '2026-07-17';"
    )
    log(
        "INSERT INTO daily_summary (date, symbol, n_trades, n_winners, n_losers, "
        "gross_pnl, fees, net_pnl, max_win, max_loss) "
        "SELECT date(exit_time), symbol, COUNT(*), "
        "       SUM(CASE WHEN net_pnl>0 THEN 1 ELSE 0 END), "
        "       SUM(CASE WHEN net_pnl<=0 THEN 1 ELSE 0 END), "
        "       SUM(gross_pnl), SUM(fees), SUM(net_pnl), "
        "       MAX(net_pnl), MIN(net_pnl) "
        "FROM trades "
        "WHERE date(entry_time) = '2026-07-17' "
        "  AND strategy NOT LIKE '%[ORPHAN_CLOSING]%' "
        "  AND strategy NOT LIKE '%[EXCLUDED]%' "
        "GROUP BY symbol;"
    )
    log("")

    log("=" * 72)
    log(f"MODO: {'DRY-RUN (nada foi escrito)' if dry_run else 'EXECUTE (vai escrever!)'}")
    log("=" * 72)


def execute_plan(plan: dict):
    """EXECUTA o plano (cuidado — altera o DB)."""
    log("[EXECUTE] Criando backup...")
    backup = backup_db()
    log(f"[EXECUTE] Backup: {backup}")

    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        # 1) Marcar órfãs
        for oid in plan["orphan_ids"]:
            conn.execute(
                f"UPDATE trades SET strategy = COALESCE(strategy,'') || ' {ORPHAN_MARKER}', "
                f"notes = COALESCE(notes,'') || ' | ORPHAN_CLOSING', "
                f"updated_at = datetime('now','localtime') WHERE id = ?",
                (oid,),
            )
        log(f"[EXECUTE] {len(plan['orphan_ids'])} linhas marcadas ORPHAN_CLOSING")

        # 2) Inserir linhas reconstruídas
        inserted = 0
        for r in plan["new_rows"]:
            conn.execute(
                """INSERT INTO trades (entry_ticket, exit_ticket, magic_number,
                    symbol, direction, volume, timeframe, entry_time, entry_price,
                    exit_time, exit_price, exit_reason, close_source,
                    gross_pnl, fees, swap, net_pnl, multiplier,
                    is_day_trade, asset_type, strategy, raw_exit_json, notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    r["entry_ticket"], r["exit_ticket"], r.get("magic_number", 555501),
                    r["symbol"], r["direction"], r["volume"], "M5",
                    r["entry_time"], r["entry_price"],
                    r["exit_time"], r["exit_price"],
                    r["exit_reason"], r["close_source"],
                    r["gross_pnl"], r.get("fees", 0), r.get("swap", 0),
                    r["net_pnl"], r["multiplier"],
                    1, "FUTURE", "VWAP_BROKER_TRUTH",
                    r.get("raw_exit_json"), r.get("notes"),
                ),
            )
            inserted += 1
        log(f"[EXECUTE] {inserted} linhas reconstruídas inseridas")

        # 3) Rebuild daily_summary para 2026-07-17 (apenas linhas não-órfãs)
        conn.execute("DELETE FROM daily_summary WHERE date = ?", (TRUTH_DATE,))
        conn.execute(
            """INSERT INTO daily_summary (date, symbol, n_trades, n_winners, n_losers,
                gross_pnl, fees, net_pnl, max_win, max_loss)
            SELECT date(entry_time), symbol, COUNT(*),
                   SUM(CASE WHEN net_pnl>0 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN net_pnl<=0 THEN 1 ELSE 0 END),
                   SUM(gross_pnl), SUM(fees), SUM(net_pnl),
                   MAX(net_pnl), MIN(net_pnl)
            FROM trades
            WHERE date(entry_time) = ?
              AND strategy NOT LIKE ?
              AND strategy NOT LIKE ?
            GROUP BY symbol""",
            (TRUTH_DATE, "%[ORPHAN_CLOSING]%", "%[EXCLUDED]%"),
        )
        log("[EXECUTE] daily_summary reconstruído para 2026-07-17")

        conn.commit()
        log("[EXECUTE] Commit OK")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Repair broker-truth para vt_trades.db do dia 2026-07-17"
    )
    parser.add_argument("--execute", action="store_true",
                        help="EXECUTA o repair (default: dry-run)")
    parser.add_argument("--cache", default=str(TRUTH_CACHE),
                        help="Caminho do cache pickle com broker-truth")
    args = parser.parse_args()

    dry_run = not args.execute
    log(f"START dry_run={dry_run} cache={args.cache}")

    truth = fetch_truth_from_cache()
    plan = build_repair_plan(truth)

    print_plan_and_sql(plan, truth, DB_PATH, dry_run)

    if not dry_run:
        log("[CONFIRM] --execute detectado. Escrevendo no DB...")
        execute_plan(plan)
        log("[DONE]")
    else:
        log("[DRY-RUN] Para executar de verdade:")
        log(f"  python3 scripts/vt_repair_trades_20260717.py --execute")
    log(f"END log={LOG_PATH}")


if __name__ == "__main__":
    main()