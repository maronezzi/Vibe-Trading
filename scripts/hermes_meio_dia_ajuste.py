#!/usr/bin/env python3
"""
hermes_meio_dia_ajuste — Ajuste manual de PARÂMETROS ao meio-dia (Wave 873).

NÃO troca estratégia, NÃO mexe em sl_atr_mult, no máximo 2 params por ativo.
Escopo: somente (root, tf) com WR < 40% no dia e que estão EFETIVAMENTE ativos
(não estão em disabled_symbols / disabled_timeframes). Respeita decisões do AGI
(bit/wsp/ind pausados pelo W871 — não reativa mid-day).

Escreve em `params_by_tf` (não no bloco do símbolo), pois é onde o autotrader
lê a config com prioridade (_get_params_for_tf em core/vt_autotrader.py).

Autor: Hermes Agent (cron 12h00) — operador: bruno
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from core.vt_config_loader import load_config, save_full_config  # noqa: E402

DB_PATH = HERE.parent / "vt_trades.db"
CONTRACT_TRAIL = ("Q26", "M26", "U26", "N26", "V26", "G26", "J26", "K26", "Z26")


def _strip_contract(sym: str) -> str:
    for t in CONTRACT_TRAIL:
        if sym.endswith(t):
            return sym[: -len(t)]
    return sym


def fetch_today_before_noon(db_path: Path) -> dict:
    """Hoje < 12h, agrupado por (root, tf). Inclui só trades com exit_time."""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            """
            SELECT symbol, timeframe, COUNT(*) as n,
                   ROUND(SUM(net_pnl), 2) as pnl,
                   ROUND(AVG(CASE WHEN net_pnl > 0 THEN 1.0 ELSE 0.0 END) * 100, 1) as wr
            FROM trades
            WHERE date(entry_time) = date('now')
              AND strftime('%H', entry_time) < '12'
              AND exit_time IS NOT NULL AND exit_time != ''
            GROUP BY symbol, timeframe
            """
        ).fetchall()
    finally:
        conn.close()
    out = {}
    for sym, tf, n, pnl, wr in rows:
        root = _strip_contract(sym).lower()
        out[(root, tf)] = {"n": n, "pnl": pnl, "wr": wr}
    return out


def fetch_rolling_7d(db_path: Path, min_n: int = 4) -> dict:
    """7d rolling por (root, tf, strategy). n>=min_n, só fechados."""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            """
            SELECT symbol, timeframe, strategy, COUNT(*) as n,
                   ROUND(SUM(net_pnl), 2) as pnl,
                   ROUND(AVG(CASE WHEN net_pnl > 0 THEN 1.0 ELSE 0.0 END) * 100, 1) as wr
            FROM trades
            WHERE date(entry_time) >= date('now', '-7 days')
              AND exit_time IS NOT NULL AND exit_time != ''
            GROUP BY symbol, timeframe, strategy
            HAVING COUNT(*) >= ?
            """,
            (min_n,),
        ).fetchall()
    finally:
        conn.close()
    out = {}
    for sym, tf, strat, n, pnl, wr in rows:
        root = _strip_contract(sym).lower()
        out[(root, tf, strat)] = {"n": n, "pnl": pnl, "wr": wr}
    return out


def is_active(config: dict, root: str, tf: str) -> bool:
    """Verifica se (root, tf) está realmente ativo na config atual."""
    if root.upper() in config.get("disabled_symbols", []):
        return False
    key = f"{root.upper()}_{tf}"
    if key in config.get("disabled_timeframes", []):
        return False
    # Tem que estar em timeframes_by_symbol
    tfs = config.get("timeframes_by_symbol", {}).get(root.upper(), [])
    if tf not in tfs:
        return False
    # Tem que existir em params_by_tf (senão não há o que ajustar)
    if key not in config.get("params_by_tf", {}):
        return False
    return True


def decide_changes(config: dict, today: dict, rolling: dict) -> list:
    """
    Para cada (root, tf) ativo com problema (WR < 40% no dia E pnl < 0 E
    consistente nos últimos 7d quando há dados), decide até 2 params a ajustar.
    Heurística por estratégia ativa:
      - RSI_REVERSION: tighten rsi_oversold (mais BUY seletivo)
      - SMART_EMA M30:  raise adx_threshold (requer trend mais forte)
      - Outros: skip (deixa pro AGI da tarde)
    """
    changes = []
    strat_by_tf = config.get("strategy_by_tf", {})

    for (root, tf), t in sorted(today.items()):
        if t["wr"] >= 40 or t["pnl"] >= 0:
            continue
        if not is_active(config, root, tf):
            continue
        key = f"{root.upper()}_{tf}"
        tf_cfg = config.get("params_by_tf", {}).get(key, {})
        strategy = strat_by_tf.get(key, "?")

        # Confirma 7d consistente quando há dados
        rolling_key = (root, tf, strategy)
        if rolling_key in rolling:
            r = rolling[rolling_key]
            # Se 7d WR>=40% ou pnl>=0, o problema é só de hoje (mão única / barulho)
            if r["wr"] >= 40 or r["pnl"] >= 0:
                continue
            consistent = True
            n_7d = r["n"]
        else:
            # Sem dados 7d, ainda assim permite ajuste conservador se n_hoje >= 4
            consistent = (t["n"] >= 4)
            n_7d = 0

        if not consistent:
            continue

        # Heurística por estratégia
        if strategy == "RSI_REVERSION":
            cur = tf_cfg.get("rsi_oversold")
            if cur is not None and cur > 20:
                new = max(20, cur - 5)  # aperta 5pts, piso 20
                if new != cur:
                    changes.append({
                        "key": key, "root": root.upper(), "tf": tf,
                        "strategy": strategy, "field": "rsi_oversold",
                        "old": cur, "new": new,
                        "n_hoje": t["n"], "pnl_hoje": t["pnl"], "wr_hoje": t["wr"],
                        "n_7d": n_7d, "consistent": consistent,
                    })
        elif strategy == "SMART_EMA":
            cur = tf_cfg.get("adx_threshold")
            if cur is None:
                # fallback: bloco do símbolo
                cur = config.get(root, {}).get("adx_threshold")
            if cur is not None and cur < 35:
                new = min(35, cur + 5)
                if new != cur:
                    changes.append({
                        "key": key, "root": root.upper(), "tf": tf,
                        "strategy": strategy, "field": "adx_threshold",
                        "old": cur, "new": new,
                        "n_hoje": t["n"], "pnl_hoje": t["pnl"], "wr_hoje": t["wr"],
                        "n_7d": n_7d, "consistent": consistent,
                        "scope": ("params_by_tf" if tf_cfg.get("adx_threshold") is not None
                                  else root),
                    })
        # Outras estratégias: skip (deixa pro AGI das 17h10)

    return changes


def apply_changes(config: dict, changes: list) -> dict:
    for ch in changes:
        key = ch["key"]
        if ch["field"] == "adx_threshold" and ch.get("scope") and ch["scope"] != "params_by_tf":
            # escreve no bloco do símbolo (caso adx_threshold só exista lá)
            config.setdefault(ch["scope"], {})[ch["field"]] = ch["new"]
        else:
            config.setdefault("params_by_tf", {}).setdefault(key, {})[ch["field"]] = ch["new"]
    config["_version"] = config.get("_version", 0) + 1
    config["_updated_at"] = datetime.now().isoformat()
    config["_updated_by"] = "hermes_meio_dia_ajuste"
    return config


def format_telegram(changes: list, today: dict, active_symbols: list) -> str:
    lines = ["🛠 *Vibe-Trading — Ajuste meio-dia (Wave 873)*"]
    lines.append("")
    lines.append(f"*Hoje <12h (escopo ativo: {active_symbols}):*")
    if today:
        for (root, tf), v in sorted(today.items()):
            emoji = "✅" if v["wr"] >= 50 else ("⚠️" if v["wr"] >= 30 else "❌")
            lines.append(f"  {emoji} `{root.upper()}_{tf}`: n={v['n']} pnl=R${v['pnl']:+.2f} WR={v['wr']}%")
    else:
        lines.append("  (nenhum trade até 12h)")
    if changes:
        lines.append("")
        lines.append(f"*Mudanças ({len(changes)} — máx 2/ativo, sem sl_atr_mult):*")
        for ch in changes:
            lines.append(
                f"  • `{ch['root']}_{ch['tf']}` ({ch['strategy']}): "
                f"`{ch['field']}` {ch['old']}→{ch['new']}  "
                f"(hoje n={ch['n_hoje']} WR={ch['wr_hoje']}%; "
                f"7d n={ch['n_7d']})"
            )
    else:
        lines.append("")
        lines.append("✅ Nenhuma mudança — WR saudável ou sample insuficiente.")
    lines.append("")
    lines.append("🛡 *Regras respeitadas:* estratégia inalterada, sl_atr_mult intacto, ≤2 params/ativo.")
    return "\n".join(lines)


def main():
    ts = datetime.now().isoformat(timespec="seconds")
    print(f"[meio_dia_ajuste] {ts}  DB={DB_PATH}")

    config = load_config(force=True)
    last_ub = str(config.get("_updated_by", ""))
    last_uat = str(config.get("_updated_at", ""))
    today_iso = datetime.now().date().isoformat()
    if last_ub == "hermes_meio_dia_ajuste" and last_uat.startswith(today_iso):
        print(f"[meio_dia_ajuste] ⏭️  Já rodou hoje (at={last_uat}) — idempotente.")
        return 0

    today = fetch_today_before_noon(DB_PATH)
    rolling = fetch_rolling_7d(DB_PATH, min_n=4)
    active_symbols = [s for s in config.get("symbols", []) if s not in config.get("disabled_symbols", [])]

    print(f"[meio_dia_ajuste] hoje <12h: {len(today)} grupos")
    for k, v in sorted(today.items()):
        print(f"  {k}  n={v['n']}  pnl=R${v['pnl']:+.2f}  wr={v['wr']}%")

    changes = decide_changes(config, today, rolling)
    if not changes:
        print("[meio_dia_ajuste] ✅ Nenhuma mudança necessária.")
        msg = format_telegram(changes, today, active_symbols)
        print("[TELEGRAM-MSG]\n" + msg)
        return 0

    print(f"[meio_dia_ajuste] {len(changes)} mudança(s) proposta(s):")
    for ch in changes:
        print(f"  {ch['root']}_{ch['tf']} ({ch['strategy']}): {ch['field']} {ch['old']}→{ch['new']} "
              f"| hoje n={ch['n_hoje']} pnl={ch['pnl_hoje']} WR={ch['wr_hoje']}% 7d n={ch['n_7d']}")

    new_config = apply_changes(config, changes)
    ok = save_full_config(new_config, updated_by="hermes_meio_dia_ajuste")
    if not ok:
        print("[meio_dia_ajuste] ❌ Falha ao salvar config")
        return 1
    print(f"[meio_dia_ajuste] ✅ Config v{new_config['_version']} salva.")

    msg = format_telegram(changes, today, active_symbols)
    print("[TELEGRAM-MSG]\n" + msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
