"""
stage1_collect.py — Seleção de pares por SIMULAÇÃO + contexto de regime.

REGRA DE OURO (Bruno 16/07): a otimização NUNCA é decidida em cima de trades
passados. O DB (vt_trades.db) é lido APENAS para o relatório Telegram
(ctx["performance"] = PnL realizado real de 7d, contexto display-only). A
decisão de quais pares otimizar vem de _identify_failing_simulated, que
simula cada par bar-a-bar via evaluate_baseline sobre as últimas ~30d de
barras reais do MT5.

Lei 4 (MT5/broker-truth) ainda vale para o PnL de display do relatório.

Saída no ctx:
  ctx["performance"] = {              # display-only (DB 7d)
      "by_symbol":     {root: {n_trades, win_rate, total_pnl, ...}},
      "by_symbol_tf":  {"WIN_M5": {n_trades, win_rate, total_pnl, strategy}},
      "exit_reasons":  {"SL": {count, pnl}, "TRAILING": {...}},
      "streaks":       {root: [{losses, pnl}]},
  }
  ctx["regime"] = {root: {label, atr, adx}}   # se classifier disponível
  ctx["failing_pairs"] = ["WIN_M5", ...]      # POR SIMULAÇÃO (não DB)

Reusa a lógica comprovada do agi_tuning_17h.collect_performance (linhas
187-355), refatorada como módulo isolado com tratamento de erro defensivo.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("agi_v4.stage1")

# DB path — mesmo do agi_tuning_17h.py:34 (via vt_trade_log.DB_PATH indireto).
# Hardcode do caminho é aceitável aqui (não é param de trading); mas tentamos
# ler do config primeiro (Lei 1).
_DB_PATH_DEFAULT = Path("/home/bruno/Projects/Vibe-Trading/vt_trades.db")


def _resolve_db_path(config: dict | None) -> Path:
    """Resolve o caminho do DB. Lei 1: prefere config; fallback é caminho fixo."""
    if config:
        # vt_trade_log lê de config["db_path"] se definido
        db_from_config = config.get("db_path")
        if db_from_config:
            p = Path(db_from_config)
            if p.exists():
                return p
    return _DB_PATH_DEFAULT


def run(ctx: dict) -> dict:
    """Executa o stage 1: coleta performance + regime do DB.

    Args:
        ctx: contexto do pipeline (ver pipeline.py).

    Returns:
        dict com "performance", "regime", "failing_pairs", "summary".
    """
    config = ctx.get("config", {}) or {}
    days = ctx.get("days", 7)
    db_path = _resolve_db_path(config)

    if not db_path.exists():
        log.warning(f"DB não encontrado: {db_path} — stage 1 sem dados")
        return {"performance": {}, "regime": {}, "failing_pairs": [],
                "summary": "DB ausente"}

    # performance: lida do DB APENAS para o relatório Telegram (contexto de
    # PnL realizado real de 7d). NÃO dirige a decisão de otimização — ver
    # _identify_failing_simulated abaixo. (Wave "sem-trades")
    performance = _collect_performance(db_path, days)
    regime = _classify_regimes(config, performance)

    # Selecionar pares perdedores por SIMULAÇÃO bar-a-bar (evaluate_baseline),
    # nunca por trades passados. Mesmo critério de _check_convergence_simulated
    # (pipeline.py:396): par é failing se PnL simulado <= 0.
    failing_pairs = _identify_failing_simulated(config)

    summary = (f"{len(performance.get('by_symbol', {}))} símbolos analisados, "
               f"{len(failing_pairs)} par(es) não-lucrativos em sim 30d, "
               f"{performance.get('by_symbol', {}).get('WIN', {}).get('n_trades', 0)} trades WIN (display)")

    return {
        "performance": performance,
        "regime": regime,
        "failing_pairs": failing_pairs,
        "summary": summary,
    }


# ═══════════════════════════════════════════════════════════════════
# Coleta de performance — refatorado de agi_tuning_17h.collect_performance
# ═══════════════════════════════════════════════════════════════════

def _collect_performance(db_path: Path, days: int) -> dict:
    """Lê SQLite e agrega performance por símbolo, TF, estratégia, etc.

    Reusa queries comprovadas do agi_tuning_17h.py:199-355.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    perf: dict = {}

    # ── Por símbolo ──
    by_symbol = {}
    for r in conn.execute("""
        SELECT substr(symbol,1,3) as root,
               count(*) as n,
               sum(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as wins,
               sum(CASE WHEN net_pnl <= 0 THEN 1 ELSE 0 END) as losses,
               round(sum(net_pnl),2) as total_pnl,
               round(avg(net_pnl),2) as avg_pnl,
               round(min(net_pnl),2) as worst,
               round(max(net_pnl),2) as best,
               round(sum(fees),2) as total_fees
        FROM trades
        WHERE entry_time >= ? AND exit_time IS NOT NULL
        GROUP BY root ORDER BY total_pnl
    """, (cutoff,)).fetchall():
        wr = round(r["wins"] / r["n"] * 100, 1) if r["n"] else 0
        by_symbol[r["root"]] = {
            "n_trades": r["n"], "wins": r["wins"], "losses": r["losses"],
            "win_rate": wr, "total_pnl": r["total_pnl"], "avg_pnl": r["avg_pnl"],
            "worst": r["worst"], "best": r["best"], "total_fees": r["total_fees"],
            # Alias para gate de convergência (pipeline._check_convergence)
            "pnl": r["total_pnl"],
        }
    perf["by_symbol"] = by_symbol

    # ── Por símbolo + TF (chave para identificar pares perdedores) ──
    by_symbol_tf = {}
    for r in conn.execute("""
        SELECT substr(symbol,1,3) as root, timeframe, strategy,
               count(*) as n,
               sum(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as wins,
               round(sum(net_pnl),2) as total_pnl,
               round(avg(net_pnl),2) as avg_pnl
        FROM trades
        WHERE entry_time >= ? AND exit_time IS NOT NULL
        GROUP BY root, timeframe ORDER BY root, timeframe
    """, (cutoff,)).fetchall():
        key = f"{r['root']}_{r['timeframe']}"
        wr = round(r["wins"] / r["n"] * 100, 1) if r["n"] else 0
        by_symbol_tf[key] = {
            "n_trades": r["n"], "win_rate": wr,
            "total_pnl": r["total_pnl"], "avg_pnl": r["avg_pnl"],
            "strategy": r["strategy"],
            # Alias p/ convergência
            "pnl": r["total_pnl"],
        }
    perf["by_symbol_tf"] = by_symbol_tf

    # ── Exit reasons (diagnóstico) ──
    exit_reasons = {}
    for r in conn.execute("""
        SELECT exit_reason,
               count(*) as n,
               round(sum(net_pnl),2) as pnl,
               round(avg(net_pnl),2) as avg_pnl
        FROM trades
        WHERE entry_time >= ? AND exit_time IS NOT NULL
        GROUP BY exit_reason ORDER BY n DESC
    """, (cutoff,)).fetchall():
        exit_reasons[r["exit_reason"]] = {
            "count": r["n"], "total_pnl": r["pnl"], "avg_pnl": r["avg_pnl"],
        }
    perf["exit_reasons"] = exit_reasons

    # ── Streak analysis (sequência de perdas) ──
    perf["streaks"] = _collect_streaks(conn, cutoff, list(by_symbol.keys()))

    conn.close()
    log.info(f"Coletado: {len(by_symbol)} símbolos, {len(by_symbol_tf)} pares TF")
    return perf


