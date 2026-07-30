#!/usr/bin/env python3
"""W14.8 (2026-07-20, seg, Bruno) — Probe de diagnóstico do history() do MT5.

READ-ONLY. Não envia ordens, não modifica posições/SL, não escreve em config/DB
(só lê vt_trades.db para pegar tickets reais). Seguro para rodar com daemon no ar.

Objetivo: descobrir QUAL forma de mt5_orchestrator.history() retorna deals
hoje. O import_mt5_history() usa history() sem args; _truth.get_position_history
usa history(symbol=…, days=1); o reconcile ghost usa history(position=ticket).
Hoje (20/07) TODAS retornaram vazio (0/20 GHOSTs recuperaram PnL). Este probe
testa as 5 variantes contra o MT5 ao vivo e reporta qual funciona (se alguma).

Como o MT5 é lento/pesado, faz 5 chamadas sequenciais com timeout de 60s cada.
Resultado estruturado em data/mt5_history_probe_<ts>.json para comparação futura.

Uso:
    python3 scripts/w14_8_probe_mt5_history.py
"""
import json
import sqlite3
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

_PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT))

# Importa só a função history (isolada). NÃO importa core.vt_autotrader
# (que construiria estado global e contato MT5 no top-level).
from mt5.mt5_orchestrator import history  # noqa: E402

DB_PATH = _PROJECT / "vt_trades.db"
OUT_DIR = _PROJECT / "data"


def _pick_tickets():
    """Pega 2 tickets reais do DB: um fechado e um aberto, ambos de hoje."""
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # Fechado hoje, via SL servidor (deal de saída deve existir no MT5 history).
    closed = cur.execute(
        "SELECT entry_ticket FROM trades "
        "WHERE substr(entry_time,1,10)='2026-07-20' "
        "AND close_source='MT5_SERVER_SL' "
        "ORDER BY entry_time DESC LIMIT 1"
    ).fetchone()

    # Aberto agora (posição viva no MT5).
    opened = cur.execute(
        "SELECT entry_ticket FROM trades "
        "WHERE (exit_time IS NULL OR exit_time='') AND entry_ticket IS NOT NULL "
        "ORDER BY entry_time DESC LIMIT 1"
    ).fetchone()

    con.close()
    return (
        closed["entry_ticket"] if closed else None,
        opened["entry_ticket"] if opened else None,
    )


def _run_probe(name, fn):
    """Roda um teste; retorna dict com resultado/latency/erro."""
    result = {"name": name, "ok": False, "count": None, "info": None,
              "deals_sample": [], "latency_s": None, "error": None}
    t0 = time.monotonic()
    try:
        raw = fn()
        result["latency_s"] = round(time.monotonic() - t0, 2)
        if isinstance(raw, dict):
            hist = raw.get("history") or raw.get("deals") or []
            result["ok"] = True
            result["count"] = len(hist)
            result["info"] = raw.get("info")
            result["deals_sample"] = [
                {
                    "ticket": d.get("ticket"),
                    "symbol": d.get("symbol"),
                    "type": d.get("type"),
                    "profit": d.get("profit"),
                    "price": d.get("price"),
                    "position_id": d.get("position_id"),
                    "time": d.get("time"),
                }
                for d in hist[:3]
            ]
        else:
            result["error"] = f"resposta inesperada (type={type(raw).__name__}): {raw!r}"
    except Exception as e:
        result["latency_s"] = round(time.monotonic() - t0, 2)
        result["error"] = f"{type(e).__name__}: {e}"
        result["traceback"] = traceback.format_exc()
    return result


def main():
    print(f"=== W14.8 MT5 history probe — {datetime.now().isoformat()} ===\n")

    closed_ticket, open_ticket = _pick_tickets()
    print("Tickets do DB:")
    print(f"  fechado hoje (SL servidor): {closed_ticket}")
    print(f"  aberto agora:               {open_ticket}")
    if not closed_ticket and not open_ticket:
        print("\n⚠️  Nenhum ticket encontrado no DB. Abortando.")
        sys.exit(2)
    print()

    # Símbolo alvo: WINQ26 se disponível (hoje é onde há atividade).
    symbol = "WINQ26"

    probes = []
    probes.append(("1. history() sem args (caminho do import_mt5_history)",
                   lambda: history()))
    probes.append((f"2. history(symbol={symbol}, days=1) (caminho de _truth)",
                   lambda: history(symbol=symbol, days=1)))
    probes.append((f"3. history(symbol={symbol}, days=7) (janela maior)",
                   lambda: history(symbol=symbol, days=7)))
    if closed_ticket:
        probes.append((f"4. history(position={closed_ticket}) — ticket FECHADO",
                       lambda: history(position=str(closed_ticket))))
    if open_ticket:
        probes.append((f"5. history(position={open_ticket}) — ticket ABERTO",
                       lambda: history(position=str(open_ticket))))

    results = []
    for name, fn in probes:
        print(f"▶ {name}")
        r = _run_probe(name, fn)
        results.append(r)
        status = "OK" if r["ok"] else "ERRO"
        cnt = r["count"] if r["count"] is not None else "-"
        print(f"   → {status} | count={cnt} | latency={r['latency_s']}s | info={r['info']}")
        if r["error"]:
            print(f"   error: {r['error']}")
        if r["deals_sample"]:
            for d in r["deals_sample"]:
                print(f"   deal: ticket={d['ticket']} sym={d['symbol']} "
                      f"type={d['type']} profit={d['profit']} pos_id={d['position_id']}")
        print()

    # Persiste resultado estruturado para comparação futura.
    out_path = OUT_DIR / f"mt5_history_probe_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    OUT_DIR.mkdir(exist_ok=True)
    payload = {
        "timestamp": datetime.now().isoformat(),
        "closed_ticket": closed_ticket,
        "open_ticket": open_ticket,
        "symbol": symbol,
        "results": results,
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"=== Resultado salvo em {out_path} ===")

    # Sumário executivo.
    print("\n=== SUMÁRIO ===")
    for r in results:
        verdict = "✓ TEM DEALS" if (r["ok"] and r["count"] and r["count"] > 0) else \
                  ("✗ vazio" if r["ok"] else "✗ ERRO")
        print(f"  {verdict:<12} {r['name']}")


if __name__ == "__main__":
    main()