def _collect_streaks(conn: sqlite3.Connection, cutoff: str, roots: list) -> dict:
    """Análise de sequência de perdas por símbolo. Reusa lógica 187-301."""
    streaks = {}
    for sym_root in roots:
        losses_seq = 0
        cur_seq_pnl = 0.0
        try:
            for r in conn.execute("""
                SELECT net_pnl FROM trades
                WHERE entry_time >= ? AND exit_time IS NOT NULL
                  AND substr(symbol,1,3) = ?
                ORDER BY entry_time ASC
            """, (cutoff, sym_root)).fetchall():
                pnl = r["net_pnl"] or 0
                if pnl < 0:
                    losses_seq += 1
                    cur_seq_pnl += pnl
                else:
                    if losses_seq >= 3:
                        streaks.setdefault(sym_root, []).append(
                            {"losses": losses_seq, "pnl": round(cur_seq_pnl, 2)})
                    losses_seq = 0
                    cur_seq_pnl = 0.0
            if losses_seq >= 3:
                streaks.setdefault(sym_root, []).append(
                    {"losses": losses_seq, "pnl": round(cur_seq_pnl, 2)})
        except Exception as e:
            log.warning(f"Streak analysis erro para {sym_root} (não crítico): {e}")
    return streaks


# ═══════════════════════════════════════════════════════════════════
# Regime classification — reusa agi_regime_classifier se disponível
# ═══════════════════════════════════════════════════════════════════

def _classify_regimes(config: dict, performance: dict) -> dict:
    """Classifica regime de mercado por símbolo (trend/range/volatile).

    Reusa optimization/agi_regime_classifier se disponível. Fail-safe: se o
    módulo não estiver instalado ou falhar, retorna dict vazio (a AGI ainda
    funciona, só sem contexto de regime).
    """
    try:
        from optimization.agi_regime_classifier import classify_regime
    except ImportError:
        log.debug("agi_regime_classifier não disponível — regime vazio")
        return {}

    regimes = {}
    for root in performance.get("by_symbol", {}):
        try:
            regimes[root] = classify_regime(root)
        except Exception as e:
            log.warning(f"Regime classify falhou p/ {root}: {e}")
            regimes[root] = {"label": "unknown", "error": str(e)}
    return regimes


# ═══════════════════════════════════════════════════════════════════
# Identificação de pares perdedores — alvo dos stages 2-5
# ═══════════════════════════════════════════════════════════════════

def _identify_failing_simulated(config: dict) -> list[str]:
    """Identifica pares SYM_TF não-lucrativos por SIMULAÇÃO bar-a-bar.

    REGRA DE OURO (Bruno 16/07): nunca julgar otimização em cima de trades
    passados. Cada par é simulado (evaluate_baseline) sobre as últimas ~30d de
    barras reais do MT5 (fetch copy_rates_from_pos(0, N) = até agora). Um par
    é 'failing' se o PnL simulado <= 0 — mesmo critério de
    _check_convergence_simulated (pipeline.py:396).

    Lei 2 (Escopo): NUNCA desabilitamos o par — só o marcamos como alvo de
    otimização. Se não achar edge, o stage 4 gera estratégia nova.

    Returns:
        list[str] de "SYM_TF" (contrato preservado: downstream aceita
        list[str] ou list[dict]).
    """
    try:
        from optimization.agi_v4.backtest_evaluator import evaluate_baseline
    except ImportError:
        log.error("backtest_evaluator indisponível — stage 1 sem seleção por simulação")
        return []

    symbols = config.get("symbols", [])
    tfs_by_sym = config.get("timeframes_by_symbol", {})
    global_tfs = config.get("timeframes", [])

    failing: list[tuple[str, float]] = []  # (pair, pnl) para ordenar
    for sym in symbols:
        for tf in tfs_by_sym.get(sym, global_tfs):
            pair = f"{sym}_{tf}"
            try:
                m = evaluate_baseline(sym, tf, config)
                pnl = m.get("total_pnl", 0)
                n_trades = m.get("n_trades", 0)
            except Exception as e:
                log.warning(f"stage1 simulação {pair} falhou ({e}) — marcado failing")
                failing.append((pair, 0.0))
                continue
            # Mesmo critério de _check_convergence_simulated: PnL <= 0 = failing.
            if pnl <= 0:
                failing.append((pair, pnl))
                log.info(f"stage1 {pair}: PnL sim R$ {pnl:.2f} ({n_trades}t) → failing")

    # Ordena pelo PnL simulado (mais negativo primeiro = prioridade maior).
    failing.sort(key=lambda x: x[1])
    result = [p for p, _ in failing]
    log.info(f"Pares não-lucrativos (sim 30d): {result}")
    return result
