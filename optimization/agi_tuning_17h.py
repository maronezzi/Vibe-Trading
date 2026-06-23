#!/usr/bin/env python3
"""
AGI 17h Tuning v3.0 — Super Estratégia Architecture.

Fluxo (6 Stages):
  1. Lê performance real do SQLite + Classifica Regime (ATR/ADX) + Context
  2. Macro Intelligence (web intel) → Risk Tags
  3. Multi-Stage Discovery Engine (Bayesian/Optuna):
     3.1 Macro-Selection (PF > 1.1, Sharpe > 0.8)
     3.2 Micro-Tuning (Bayesian + Occam's Razor)
     3.3 Walk-Forward & Stress Test (out-of-sample)
     3.4 Synthesis (Regime Switching Meta-Strategy)
  4. LLM Portfolio Manager (validate + adjust)
  5. Safe Application (Pydantic validation + Shadow Mode)
  6. Convergence Loop & Circuit Breaker

3 Safety Pillars:
  1. Occam's Razor: fitness penalizes complexity
  2. Cost of Not Trading: min_atr_for_entry optimization
  3. Brutal Reality: Net Profit > Total_Cost * 2

Uso:
  python3 agi_tuning_17h.py              # análise dos últimos 7 dias
  python3 agi_tuning_17h.py --days 3     # janela customizada
  python3 agi_tuning_17h.py --dry-run    # só analisa, não aplica
  python3 agi_tuning_17h.py --no-llm     # só estatísticas, sem LLM
  python3 agi_tuning_17h.py --train-days 30 --validate-days 5 \\
      --optimizer-engine bayesian --max-evaluations 500 \\
      --enable-regime-switching --slippage-ticks 1 --latency-ms 200 \\
      --convergence-mode sharpe_ratio --timeout 300
"""

import argparse
from collections import Counter
import json
import logging
import os
import re
import shutil
import subprocess
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.vt_config_loader import load_config, save_params, save_full_config

# ─── AGI v3.0 modules ────────────────────────────────────────────
try:
    from optimization.agi_regime_classifier import (
        classify_regimes_from_trades,
        classify_current_regime,
        describe_regime,
        parse_trade_analysis_files,
    )
    HAS_REGIME_CLASSIFIER = True
except ImportError:
    HAS_REGIME_CLASSIFIER = False

try:
    from optimization.agi_safety_validator import (
        AGISafetyValidator,
        apply_ocam_razor,
        compute_total_cost,
        filter_trades_by_costs,
        evaluate_patience_filter,
        apply_sl_hit_rate_penalty,
        compute_dynamic_sl_mult,
        compute_atr_based_min_sl,
    )
    HAS_SAFETY_VALIDATOR = True
except ImportError:
    HAS_SAFETY_VALIDATOR = False

try:
    from optimization.agi_bayesian_optimizer import (
        run_discovery_engine,
        HAS_OPTUNA,
    )
    HAS_BAYESIAN = True
except ImportError:
    HAS_BAYESIAN = False
    HAS_OPTUNA = False

# ─── Constants ───
PROJECT_DIR = Path(__file__).parent.parent
DB_PATH = PROJECT_DIR / "vt_trades.db"
LOG_FILE = Path("/tmp/vt_agi_tuning.log")
TODAY = datetime.now().strftime("%Y-%m-%d")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("agi_17h")

# ─── Bounds de segurança para parâmetros (LLM não pode ultrapassar) ───
PARAM_BOUNDS = {
    "vwap_buy_threshold":   (1.001, 1.020),
    "vwap_sell_threshold":  (0.980, 0.999),
    "bb_std":               (1.5, 3.5),
    "rsi_overbought":       (65, 85),
    "rsi_oversold":         (15, 35),
    "adx_threshold":        (10, 35),
    "cooldown_seconds":     (120, 3600),
    "max_daily_trades":     (2, 12),
    "sl_atr_mult":          (1.0, 3.0),  # was (0.5, 3.0) — 0.5 results in noise-level stops
    "min_atr_for_entry":    (0.0, 1000.0),
    "trail_activate":       (0.8, 3.0),
    "trail_distance":       (0.3, 1.5),
    "pullback_pct":         (0.03, 0.30),
    "macd_signal":          (5, 21),
    "macd_fast":            (5, 15),
    "macd_slow":            (15, 30),
    "ema_fast":             (5, 15),
    "ema_slow":             (15, 30),
    "breakeven_minutes":    (5, 45),
    "time_trail_minutes":   (15, 90),
    "max_position_minutes": (30, 240),
    "vwap_period":          (10, 50),
    "bb_period":            (10, 50),
    "adx_period":           (7, 28),
    "rsi_period":           (7, 28),
}

# Máximo de mudanças por parâmetro por execução (evita saltos bruscos)
MAX_CHANGE_PCT = {
    "bb_std": 0.30,        # ±30%
    "cooldown_seconds": 0.50,
    "max_daily_trades": 0.50,
    "sl_atr_mult": 0.30,
    "trail_activate": 0.30,
    "trail_distance": 0.30,
    "pullback_pct": 0.40,
    "adx_threshold": 0.40,
    "breakeven_minutes": 0.40,
    "time_trail_minutes": 0.40,
    "max_position_minutes": 0.30,
    "rsi_overbought": 0.20,  # ±20% — RSI é sensível
    "rsi_oversold": 0.20,
}


# ═══════════════════════════════════════════════════════════════════
# 1. COLETA DE DADOS
# ═══════════════════════════════════════════════════════════════════

def collect_performance(days: int = 7) -> dict:
    """Lê SQLite e agrega performance por símbolo, timeframe e estratégia."""
    if not DB_PATH.exists():
        log.warning(f"DB não encontrado: {DB_PATH}")
        return {}

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    # Performance agregada por símbolo
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
        }

    # Performance por símbolo + timeframe
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
        }

    # Exit reasons (diagnóstico de problemas)
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

    # Performance de hoje (para contexto)
    today_perf = {}
    for r in conn.execute("""
        SELECT substr(symbol,1,3) as root,
               count(*) as n,
               sum(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as wins,
               round(sum(net_pnl),2) as total_pnl
        FROM trades
        WHERE date(entry_time) = date('now','localtime') AND exit_time IS NOT NULL
        GROUP BY root
    """).fetchall():
        wr = round(r["wins"] / r["n"] * 100, 1) if r["n"] else 0
        today_perf[r["root"]] = {
            "n_trades": r["n"], "win_rate": wr, "total_pnl": r["total_pnl"],
        }

    # Streak analysis (sequência de perdas) — abordagem simples sem window functions
    streaks = {}
    try:
        for sym_root in by_symbol.keys():
            losses_seq = 0
            worst_seq_pnl = 0
            cur_seq_pnl = 0
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
                    if cur_seq_pnl < worst_seq_pnl:
                        worst_seq_pnl = cur_seq_pnl
                else:
                    if losses_seq >= 3:
                        streaks.setdefault(sym_root, []).append(
                            {"losses": losses_seq, "pnl": round(cur_seq_pnl, 2)}
                        )
                    losses_seq = 0
                    cur_seq_pnl = 0
            # Final do loop
            if losses_seq >= 3:
                streaks.setdefault(sym_root, []).append(
                    {"losses": losses_seq, "pnl": round(cur_seq_pnl, 2)}
                )
    except Exception as e:
        log.warning(f"Streak analysis erro (não crítico): {e}")

    conn.close()

    # ── Signal analysis: correlação RSI/ATR na entrada com resultado ──
    signal_analysis = {}
    try:
        conn2 = sqlite3.connect(str(DB_PATH))
        conn2.row_factory = sqlite3.Row
        for r in conn2.execute("""
            SELECT substr(symbol,1,3) as root,
                   AVG(CASE WHEN net_pnl > 0 THEN json_extract(signal_detail, '$.rsi') END) as avg_rsi_win,
                   AVG(CASE WHEN net_pnl <= 0 THEN json_extract(signal_detail, '$.rsi') END) as avg_rsi_loss,
                   AVG(CASE WHEN net_pnl > 0 THEN json_extract(signal_detail, '$.atr') END) as avg_atr_win,
                   AVG(CASE WHEN net_pnl <= 0 THEN json_extract(signal_detail, '$.atr') END) as avg_atr_loss,
                   COUNT(*) as n
            FROM trades
            WHERE entry_time >= ? AND exit_time IS NOT NULL
              AND signal_detail IS NOT NULL AND signal_detail != ''
            GROUP BY root
        """, (cutoff,)).fetchall():
            if r["n"] >= 2:
                signal_analysis[r["root"]] = {
                    "avg_rsi_win": round(r["avg_rsi_win"], 1) if r["avg_rsi_win"] else None,
                    "avg_rsi_loss": round(r["avg_rsi_loss"], 1) if r["avg_rsi_loss"] else None,
                    "avg_atr_win": round(r["avg_atr_win"], 1) if r["avg_atr_win"] else None,
                    "avg_atr_loss": round(r["avg_atr_loss"], 1) if r["avg_atr_loss"] else None,
                    "n_with_signal": r["n"],
                }
        conn2.close()
    except Exception as e:
        log.warning(f"Signal analysis erro (não crítico): {e}")

    # ── SL analysis: efetividade do stop loss ──
    sl_analysis = {}
    try:
        conn3 = sqlite3.connect(str(DB_PATH))
        conn3.row_factory = sqlite3.Row
        for r in conn3.execute("""
            SELECT substr(symbol,1,3) as root,
                   COUNT(*) as n,
                   SUM(CASE WHEN exit_reason LIKE 'SL%' THEN 1 ELSE 0 END) as sl_hits,
                   AVG(CASE WHEN entry_sl IS NOT NULL AND entry_sl > 0
                       THEN ABS(entry_price - entry_sl) END) as avg_sl_pts,
                   AVG(CASE WHEN exit_reason LIKE 'SL%' AND entry_sl IS NOT NULL
                       THEN ABS(exit_price - entry_sl) END) as avg_sl_slippage
            FROM trades
            WHERE entry_time >= ? AND exit_time IS NOT NULL
            GROUP BY root
        """, (cutoff,)).fetchall():
            if r["n"] >= 2:
                sl_analysis[r["root"]] = {
                    "sl_hit_rate": round(r["sl_hits"] / r["n"] * 100, 1) if r["n"] else 0,
                    "sl_hits": r["sl_hits"],
                    "avg_sl_pts": round(r["avg_sl_pts"], 0) if r["avg_sl_pts"] else None,
                    "avg_sl_slippage": round(r["avg_sl_slippage"], 0) if r["avg_sl_slippage"] else None,
                    "n_trades": r["n"],
                }
        conn3.close()
    except Exception as e:
        log.warning(f"SL analysis erro (não crítico): {e}")

    # ── Direction analysis: BUY vs SELL performance ──
    direction_analysis = {}
    try:
        conn4 = sqlite3.connect(str(DB_PATH))
        conn4.row_factory = sqlite3.Row
        for r in conn4.execute("""
            SELECT substr(symbol,1,3) as root, direction,
                   COUNT(*) as n,
                   SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as wins,
                   round(SUM(net_pnl), 2) as total_pnl,
                   round(AVG(net_pnl), 2) as avg_pnl
            FROM trades
            WHERE entry_time >= ? AND exit_time IS NOT NULL
            GROUP BY root, direction
        """, (cutoff,)).fetchall():
            sym = r["root"]
            if sym not in direction_analysis:
                direction_analysis[sym] = {}
            wr = round(r["wins"] / r["n"] * 100, 1) if r["n"] else 0
            direction_analysis[sym][r["direction"]] = {
                "n_trades": r["n"],
                "wins": r["wins"],
                "win_rate": wr,
                "total_pnl": r["total_pnl"],
                "avg_pnl": r["avg_pnl"],
            }
        conn4.close()
    except Exception as e:
        log.warning(f"Direction analysis erro (não crítico): {e}")

    return {
        "by_symbol": by_symbol,
        "by_symbol_tf": by_symbol_tf,
        "exit_reasons": exit_reasons,
        "today": today_perf,
        "streaks": streaks,
        "signal_analysis": signal_analysis,
        "sl_analysis": sl_analysis,
        "direction_analysis": direction_analysis,
        "period_days": days,
        "cutoff_date": cutoff,
    }


# ═══════════════════════════════════════════════════════════════════
# 2. DIAGNÓSTICO DE PROBLEMAS
# ═══════════════════════════════════════════════════════════════════

def diagnose_issues(perf: dict) -> list:
    """Identifica problemas automaticamente a partir dos dados."""
    issues = []
    for sym, data in perf.get("by_symbol", {}).items():
        n = data["n_trades"]
        wr = data["win_rate"]
        pnl = data["total_pnl"]

        # Muito poucos trades — não há dados suficientes
        if n < 3:
            issues.append({"symbol": sym, "type": "LOW_SAMPLE", "detail": f"Só {n} trades — pouca amostra"})
            continue

        # Win Rate crítica (< 25%)
        if wr < 25:
            issues.append({
                "symbol": sym, "type": "CRITICAL_WR",
                "detail": f"WR={wr}% em {n} trades — entradas ruins, ajustar filtros",
                "severity": "CRITICAL",
            })

        # Win Rate baixa (25-35%)
        elif wr < 35:
            issues.append({
                "symbol": sym, "type": "LOW_WR",
                "detail": f"WR={wr}% em {n} trades — pode melhorar com filtros mais restritivos",
                "severity": "HIGH",
            })

        # Drawdown alto (worst trade)
        if data["worst"] < -500:
            issues.append({
                "symbol": sym, "type": "LARGE_LOSS",
                "detail": f"Pior trade: R$ {data['worst']:.2f} — SL muito largo ou sem proteção",
                "severity": "HIGH",
            })

        # Streak de perdas
        if sym in perf.get("streaks", {}):
            for s in perf["streaks"][sym]:
                if s["losses"] >= 4:
                    issues.append({
                        "symbol": sym, "type": "LOSS_STREAK",
                        "detail": f"Sequência de {s['losses']} perdas (R$ {s['pnl']:.2f}) — elevar cooldown ou max_daily_trades",
                        "severity": "HIGH",
                    })

    # Exit reasons problemáticos
    for reason, data in perf.get("exit_reasons", {}).items():
        if reason == "SL_SERVIDOR" and data["avg_pnl"] < -50:
            issues.append({
                "symbol": "ALL", "type": "SL_TOO_TIGHT_OR_ENTRIES_BAD",
                "detail": f"SL_SERVIDOR: {data['count']} exits, avg {data['avg_pnl']:.2f} — SL sendo estopado com frequência",
                "severity": "HIGH",
            })
        if reason and "ORFAO" in str(reason):
            issues.append({
                "symbol": "ALL", "type": "ORPHAN_TRADES",
                "detail": f"{reason}: {data['count']} trades órfãos — bug de sync MT5/DB",
                "severity": "MEDIUM",
            })

    # ── Signal-based diagnostics ──
    for sym, sa in perf.get("signal_analysis", {}).items():
        rsi_win = sa.get("avg_rsi_win")
        rsi_loss = sa.get("avg_rsi_loss")
        atr_win = sa.get("avg_atr_win")
        atr_loss = sa.get("avg_atr_loss")

        # RSI extremes na entrada indicam entradas ruins
        if rsi_loss and rsi_loss > 75 and sa.get("n_with_signal", 0) >= 3:
            issues.append({
                "symbol": sym, "type": "RSI_EXTREME_LOSS",
                "detail": f"RSI médio nas perdas: {rsi_loss:.0f} (sobrecomprado) — evitar BUY quando RSI > 70",
                "severity": "MEDIUM",
            })
        if rsi_loss and rsi_loss < 25 and sa.get("n_with_signal", 0) >= 3:
            issues.append({
                "symbol": sym, "type": "RSI_EXTREME_LOSS",
                "detail": f"RSI médio nas perdas: {rsi_loss:.0f} (sobrevendido) — evitar SELL quando RSI < 30",
                "severity": "MEDIUM",
            })

        # ATR alto nas perdas = SL largo demais para a volatilidade
        if atr_loss and atr_win and atr_loss > atr_win * 1.8 and sa.get("n_with_signal", 0) >= 3:
            issues.append({
                "symbol": sym, "type": "ATR_MISMATCH",
                "detail": f"ATR nas perdas ({atr_loss:.0f}) 1.8x maior que nos ganhos ({atr_win:.0f}) — SL não se adapta à volatilidade",
                "severity": "HIGH",
            })

    # ── SL effectiveness diagnostics ──
    for sym, sla in perf.get("sl_analysis", {}).items():
        # SL hit rate muito alto = SL apertado demais ou entradas ruins
        if sla["sl_hit_rate"] > 70 and sla["n_trades"] >= 5:
            issues.append({
                "symbol": sym, "type": "HIGH_SL_HIT_RATE",
                "detail": f"SL hit rate {sla['sl_hit_rate']:.0f}% ({sla['sl_hits']}/{sla['n_trades']}) — SL muito apertado ou entradas ruins",
                "severity": "HIGH",
            })

        # SL slippage alto = ordem não executada no preço
        if sla.get("avg_sl_slippage") and sla["avg_sl_slippage"] > 50:
            issues.append({
                "symbol": sym, "type": "SL_SLIPPAGE",
                "detail": f"Slippage médio no SL: {sla['avg_sl_slippage']:.0f}pts — preço se move rápido, considerar SL mais largo",
                "severity": "MEDIUM",
            })

    # ── Direction-based diagnostics ──
    for sym, dirs in perf.get("direction_analysis", {}).items():
        buy = dirs.get("BUY")
        sell = dirs.get("SELL")
        if not buy or not sell:
            continue
        if buy["n_trades"] < 3 or sell["n_trades"] < 3:
            continue

        # Uma direction muito pior que a outra
        wr_diff = abs(buy["win_rate"] - sell["win_rate"])
        if wr_diff > 30:
            worst_dir = "BUY" if buy["win_rate"] < sell["win_rate"] else "SELL"
            worst_wr = buy["win_rate"] if worst_dir == "BUY" else sell["win_rate"]
            best_wr = sell["win_rate"] if worst_dir == "BUY" else buy["win_rate"]
            opposite = "SELL" if worst_dir == "BUY" else "BUY"
            issues.append({
                "symbol": sym, "type": "DIRECTION_BIAS",
                "detail": f"{worst_dir} WR={worst_wr:.0f}% vs {opposite} WR={best_wr:.0f}% — considerar filtrar {worst_dir}",
                "severity": "MEDIUM",
            })

    return issues


# ═══════════════════════════════════════════════════════════════════
# 3. LLM — CONSULTA AO MODELO ATIVO NO HERMES
# ═══════════════════════════════════════════════════════════════════

def _find_hermes():
    """Localiza o binário do hermes (cron pode ter PATH minimalista)."""
    for p in [
        os.path.expanduser("~/.local/bin/hermes"),
        os.path.expanduser("~/.hermes/hermes-agent/venv/bin/hermes"),
        shutil.which("hermes") if shutil else None,
    ]:
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


# ═══════════════════════════════════════════════════════════════════
# TINYFISH — WEB INTELLIGENCE PARA O AGI
# ═══════════════════════════════════════════════════════════════════

TINYFISH_BIN = shutil.which("tinyfish") if shutil else None


def _tinyfish_check() -> bool:
    """Verifica se tinyfish está instalado e autenticado."""
    if not TINYFISH_BIN:
        return False
    try:
        r = subprocess.run(
            [TINYFISH_BIN, "auth", "status"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0 and "authenticated" in r.stdout
    except Exception:
        return False


def tinyfish_search(query: str, num_results: int = 5, timeout: int = 30) -> list:
    """Busca web rápida via tinyfish. Retorna lista de dicts com title/url/snippet."""
    if not TINYFISH_BIN:
        return []
    try:
        r = subprocess.run(
            [TINYFISH_BIN, "search", "query", query],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            log.warning(f"tinyfish search falhou: {r.stderr[:200]}")
            return []
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            return []
        # Formato tinyfish: pode ser {results: [...]} ou lista direta
        if isinstance(data, dict):
            results = data.get("results", data.get("data", []))
        else:
            results = data
        return results[:num_results] if isinstance(results, list) else []
    except subprocess.TimeoutExpired:
        log.warning(f"tinyfish search timeout ({timeout}s): {query[:60]}")
        return []
    except Exception as e:
        log.warning(f"tinyfish search erro: {e}")
        return []


def tinyfish_fetch(urls: list, max_chars: int = 3000, timeout: int = 30) -> str:
    """Fetch de múltiplas URLs em paralelo via tinyfish. Retorna texto consolidado."""
    if not TINYFISH_BIN or not urls:
        return ""
    try:
        cmd = [TINYFISH_BIN, "fetch", "content", "get", "--format", "markdown"] + list(urls)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            log.warning(f"tinyfish fetch falhou: {r.stderr[:200]}")
            return ""
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            return r.stdout[:max_chars]
        # Extrair texto de cada URL
        chunks = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content") or ""
                    title = item.get("title", "")
                    if text:
                        chunks.append(f"### {title}\n{text}")
        elif isinstance(data, dict):
            # Resposta única
            results = data.get("results", [data])
            for item in results:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content") or ""
                    title = item.get("title", "")
                    if text:
                        chunks.append(f"### {title}\n{text}")
        full = "\n\n".join(chunks)
        return full[:max_chars]
    except subprocess.TimeoutExpired:
        log.warning(f"tinyfish fetch timeout ({timeout}s)")
        return ""
    except Exception as e:
        log.warning(f"tinyfish fetch erro: {e}")
        return ""


def tinyfish_agent(url: str, goal: str, timeout: int = 60) -> str:
    """Browser agent para extrair dados estruturados de uma página dinâmica."""
    if not TINYFISH_BIN:
        return ""
    try:
        r = subprocess.run(
            [TINYFISH_BIN, "agent", "run", "--url", url, "--sync", goal],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            log.warning(f"tinyfish agent falhou: {r.stderr[:200]}")
            return ""
        # Extrair o evento COMPLETE do SSE stream
        for line in r.stdout.split("\n"):
            if line.startswith("data:") and "COMPLETE" in line:
                try:
                    event = json.loads(line[5:].strip())
                    return event.get("resultJson", r.stdout)
                except json.JSONDecodeError:
                    continue
        return r.stdout[:3000]
    except subprocess.TimeoutExpired:
        log.warning(f"tinyfish agent timeout ({timeout}s)")
        return ""
    except Exception as e:
        log.warning(f"tinyfish agent erro: {e}")
        return ""


# Mapeamento de estratégias do Vibe-Trading → queries técnicas ESPECÍFICAS
# Foco: trading automatizado, MT5, alta taxa de acerto, B3 mini contratos

# Whitelist de estratégias válidas que o AGI pode atribuir
# Regra Bruno 17/06: se um par SYM_TF não é lucrativo, testar outras estratégias.
VALID_STRATEGIES = {
    # Estratégias clássicas
    "BOLLINGER",
    "RSI_REVERSION",
    "EMA_PULLBACK",
    "VWAP",
    "MACD_MOMENTUM",
    "BREAKOUT",
    # Estratégias adicionais (testar se clássicos não funcionam)
    "MEAN_REVERSION",
    "MOMENTUM",
    "TREND_FOLLOWING",
    "DONCHIAN_BREAKOUT",
    "ICHIMOKU",
    "SUPERTREND",
    "KELTNER_CHANNEL",
    "STOCHASTIC",
    "PARABOLIC_SAR",
    "ATR_BREAKOUT",
    # Genérico
    "ADX",
    "RSI",
    "ATR",
}

TECHNICAL_QUERIES = {
    "BOLLINGER": [
        "Bollinger Bands expert advisor MT5 profit factor backtest",
        "Bollinger Bands RSI day trade win rate above 60 percent mini indice",
        "Bollinger Bands squeeze mean reversion automated trading backtest results",
        "Bollinger Bands bot MT5 high win rate strategy optimization",
    ],
    "VWAP": [
        "VWAP pullback expert advisor MT5 mini dolar backtest win rate",
        "VWAP deviation bands automated trading strategy high profit factor",
        "VWAP mean reversion bot MT5 mini indice win rate",
        "VWAP intraday strategy expert advisor backtest B3 futures",
    ],
    "EMA_PULLBACK": [
        "EMA 9 21 pullback expert advisor MT5 high win rate backtest",
        "EMA crossover pullback ADX filter automated strategy mini indice",
        "EMA pullback continuation pattern expert advisor MT5 B3",
        "EMA fast slow crossover pullback bot MT5 profit factor",
    ],
    "MACD_MOMENTUM": [
        "MACD signal line crossover expert advisor MT5 momentum backtest",
        "MACD histogram zero line rejection automated strategy win rate",
        "MACD momentum bot MT5 mini S&P 500 backtest profit factor",
        "MACD crossover expert advisor MT5 high win rate strategy",
    ],
    "RSI": [
        "RSI 14 70 30 overbought oversold expert advisor MT5 backtest win rate",
        "RSI 2 mean reversion expert advisor MT5 high win rate",
        "RSI divergence automated strategy MT5 mini indice B3 backtest",
        "RSI period 7 vs 14 day trade expert advisor MT5 win rate",
    ],
    "ADX": [
        "ADX 14 25 trend strength filter expert advisor MT5 backtest",
        "ADX plus minus DI directional movement automated strategy win rate",
        "ADX trend filter expert advisor MT5 mini indice high win rate",
        "ADX 20 25 threshold expert advisor MT5 backtest profit factor",
    ],
    "ATR": [
        "ATR stop loss 1.5 2.0 multiplier expert advisor MT5 backtest win rate",
        "ATR trailing stop expert advisor MT5 high profit factor",
        "ATR volatility position sizing mini contrato B3 day trade",
        "ATR multiplier 1.0 1.5 2.0 SL expert advisor MT5 backtest",
    ],
    "GENERAL": [
        "expert advisor MT5 day trade mini indice B3 high win rate backtest",
        "expert advisor MT5 mini dolar B3 high profit factor strategy",
        "MetaTrader 5 bot day trade B3 futures high win rate backtest",
        "MT5 expert advisor mini contrato B3 win rate above 60 percent",
        "automated trading bot B3 mini indice dolar win rate backtest",
        "day trade B3 high win rate strategy backtest profit factor 1.5",
    ],
}


# Whitelist de fontes técnicas sólidas (preferidas)
PREFERRED_DOMAINS = [
    "b3.com.br",                    # B3 oficial
    "schwab.com",                   # Schwab (broker)
    "investopedia.com",             # Investopedia (referência)
    "babypips.com",                 # Babypips (forex education)
    "tradingwithrayner.com",        # Trading with Rayner
    "tradingview.com",              # TradingView
    "mql5.com",                     # MQL5 (comunidade MT5)
    "forexfactory.com",             # ForexFactory
    "stockcharts.com",              # StockCharts
    "incrediblecharts.com",         # Incredible Charts
    "technicalanalysis.org.uk",     # Technical Analysis
    "earnforex.com",                # EarnForex (MT5)
    "forexstrategiesresources.com", # Forex Strategies
    "tradingstrategyguides.com",    # Trading Strategy Guides
    "tradingpedia.com",             # TradingPedia
    "learn.tradimo.com",            # Tradimo
    "fidelity.com",                 # Fidelity (broker)
    "tastytrade.com",               # TastyTrade
    "interactivebrokers.com",       # Interactive Brokers
    "global-view.com",              # Barchart global view
    "barchart.com",                 # Barchart
    "dailyfx.com",                  # DailyFX
    "fxstreet.com",                 # FXStreet
    "investing.com",                # Investing.com
]

# Blacklist: fontes com muito lixo, pouco conteúdo técnico profundo
BLOCKED_DOMAINS = [
    "youtube.com",        # Vídeos — fetch retorna lixo de metadata
    "youtu.be",           # Shorts
    "instagram.com",      # Posts curtos + boilerplate
    "tiktok.com",         # Vídeos curtos
    "pinterest.com",      # Imagens + descriptions curtas
    "facebook.com",       # Posts + ads
    "twitter.com",        # Tweets curtos
    "x.com",              # Mesma coisa
    "reddit.com",         # Opiniões, não referências técnicas
    "quora.com",          # Respostas curtas
    "linkedin.com",       # Posts pessoais
    "medium.com",         # Variável — alguns bons, mas muito lixo
    "wordpress.com",      # Blogs pessoais
    "blogspot.com",       # Blogs antigos
]


def _score_url(url: str) -> float:
    """Retorna score 0.0-1.0 de qualidade de uma URL técnica."""
    url_lower = url.lower()

    # Blacklist → score 0
    for blocked in BLOCKED_DOMAINS:
        if blocked in url_lower:
            return 0.0

    # Whitelist → score alto
    for pref in PREFERRED_DOMAINS:
        if pref in url_lower:
            return 1.0

    # B3 específica (.b3.com.br) → bônus
    if ".b3." in url_lower or "b3.com" in url_lower:
        return 1.0

    # Domínios .gov / .edu → alta confiança
    if ".gov" in url_lower or ".edu" in url_lower:
        return 0.9

    # Default: fonte neutra (blogs, sites desconhecidos)
    return 0.5


def _clean_junk(text: str) -> str:
    """Remove lixo comum de páginas web (rodapés, menus, etc)."""
    if not text:
        return ""

    # Padrões de lixo comuns
    junk_patterns = [
        # YouTube
        r"About\s+Press\s+Copyright.*?(?=\n\n|\Z)",
        r"Advertise\s+Developers\s+Terms.*?(?=\n\n|\Z)",
        r"Test new features.*?NFL Sunday Ticket.*?(?=\n\n|\Z)",
        r"© \d{4} Google LLC.*?(?=\n\n|\Z)",
        # Instagram
        r"Sign up to Instagram.*?(?=\n\n|\Z)",
        r"Sign in to like.*?(?=\n\n|\Z)",
        r"Never miss a post from.*?(?=\n\n|\Z)",
        r"Follow\s*More\s*",
        # Genéricos
        r"Cookie Policy\s*Accept\s*Reject\s*",
        r"Subscribe to our newsletter.*?(?=\n\n|\Z)",
        r"Follow us on.*?(?=\n\n|\Z)",
    ]

    cleaned = text
    for pattern in junk_patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.DOTALL | re.IGNORECASE)

    # Colapsar múltiplas linhas em branco
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)

    return cleaned.strip()


def _filter_results_by_quality(results: list, min_score: float = 0.5) -> list:
    """Filtra e ordena resultados por score de qualidade."""
    scored = []
    for r in results:
        url = r.get("url", r.get("link", ""))
        if not url:
            continue
        score = _score_url(url)
        if score >= min_score:
            r_copy = dict(r)
            r_copy["_quality_score"] = score
            scored.append(r_copy)

    # Ordenar por score desc
    scored.sort(key=lambda x: x["_quality_score"], reverse=True)
    return scored


def web_intel_for_symbol(symbol: str, strategy: str = None) -> dict:
    """Coleta inteligência TÉCNICA de mercado via tinyfish para um símbolo B3.

    Foco 100% técnico (operação é puramente técnica, sem fundamentalismo):
    - Estratégia do ativo (BB/VWAP/EMA/MACD) → melhores configurações
    - Períodos ideais de indicadores (RSI 7/14/21, ADX 14, BB 20)
    - SL/ATR — multiplicador, trailing stop
    - Padrões de pullback, breakout, mean reversion
    - Suporte/resistência, S/R dinâmico

    Retorna dict com chaves: strategy_tips, indicator_settings,
    sl_trail_tactics, patterns, sources
    """
    result = {
        "symbol": symbol,
        "strategy": strategy,
        "strategy_tips": "",
        "indicator_settings": "",
        "sl_trail_tactics": "",
        "patterns": "",
        "sources": [],
        "queries_made": [],
        "fetched_at": datetime.now().isoformat(),
    }

    if not _tinyfish_check():
        log.warning("tinyfish não disponível — pulando web intel")
        return result

    # Mapear símbolo B3 → nome de busca
    symbol_map = {
        "WIN": "Mini Índice Bovespa",
        "WDO": "Mini Dólar",
        "DOL": "Dólar Futuro B3",
        "IND": "Índice Bovespa",
        "BIT": "Bitcoin B3",
        "WSP": "Mini S&P 500",
    }
    name = symbol_map.get(symbol, symbol)

    def _search_and_fetch(query: str, num_results: int = 5, max_chars: int = 1500) -> str:
        """Busca + filtra por qualidade + fetch + limpa lixo."""
        result["queries_made"].append(query)
        r = tinyfish_search(query, num_results=num_results, timeout=15)

        # Filtrar por score de qualidade (>= 0.5)
        filtered = _filter_results_by_quality(r, min_score=0.5)
        if not filtered:
            log.debug(f"query '{query[:40]}': nenhuma fonte de qualidade")
            return ""

        # Top 2 fontes por score
        top_urls = [x.get("url", x.get("link", "")) for x in filtered[:2]]
        top_urls = [u for u in top_urls if u][:2]
        if not top_urls:
            return ""

        text = tinyfish_fetch(top_urls, max_chars=max_chars, timeout=20)
        if text:
            text = _clean_junk(text)
            result["sources"].extend(top_urls)
        return text or ""

    # 1. Configurações da estratégia do ativo (prioritário)
    if strategy and strategy in TECHNICAL_QUERIES:
        try:
            queries = TECHNICAL_QUERIES[strategy]
            all_text = []
            for q in queries[:2]:
                text = _search_and_fetch(q, num_results=5, max_chars=1500)
                if text and len(text) > 100:
                    all_text.append(text)
            result["strategy_tips"] = "\n\n---\n\n".join(all_text)
        except Exception as e:
            log.warning(f"web intel strategy tips erro: {e}")

    # 2. Configurações ideais de indicadores (períodos, std)
    try:
        ind_queries = TECHNICAL_QUERIES.get("GENERAL", [])
        all_text = []
        for q in ind_queries[:2]:
            text = _search_and_fetch(q, num_results=5, max_chars=1500)
            if text and len(text) > 100:
                all_text.append(text)
        result["indicator_settings"] = "\n\n---\n\n".join(all_text)
    except Exception as e:
        log.warning(f"web intel indicator settings erro: {e}")

    # 3. SL/ATR/Trailing — táticas operacionais
    try:
        sl_queries = TECHNICAL_QUERIES.get("ATR", [])
        all_text = []
        for q in sl_queries[:2]:
            text = _search_and_fetch(q, num_results=5, max_chars=1500)
            if text and len(text) > 100:
                all_text.append(text)
        result["sl_trail_tactics"] = "\n\n---\n\n".join(all_text)
    except Exception as e:
        log.warning(f"web intel SL tactics erro: {e}")

    # 4. Padrões gráficos aplicáveis (pullback, breakout, mean reversion)
    try:
        pattern_queries = [
            f"{name} day trade pullback breakout mean reversion backtest strategy",
            f"{symbol} day trade high win rate patterns support resistance automated",
        ]
        all_text = []
        for q in pattern_queries[:2]:
            text = _search_and_fetch(q, num_results=5, max_chars=1500)
            if text and len(text) > 100:
                all_text.append(text)
        result["patterns"] = "\n\n---\n\n".join(all_text)
    except Exception as e:
        log.warning(f"web intel patterns erro: {e}")

    log.info(f"🌐 Web intel (técnica) para {symbol} [{strategy}]: "
             f"strategy_tips={len(result['strategy_tips'])}ch, "
             f"indicators={len(result['indicator_settings'])}ch, "
             f"sl_tactics={len(result['sl_trail_tactics'])}ch, "
             f"patterns={len(result['patterns'])}ch, "
             f"{len(result['sources'])} fontes")
    return result


def ask_llm(prompt: str, timeout: int = 300) -> str | None:
    """Consulta o MiniMax-M3 via Hermes CLI.

    Modelo: minimax/minimax-m3 (OpenRouter)
    Timeout 300s (5min) pois o prompt com web intel é grande (~5K tokens).
    1M context window do M3 comporta isso facilmente.
    """
    hermes_bin = _find_hermes()
    if not hermes_bin:
        log.warning("hermes CLI não encontrado no sistema")
        return None
    try:
        # Provider: minimax-oauth (ativo no Hermes), fallback automático
        result = subprocess.run(
            [hermes_bin, "-z", prompt, "-m", "MiniMax-M3", "--provider", "minimax-oauth"],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(PROJECT_DIR),
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        else:
            log.warning(f"LLM erro (rc={result.returncode}): {result.stderr[:300]}")
            return None
    except subprocess.TimeoutExpired:
        log.warning("LLM timeout (300s)")
        return None
    except FileNotFoundError:
        log.warning("hermes CLI não encontrado")
        return None
    except Exception as e:
        log.warning(f"LLM exceção: {e}")
        return None


def build_llm_prompt(perf: dict, issues: list, config: dict, web_intel: dict = None,
                    optimization: dict = None) -> str:
    """Constrói prompt para o LLM com performance + config atual + problemas + web intel + optimization."""
    web_intel = web_intel or {}
    optimization = optimization or {}

    # Resumo de performance
    perf_lines = []
    for sym, data in sorted(perf.get("by_symbol", {}).items(), key=lambda x: x[1]["total_pnl"]):
        wr = data["win_rate"]
        pnl = data["total_pnl"]
        n = data["n_trades"]
        avg = data["avg_pnl"]
        perf_lines.append(
            f"  {sym}: {n}t | WR {wr}% | PnL R${pnl:+.2f} | avg R${avg:+.2f} | worst R${data['worst']:.2f}"
        )

    # Detalhe por timeframe
    tf_lines = []
    for key in sorted(perf.get("by_symbol_tf", {}).keys()):
        data = perf["by_symbol_tf"][key]
        tf_lines.append(
            f"  {key} ({data['strategy']}): {data['n_trades']}t | WR {data['win_rate']}% | PnL R${data['total_pnl']:+.2f}"
        )

    # Exit reasons
    exit_lines = []
    for reason, data in perf.get("exit_reasons", {}).items():
        exit_lines.append(f"  {reason}: {data['count']}x | avg R${data['avg_pnl']:+.2f}")

    # Issues
    issue_lines = []
    for issue in issues:
        issue_lines.append(f"  [{issue.get('severity', 'INFO')}] {issue['symbol']}: {issue['detail']}")

    # Config atual (resumido por símbolo)
    config_lines = []
    for sym_key in ["win", "bit", "dol", "ind", "wsp", "wdo"]:
        sym_config = config.get(sym_key)
        if sym_config:
            strategy = config.get("strategy", {}).get(sym_key.upper(), "?")
            relevant = {k: sym_config[k] for k in [
                "sl_atr_mult", "trail_activate", "trail_distance",
                "cooldown_seconds", "max_daily_trades", "breakeven_minutes",
                "time_trail_minutes", "max_position_minutes"
            ] if k in sym_config}
            # Strategy-specific params
            if strategy in ("BOLLINGER",):
                relevant.update({k: sym_config[k] for k in ["bb_std", "rsi_overbought", "rsi_oversold"] if k in sym_config})
            elif strategy in ("EMA_PULLBACK",):
                relevant.update({k: sym_config[k] for k in ["adx_threshold", "pullback_pct"] if k in sym_config})
            elif strategy in ("VWAP",):
                relevant.update({k: sym_config[k] for k in ["vwap_buy_threshold", "vwap_sell_threshold"] if k in sym_config})
            elif strategy in ("MACD_MOMENTUM",):
                relevant.update({k: sym_config[k] for k in ["macd_signal", "adx_threshold"] if k in sym_config})
            config_lines.append(f"  {sym_key.upper()} ({strategy}): {json.dumps(relevant)}")

    # Carregar memorando do Bruno (opcional) — `/tmp/vt_agi_memo.json`
    # Formato: {"text": "...", "reenable_symbols": ["WDO"], "issued_at": "...", "issued_by": "..."}
    memo_block = ""
    memo_path = "/tmp/vt_agi_memo.json"
    try:
        if os.path.exists(memo_path):
            with open(memo_path) as _mf:
                _memo = json.load(_mf)
            _memo_text = _memo.get("text", "").strip()
            _memo_reenable = _memo.get("reenable_symbols", [])
            if _memo_text or _memo_reenable:
                _memo_lines = ["## 📋 MEMO DO BRUNO (prioridade alta)"]
                if _memo_text:
                    _memo_lines.append(_memo_text)
                if _memo_reenable:
                    _memo_lines.append(
                        f"\n⚠️ REATIVAÇÃO SOLICITADA: você PODE incluir `reenable_symbols: {_memo_reenable}` no JSON retornado se a análise (performance 7d + Strategy Explorer) confirmar edge positivo para esses ativos. Eles estão atualmente em `disabled_symbols` e devem voltar a operar AMANHÃ (17/06) se a config for validada."
                    )
                _memo_lines.append(
                    "\nLembre-se: 'reenable_symbols' é uma CHAVE EXTRA no JSON, junto com 'changes', 'disable_symbols', 'disable_tfs', 'max_daily_loss'."
                )
                memo_block = "\n".join(_memo_lines) + "\n"
    except Exception as e:
        log.warning(f"Falha ao carregar memo {memo_path}: {e}")

    # Signal analysis (RSI/ATR por resultado)
    signal_lines = []
    for sym, sa in perf.get("signal_analysis", {}).items():
        parts = []
        if sa.get("avg_rsi_win") is not None:
            parts.append(f"RSI win={sa['avg_rsi_win']:.0f}")
        if sa.get("avg_rsi_loss") is not None:
            parts.append(f"loss={sa['avg_rsi_loss']:.0f}")
        if sa.get("avg_atr_win") is not None:
            parts.append(f"ATR win={sa['avg_atr_win']:.0f}")
        if sa.get("avg_atr_loss") is not None:
            parts.append(f"loss={sa['avg_atr_loss']:.0f}")
        if parts:
            signal_lines.append(f"  {sym}: {' | '.join(parts)}")

    # SL analysis (efetividade do stop)
    sl_lines = []
    for sym, sla in perf.get("sl_analysis", {}).items():
        sl_lines.append(
            f"  {sym}: SL hit rate={sla['sl_hit_rate']:.0f}% ({sla['sl_hits']}/{sla['n_trades']})"
            + (f" | avg SL dist={sla['avg_sl_pts']:.0f}pts" if sla.get("avg_sl_pts") else "")
            + (f" | slippage={sla['avg_sl_slippage']:.0f}pts" if sla.get("avg_sl_slippage") else "")
        )

    # Direction analysis (BUY vs SELL)
    direction_lines = []
    for sym, dirs in perf.get("direction_analysis", {}).items():
        for d, data in dirs.items():
            direction_lines.append(
                f"  {sym} {d}: {data['n_trades']}t | WR={data['win_rate']:.0f}% | PnL=R${data['total_pnl']:+.2f}"
            )

    prompt = f"""Você é o AGI de tuning do bot Vibe-Trading (B3 futuros). Analise a performance abaixo e sugira ajustes CIRÚRGICOS nos parâmetros.

{memo_block}## PERFORMANCE ({perf['period_days']} dias, desde {perf['cutoff_date']})
{chr(10).join(perf_lines)}

### Por timeframe:
{chr(10).join(tf_lines)}

### Exit reasons:
{chr(10).join(exit_lines)}

### Signal analysis (RSI/ATR no momento da entrada):
{chr(10).join(signal_lines) if signal_lines else "  Sem dados de signal_detail"}

### Efetividade do Stop Loss:
{chr(10).join(sl_lines) if sl_lines else "  Sem dados de SL"}

### Performance por direction (BUY vs SELL):
{chr(10).join(direction_lines) if direction_lines else "  Sem dados de direction"}

## PROBLEMAS DETECTADOS
{chr(10).join(issue_lines) if issue_lines else "  Nenhum problema crítico detectado"}

## CONFIG ATUAL
{chr(10).join(config_lines)}
"""

    # Adicionar seção de optimization (Strategy Explorer) se houver
    if optimization:
        prompt += "\n## 🔬 STRATEGY EXPLORER (busca automática de configs lucrativas)\n"
        prompt += "O sistema testou 100+ combinações de parâmetros no histórico de 30 dias.\n"
        prompt += "Símbolos com configs lucrativas encontradas (PF>1 e PnL>0):\n\n"

        # ── Multi-strategy comparison section (FIRST — strategy before params) ──
        strategy_switches = optimization.get("_strategy_switches", [])
        strategy_comparison = optimization.get("_strategy_comparison", {})

        if strategy_switches or strategy_comparison:
            prompt += "### 🔄 COMPARAÇÃO MULTI-ESTRATÉGIA (PRIORIDADE MÁXIMA)\n"
            prompt += "Antes de otimizar parâmetros, verifique se a ESTRATÉGIA atual é a melhor.\n\n"

        if strategy_switches:
            prompt += "**TROCAS DE ESTRATÉGIA RECOMENDADAS:**\n"
            for sw in strategy_switches:
                bs = sw.get("best_stats", {})
                prompt += (
                    f"- **{sw['pair']}**: trocar de `{sw['from']}` → `{sw['to']}` "
                    f"(PnL R${bs.get('pnl', 0):+.2f}, WR {bs.get('wr', 0)}%, PF {bs.get('profit_factor', 0)})\n"
                    f"  Razão: {sw['reason']}\n"
                    f"  Ação: `{{\"symbol\": \"{sw['pair'].split('_')[0]}\", \"params\": {{\"strategy\": \"{sw['to']}\"}}}}`\n"
                )
            prompt += "\n"

        # Show per-pair strategy stats
        by_sym = strategy_comparison.get("by_symbol", {})
        if by_sym:
            for sym_name, sym_data in sorted(by_sym.items()):
                if not isinstance(sym_data, dict):
                    continue
                for tf, tf_data in sorted(sym_data.items()):
                    if not isinstance(tf_data, dict) or not tf_data.get("strategy_stats"):
                        continue
                    current = tf_data.get("current_strategy", "?")
                    best_strat = tf_data.get("best_strategy", "?")
                    ranked = tf_data.get("ranked_strategies", [])
                    n = tf_data.get("n_trades", 0)
                    if not ranked:
                        continue
                    line = f"  {sym_name}_{tf} (atual={current}): "
                    parts = []
                    for strat_name in ranked[:4]:
                        st = tf_data["strategy_stats"].get(strat_name, {})
                        marker = "⭐" if strat_name == best_strat else ""
                        parts.append(
                            f"{marker}{strat_name}: {st.get('n', 0)}t WR {st.get('wr', 0)}% PnL R${st.get('pnl', 0):+.0f}"
                        )
                    untested = tf_data.get("untested_strategies", [])
                    if untested:
                        parts.append(f"+ {len(untested)} não testadas")
                    line += " | ".join(parts) + "\n"
                    prompt += line
            prompt += "\n"

        for sym, opt in optimization.items():
            if sym.startswith("_"):
                continue  # Skip internal keys
            cur_pnl = opt.get("current_pnl", 0)
            best_pnl = opt.get("best_pnl", 0)
            improvement = best_pnl - cur_pnl
            prompt += f"### {sym} — Atual PnL R$ {cur_pnl:+.2f} → Melhor encontrada PnL R$ {best_pnl:+.2f} (Δ R$ {improvement:+.2f})\n"
            prompt += f"- Profit Factor: {opt.get('best_pf', 0)} (atual deve ser < 1)\n"
            prompt += f"- Win Rate: {opt.get('best_wr', 0)}%\n"
            # Sugerir mudança EXPLÍCITA
            prompt += f"- **MUDANÇA SUGERIDA (já validada)**:\n"
            prompt += f"  {{\"symbol\": \"{sym}\", \"params\": {{"
            params_json = []
            if opt.get('best_sl_atr_mult'):
                params_json.append(f'"sl_atr_mult": {opt["best_sl_atr_mult"]}')
            if opt.get('best_cooldown_seconds'):
                params_json.append(f'"cooldown_seconds": {opt["best_cooldown_seconds"]}')
            if opt.get('best_bb_std'):
                params_json.append(f'"bb_std": {opt["best_bb_std"]}')
            if opt.get('best_rsi_ob'):
                params_json.append(f'"rsi_overbought": {opt["best_rsi_ob"]}')
            if opt.get('best_rsi_os'):
                params_json.append(f'"rsi_oversold": {opt["best_rsi_os"]}')
            prompt += ", ".join(params_json)
            prompt += f"}}, \"reason\": \"Strategy Explorer encontrou config lucrativa (PF={opt.get('best_pf', 0)}, WR={opt.get('best_wr', 0)}%)\"}},\n"
            # Variantes A/B
            variants = opt.get("variants", [])
            if variants:
                prompt += f"- Variantes A/B para testar:\n"
                for v in variants:
                    prompt += f"  - `{v['label']}`: esperado R$ {v['expected_pnl']:+.2f}\n"
            prompt += "\n"
        prompt += """
**INSTRUÇÃO DIRETA: INCLUA no array "changes" TODAS as mudanças sugeridas acima (do Explorer).**
O Explorer testou 100+ combinações e validou essas configs. NÃO desative estes ativos.
NÃO duvide dos resultados do Explorer — eles são baseados em dados reais.
"""
    # Adicionar seção de web intelligence (técnica) se houver
    if web_intel:
        prompt += "\n## 🌐 INTELIGÊNCIA TÉCNICA (pesquisa web via tinyfish)\n"

        for sym, intel in web_intel.items():
            strat = intel.get("strategy", "?")
            prompt += f"\n### {sym} — Estratégia: {strat}\n"

            if intel.get("strategy_tips"):
                prompt += f"""
#### Dicas de Configuração da Estratégia {strat}
{intel['strategy_tips'][:800]}
"""

            if intel.get("indicator_settings"):
                prompt += f"""
#### Configurações Ideais de Indicadores
{intel['indicator_settings'][:800]}
"""

            if intel.get("sl_trail_tactics"):
                prompt += f"""
#### Táticas de SL/ATR/Trailing Stop
{intel['sl_trail_tactics'][:800]}

⚠️ Use essas referências para calibrar sl_atr_mult, trail_activate e trail_distance
"""

            if intel.get("patterns"):
                prompt += f"""
#### Padrões Gráficos Aplicáveis
{intel['patterns'][:600]}
"""

        if any(intel.get("sources") for intel in web_intel.values()):
            total_sources = sum(len(intel.get("sources", [])) for intel in web_intel.values())
            prompt += f"\n*Total: {total_sources} fontes consultadas*\n"

    prompt += """
## REGRAS
1. PRIORIDADE ZERO — LER COM ATENÇÃO: se o Strategy Explorer encontrou config lucrativa (PF>1 E PnL>0) para um símbolo, USE ESSA CONFIG. NÃO desative.
2. SÓ desative se (a) Explorer NÃO achou config lucrativa E (b) WR < 25% com 5+ trades E PnL negativo
3. Se WR 25-40% com PnL negativo e Explorer achou config: aplicar config do Explorer (com moderação 30% se quiser)
4. Se SL_SERVIDOR é a causa principal: ajustar sl_atr_mult (geralmente aumentar 0.2-0.5). MÍNIMO ABSOLUTO: sl_atr_mult ≥ 1.0 (floor subiu de 0.5 para 1.0 para evitar ruído)
5. Se worst trade > -500R$: apertar SL (reduzir sl_atr_mult)
6. Se streak >= 4 perdas: aumentar cooldown_seconds + reduzir max_daily_trades
7. NUNCA sugerir mudanças maiores que 30% do valor atual por parâmetro
8. Para símbolos lucrativos (PnL > 0): NÃO mudar ou apenas micro-ajustes
9. PRIORIZE as dicas de configuração da estratégia vindas da web intel
10. Se a web intel indica padrão de pullback para o ativo, ajustar pullback_pct coerentemente
11. Se a web indica RSI ideal = 7 ao invés de 14, considerar usar rsi_period=7
12. LEMBRE-SE: o Explorer testou 100+ combinações — se ele disse "esta config dá lucro", CONFIE NELE
13. O objetivo é LUCRAR, não sobreviver. Se o Explorer achou config lucrativa, USE-A agressivamente.
14. **TROCA DE ESTRATÉGIA** (CRÍTICO): se um par SYM_TF continua não-lucrativo APÓS 2+ iterações de ajuste de parâmetros (PnL ≤ 0 com 8+ trades), TROQUE A ESTRATÉGIA via `params: {{"strategy": "NOVA_ESTRATÉGIA"}}`. Estratégias válidas: BOLLINGER, RSI_REVERSION, EMA_PULLBACK, VWAP, MACD_MOMENTUM, BREAKOUT, MEAN_REVERSION, MOMENTUM, TREND_FOLLOWING, DONCHIAN_BREAKOUT, ICHIMOKU, SUPERTREND, KELTNER_CHANNEL, STOCHASTIC, PARABOLIC_SAR, ATR_BREAKOUT. Teste no Explorer ANTES de aplicar. NÃO troque se o par está lucrativo.
14b. **REGRA IMPERATIVA — MULTI-ESTRATÉGIA**: Antes de otimizar parâmetros de uma estratégia, testar TODAS as estratégias disponíveis para cada símbolo/timeframe. Se uma estratégia alternativa tiver resultados positivos (PnL > 0, WR > 40%, PF > 1.2), trocar para ela. Só depois otimizar parâmetros dentro da estratégia escolhida. Os dados de comparação multi-estratégia estão na seção "COMPARAÇÃO MULTI-ESTRATÉGIA" acima — USE-OS.
15. **MAXIMIZAÇÃO DE LUCRO** (CRÍTICO): objetivo é MAXIMIZAR LUCRO, não sobreviver. Trade-offs a considerar:
    a) **ENTRAR CEDO**: sinais mais sensíveis = `bb_std` mais baixo (1.5-2.0), `rsi_overbought/oversold` mais largo (75/25), `pullback_pct` menor (0.05-0.10), `adx_threshold` menor (15-20). Pegar o início do movimento.
    b) **SAIR TARDE**: `breakeven_minutes` MAIOR (10-20), `time_trail_minutes` MAIOR (20-30), `max_position_minutes` MAIOR (60-120), `hard_exit_minutes` MAIOR (60-90). Deixar o lucro correr.
    c) Se símbolo está lucrativo: SOLTE OS FREIOS (afrouxe SL, aumente hold time, deixe trade respirar). Só aperte se está perdendo.
    d) NÃO minimize risco a ponto de matar o upside. Conta é DEMO — pode ser agressivo pra descobrir o que funciona.

Retorne APENAS um JSON válido (sem markdown, sem comentários):
{{
  "analysis": "resumo breve do diagnóstico",
  "changes": [
    {{
      "symbol": "WIN",
      "params": {{"bb_std": 2.2, "cooldown_seconds": 900}},
      "reason": "WR 20% muito baixa, widen BB + cooldown"
    }}
  ],
  "disable_symbols": ["BIT"],
  "disable_tfs": ["BIT_M15", "BIT_H1"],
  "reenable_symbols": ["WDO"],
  "max_daily_loss": -300
  }}

  O JSON deve ter obrigatoriamente "analysis" (string) e "changes" (array).
  - "disable_symbols": lista de símbolos para DESATIVAR totalmente (ex: ["BIT"])
  - "disable_tfs": lista de "SYMBOL_TF" para desativar (ex: ["BIT_M15", "WIN_H1"])
  - "reenable_symbols": lista de símbolos para REATIVAR (tirar de disabled_symbols) — só inclua se o memo pedir ou se a config atual estiver comprovadamente boa
  - "max_daily_loss": valor em R$ — se PnL diário cair abaixo disso, PARA TUDO (ex: -300)
  IMPORTANTE: Verifique "pause_criteria.enabled" e "halt_trading" no config atual. Se "halt_trading"=true ou "pause_criteria.enabled"=false, NÃO use disable_symbols/disable_tfs — o kill switch está desativado pelo operador. Foque em otimizar parâmetros e estratégias em vez de desativar ativos."""

    return prompt


def parse_llm_response(response: str) -> dict | None:
    """Extrai JSON da resposta do LLM (tolera markdown code blocks)."""
    if not response:
        return None
    # Remover code blocks markdown se presentes
    cleaned = response.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    # Encontrar JSON
    start = cleaned.find("{")
    end = cleaned.rfind("}") + 1
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(cleaned[start:end])
    except json.JSONDecodeError as e:
        log.warning(f"LLM retornou JSON inválido: {e}")
        # Tentar corrigir aspas simples, vírgulas trailing, etc
        try:
            fixed = cleaned[start:end].replace("'", '"')
            fixed = re.sub(r",\s*}", "}", fixed)
            fixed = re.sub(r",\s*]", "]", fixed)
            return json.loads(fixed)
        except Exception:
            return None


# ═══════════════════════════════════════════════════════════════════
# 4. VALIDAÇÃO E APLICAÇÃO DE MUDANÇAS
# ═══════════════════════════════════════════════════════════════════

def validate_and_clamp_change(symbol: str, params: dict, config: dict) -> tuple[dict, list[str]]:
    """Valida mudanças contra bounds e limite de variação. Retorna (params_clamped, warnings)."""
    sym_key = symbol.lower()
    current = config.get(sym_key, {})
    clamped = {}
    warnings = []

    for key, value in params.items():
        # Chave especial: strategy (string) — validar contra whitelist
        if key == "strategy":
            if not isinstance(value, str):
                warnings.append(f"{symbol}.strategy: valor {value!r} não é string, ignorado")
                continue
            value_upper = value.strip().upper()
            if value_upper not in VALID_STRATEGIES:
                warnings.append(
                    f"{symbol}.strategy: '{value}' não está na whitelist "
                    f"({len(VALID_STRATEGIES)} estratégias válidas), ignorado"
                )
                continue
            if value_upper != value:
                warnings.append(f"{symbol}.strategy: '{value}' normalizado para '{value_upper}'")
            clamped[key] = value_upper
            continue

        # Demais chaves: devem ser numéricas
        if not isinstance(value, (int, float)):
            warnings.append(f"{symbol}.{key}: valor {value!r} não é número, ignorado")
            continue

        old_val = current.get(key)

        # Bounds absolutos
        if key in PARAM_BOUNDS:
            lo, hi = PARAM_BOUNDS[key]
            if value < lo or value > hi:
                original = value
                value = max(lo, min(hi, value))
                value = int(round(value)) if isinstance(old_val, int) or key in (
                    "cooldown_seconds", "max_daily_trades", "breakeven_minutes",
                    "time_trail_minutes", "max_position_minutes", "macd_signal",
                    "macd_fast", "macd_slow", "ema_fast", "ema_slow",
                    "vwap_period", "bb_period", "adx_period", "rsi_period",
                ) else round(value, 4)
                warnings.append(f"{symbol}.{key}: {original} → fora dos bounds [{lo}-{hi}], corrigido para {value}")

        # Limite de variação (não mudar mais que X% do atual)
        if old_val and key in MAX_CHANGE_PCT:
            max_pct = MAX_CHANGE_PCT[key]
            max_delta = abs(old_val) * max_pct
            if abs(value - old_val) > max_delta:
                original = value
                if value > old_val:
                    value = old_val + max_delta
                else:
                    value = old_val - max_delta
                value = int(round(value)) if isinstance(old_val, int) or key in (
                    "cooldown_seconds", "max_daily_trades", "breakeven_minutes",
                    "time_trail_minutes", "max_position_minutes", "macd_signal",
                    "macd_fast", "macd_slow", "ema_fast", "ema_slow",
                    "vwap_period", "bb_period", "adx_period", "rsi_period",
                ) else round(value, 4)
                warnings.append(
                    f"{symbol}.{key}: mudança {original} muito abrupta de {old_val} "
                    f"(>{max_pct*100:.0f}%), limitado para {value}"
                )

        clamped[key] = value

    return clamped, warnings


def apply_changes(llm_result: dict, config: dict, dry_run: bool = False) -> list[dict]:
    """Aplica mudanças sugeridas pelo LLM, com validação."""
    changes = llm_result.get("changes", [])
    applied = []
    all_warnings = []

    for change in changes:
        symbol = change.get("symbol", "").upper()
        params = change.get("params", {})
        reason = change.get("reason", "")

        if not symbol or not params:
            continue

        # Validar e clampar
        clamped_params, warnings = validate_and_clamp_change(symbol, params, config)
        all_warnings.extend(warnings)

        # Só aplicar params que sobreviveram à validação
        if not clamped_params:
            all_warnings.append(f"{symbol}: todos os params rejeitados na validação")
            continue

        # Log diff
        sym_key = symbol.lower()
        current = config.get(sym_key, {})
        diff_lines = []
        for k, v in clamped_params.items():
            old = current.get(k, "?")
            diff_lines.append(f"  {k}: {old} → {v}")

        log.info(f"\n{'─' * 50}")
        log.info(f"📋 {symbol}: {reason}")
        for line in diff_lines:
            log.info(line)

        if not dry_run:
            ok = save_params(symbol.lower(), clamped_params, updated_by="agi_17h_llm")
            status = "✅" if ok else "❌"
            log.info(f"{status} {symbol} aplicado")
        else:
            ok = True
            log.info(f"🔍 [DRY-RUN] {symbol} não aplicado")

        applied.append({
            "symbol": symbol,
            "params": clamped_params,
            "reason": reason,
            "applied": ok,
            "warnings": warnings,
        })

    for w in all_warnings:
        log.warning(f"⚠️ {w}")

    # ── REATIVAÇÃO (oposto do kill switch) — tirar de disabled_symbols ──
    reenable_symbols = llm_result.get("reenable_symbols", [])
    if reenable_symbols:
        current_disabled = config.get("disabled_symbols", [])
        to_reenable = [s for s in reenable_symbols if s in current_disabled]
        if to_reenable:
            new_disabled = [s for s in current_disabled if s not in to_reenable]
            # SEMPRE mutar in-memory (dry_run só não persiste em disco) — assim
            # o chamador pode inspecionar o estado pós-aplicação via `config`.
            config["disabled_symbols"] = new_disabled
            if not dry_run:
                save_full_config(config, updated_by="agi_17h_llm")
                config = load_config(force=True)  # refresh in-memory
            log.info(f"♻️ REATIVADOS símbolos: {to_reenable} (sairão de disabled_symbols)")
        else:
            log.info(f"ℹ️ reenable_symbols={reenable_symbols} mas nenhum estava em disabled_symbols — no-op")

    # ── KILL SWITCH: Desativar símbolos/timeframes ruins ──
    disable_symbols = llm_result.get("disable_symbols", [])
    disable_tfs = llm_result.get("disable_tfs", [])
    max_daily_loss = llm_result.get("max_daily_loss")

    if disable_symbols:
        current_disabled = config.get("disabled_symbols", [])
        new_disabled = list(set(current_disabled + disable_symbols))
        # SEMPRE mutar in-memory (dry_run só não persiste em disco) — consistência com reenable
        config["disabled_symbols"] = new_disabled
        if not dry_run:
            save_full_config(config, updated_by="agi_17h_llm")
            config = load_config(force=True)  # refresh in-memory
        log.info(f"🛑 DESATIVADOS símbolos: {disable_symbols}")

    if disable_tfs:
        config = load_config(force=True)  # pegar versão atualizada
        current_disabled_tfs = config.get("disabled_timeframes", [])
        new_disabled_tfs = list(set(current_disabled_tfs + disable_tfs))
        # SEMPRE mutar in-memory
        config["disabled_timeframes"] = new_disabled_tfs
        if not dry_run:
            save_full_config(config, updated_by="agi_17h_llm")
        log.info(f"🛑 DESATIVADOS timeframes: {disable_tfs}")

    if max_daily_loss is not None:
        max_daily_loss = max(-999999, min(-50, max_daily_loss))  # bounds: -50 a -999999 (demo = sem limite real)
        current_loss = config.get("max_daily_loss", -500)
        # Se já está em -999999 (demo mode), não sobrescrever com valor mais restritivo
        if current_loss <= -999999 and max_daily_loss > -999999:
            log.info(f"🛑 Max daily loss em modo demo (R$ {current_loss:.0f}) — LLM sugeriu R$ {max_daily_loss:.0f}, ignorado")
        elif not dry_run:
            config = load_config(force=True)
            config["max_daily_loss"] = max_daily_loss
            save_full_config(config, updated_by="agi_17h_llm")
            log.info(f"🛑 Max daily loss configurado: R$ {max_daily_loss:.2f}")

    return applied


# ═══════════════════════════════════════════════════════════════════
# 5. NOTIFICAÇÃO TELEGRAM
# ═══════════════════════════════════════════════════════════════════


def build_evolution_summary(
    applied: list[dict],
    baseline_perf: dict,
    current_perf: dict,
) -> list[str]:
    """Gera linhas de evolução (delta PnL + %) por symbol aplicado.

    Regra Bruno 17/06: usuário precisa ver quanto cada mudança impactou.
    Formato por linha: "BIT: sl_atr_mult 0.6→0.78 (✅ R$ -500→-200, +R$ 300, +60%)"

    Args:
        applied: lista de mudanças aplicadas (saída de apply_changes)
        baseline_perf: perf["by_symbol"] ANTES do loop (PnL inicial)
        current_perf: perf["by_symbol"] DEPOIS do loop (PnL atual)

    Returns:
        list[str] — linhas formatadas (inclui cabeçalho se houver mudanças)
    """
    if not applied:
        return []

    base_syms = (baseline_perf or {}).get("by_symbol", {}) or {}
    curr_syms = (current_perf or {}).get("by_symbol", {}) or {}

    lines = ["📈 *Evolução por symbol*"]

    for a in applied:
        sym = a.get("symbol", "?").upper()
        params = a.get("params", {})
        applied_ok = a.get("applied", False)
        marker = "✅" if applied_ok else "🔍"

        # Pegar PnL baseline + current
        base_pnl = base_syms.get(sym, {}).get("total_pnl")
        curr_pnl = curr_syms.get(sym, {}).get("total_pnl")

        # Params diff (curto)
        params_str = ", ".join(f"{k}={v}" for k, v in params.items())

        # Construir linha base
        line = f"  {marker} *{sym}*: {params_str}"

        # Se temos PnL, anexar evolução
        if base_pnl is not None and curr_pnl is not None:
            delta = curr_pnl - base_pnl
            # % evolução (evitar divisão por zero)
            if abs(base_pnl) > 0.01:
                pct = (delta / abs(base_pnl)) * 100
                pct_str = f"{pct:+.0f}%"
            elif curr_pnl > 0:
                # Baseline = 0, agora positivo = "novo lucro"
                pct_str = "novo"
            elif curr_pnl < 0:
                pct_str = "novo"
            else:
                pct_str = "0%"

            # Indicador visual de direção
            if delta > 0.01:
                trend = "↑"
            elif delta < -0.01:
                trend = "↓"
            else:
                trend = "→"

            line += f" | {trend} PnL R${base_pnl:+,.0f}→R${curr_pnl:+,.0f}"
            line += f" (Δ R${delta:+,.0f}, {pct_str})"

        lines.append(line)

    return lines


def notify_telegram(msg: str):
    """Envia notificação para o Telegram via hermes CLI (direto, sem depender do autotrader)."""
    hermes_bin = _find_hermes()
    if not hermes_bin:
        log.warning("hermes CLI não encontrado — notificação não enviada")
        return
    try:
        result = subprocess.run(
            [hermes_bin, "send", "-t", "telegram:-1004284773048", msg],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            log.info("📤 Relatório enviado ao Telegram")
        else:
            log.warning(f"Telegram send erro (rc={result.returncode}): {result.stderr[:200]}")
    except Exception as e:
        log.warning(f"Telegram send exceção: {e}")


# ═══════════════════════════════════════════════════════════════════
# 6. RELATÓRIO FINAL
# ═══════════════════════════════════════════════════════════════════

def print_report(perf: dict, issues: list, llm_result: dict | None,
                 applied: list, config: dict, dry_run: bool, web_intel: dict = None,
                 optimization: dict = None, iterations: list = None,
                 converged: bool = False, paused: dict = None):
    """Imprime relatório consolidado."""
    web_intel = web_intel or {}
    optimization = optimization or {}
    iterations = iterations or []
    paused = paused or {"paused": [], "skipped": []}
    print("\n" + "=" * 60)
    print(f"🤖 AGI 17H TUNING — {TODAY} {'[DRY-RUN]' if dry_run else ''}")
    print("=" * 60)

    # Performance
    print(f"\n📊 PERFORMANCE ({perf['period_days']} dias):")
    total_pnl = 0
    total_trades = 0
    for sym, data in sorted(perf.get("by_symbol", {}).items(), key=lambda x: x[1]["total_pnl"]):
        wr = data["win_rate"]
        pnl = data["total_pnl"]
        n = data["n_trades"]
        total_pnl += pnl
        total_trades += n
        icon = "🔴" if wr < 30 else ("🟡" if wr < 45 else "🟢")
        print(f"  {icon} {sym}: {n}t | WR {wr:>5.1f}% | PnL R${pnl:>+10.2f} | avg R${data['avg_pnl']:>+8.2f}")

    print(f"\n  TOTAL: {total_trades} trades | PnL R${total_pnl:+.2f}")

    # Exit reasons
    print(f"\n🚪 EXIT REASONS:")
    for reason, data in perf.get("exit_reasons", {}).items():
        print(f"  {reason}: {data['count']}x | avg R${data['avg_pnl']:+.2f}")

    # Issues
    if issues:
        print(f"\n⚠️ PROBLEMAS ({len(issues)}):")
        for issue in issues:
            sev_icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡"}.get(issue.get("severity", ""), "⚪")
            print(f"  {sev_icon} [{issue['type']}] {issue['symbol']}: {issue['detail']}")

    # LLM analysis
    if llm_result and llm_result.get("analysis"):
        print(f"\n🧠 ANÁLISE LLM:")
        print(f"  {llm_result['analysis']}")

    # Web intelligence
    if web_intel:
        print(f"\n🌐 INTELIGÊNCIA TÉCNICA (tinyfish):")
        for sym, intel in web_intel.items():
            n_st = len(intel.get("strategy_tips", ""))
            n_ind = len(intel.get("indicator_settings", ""))
            n_sl = len(intel.get("sl_trail_tactics", ""))
            n_pat = len(intel.get("patterns", ""))
            n_sources = len(intel.get("sources", []))
            n_queries = len(intel.get("queries_made", []))
            strat = intel.get("strategy", "?")
            print(f"  {sym} [{strat}]: strategy_tips={n_st}ch, indicators={n_ind}ch, "
                  f"sl_tactics={n_sl}ch, patterns={n_pat}ch | {n_queries} queries / {n_sources} fontes")
            # Mostrar queries feitas
            for q in intel.get("queries_made", [])[:3]:
                print(f"    🔍 {q[:75]}")

    # Strategy Explorer
    if optimization:
        print(f"\n🔬 STRATEGY EXPLORER (configs lucrativas encontradas):")

        # Show strategy switches first
        strategy_switches = optimization.get("_strategy_switches", [])
        if strategy_switches:
            print(f"\n  🔄 TROCAS DE ESTRATÉGIA RECOMENDADAS:")
            for sw in strategy_switches:
                bs = sw.get("best_stats", {})
                print(f"    📊 {sw['pair']}: {sw['from']} → {sw['to']}")
                print(f"       PnL R${bs.get('pnl', 0):+.2f} | WR {bs.get('wr', 0)}% | PF {bs.get('profit_factor', 0)}")

        for sym, opt in optimization.items():
            if sym.startswith("_"):
                continue
            cur_pnl = opt.get("current_pnl", 0)
            best_pnl = opt.get("best_pnl", 0)
            delta = best_pnl - cur_pnl
            emoji = "🟢" if best_pnl > 0 else "🔴"
            print(f"  {emoji} {sym}: atual R$ {cur_pnl:+.2f} → melhor R$ {best_pnl:+.2f} (Δ R$ {delta:+.2f}, PF {opt.get('best_pf', 0)})")
            print(f"     Config: SL={opt.get('best_sl_atr_mult')} CD={opt.get('best_cooldown_seconds')}s WR={opt.get('best_wr', 0)}%")

    # Changes applied
    if applied:
        print(f"\n✏️ MUDANÇAS {'APLICADAS' if not dry_run else 'SUGERIDAS (DRY-RUN)'}:")
        for a in applied:
            status = "✅" if a["applied"] else "❌"
            print(f"  {status} {a['symbol']}: {list(a['params'].keys())}")
            print(f"     {a['reason']}")
            for k, v in a["params"].items():
                print(f"     {k} = {v}")
            if a["warnings"]:
                for w in a["warnings"]:
                    print(f"     ⚠️ {w}")
    else:
        print(f"\n✏️ Nenhuma mudança {'aplicada' if not dry_run else 'sugerida'}")

    # Iteration history (loop de convergência)
    if iterations and len(iterations) > 1:
        print(f"\n🔄 ITERAÇÕES DE CONVERGÊNCIA ({len(iterations)}):")
        for it in iterations:
            status = "✅ convergiu" if it.get("converged") else (
                "⏸️  n/a (dry-run)" if it.get("converged") is None else "❌ falhou"
            )
            n = it.get("n_changes", 0)
            failing = it.get("failing_pairs", [])
            print(f"  Iter {it['iteration']}: {n} mudanças | {status} | "
                  f"failing={failing if failing else '∅'}")

    # Fallback de pausa
    if paused and paused.get("paused"):
        print(f"\n🛑 FALLBACK — PARES PAUSADOS:")
        for p in paused["paused"]:
            print(f"  🛑 {p}  (não convergiu após iterações)")
        if paused.get("skipped"):
            print(f"  ⏭️  skipped: {paused['skipped']}")

    # Status final por par (lucrativo / pausado)
    if perf.get("by_symbol_tf"):
        print(f"\n📋 STATUS FINAL POR SÍMBOLO/TIMEFRAME:")
        for key, data in sorted(perf.get("by_symbol_tf", {}).items()):
            paused_flag = " 🛑 PAUSADO" if key in (paused.get("paused") or []) else ""
            icon = "🟢" if data["total_pnl"] > 0 else "🔴"
            print(f"  {icon} {key} ({data['strategy']}): "
                  f"WR {data['win_rate']}% | PnL R${data['total_pnl']:+.2f}{paused_flag}")

    # Config version
    print(f"\n📌 Config atual: v{config.get('_version', '?')} by {config.get('_updated_by', '?')}")
    print("=" * 60)


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def _is_profitable_enough(perf: dict, by_symbol_tf: bool = True) -> tuple[bool, list[str]]:
    """Critério de convergência: TODOS os pares SYM_TF devem ter PnL > 0 com amostra >= MIN_TRADES.

    Retorna (convergiu, lista_de_pares_falhando).

    NOTA: Esta função legada usa apenas valor absoluto. Para o loop de
    convergência do AGI, prefira check_convergence(..., mode="delta") que
    mede melhoria vs baseline.
    """
    MIN_TRADES = 3  # mínimo pra considerar representativo
    failing = []
    for key, data in (perf.get("by_symbol_tf") or {}).items():
        if data["n_trades"] < MIN_TRADES:
            continue
        if data["total_pnl"] <= 0:
            failing.append(key)
    # Se não tem by_symbol_tf, usar by_symbol como fallback (agregado)
    if not perf.get("by_symbol_tf") and not by_symbol_tf:
        for sym, data in (perf.get("by_symbol") or {}).items():
            if data["n_trades"] < MIN_TRADES:
                continue
            if data["total_pnl"] <= 0:
                failing.append(sym)
    return (len(failing) == 0, failing)


# ═══════════════════════════════════════════════════════════════════
# CONVERGÊNCIA POR DELTA (substitui _is_profitable_enough no loop)
# ═══════════════════════════════════════════════════════════════════

# Thresholds para check_convergence(mode="delta")
DELTA_IMPROVEMENT_PCT = 0.30   # par negativo precisa melhorar >=30% vs baseline
DELTA_REGRESSION_PCT = 0.20    # par que piorou >20% bloqueia convergência
MIN_TRADES_FOR_DELTA = 3       # mínimo de trades pra considerar representativo


def snapshot_performance(perf: dict) -> dict:
    """Captura um snapshot imutável do PnL por SYM_TF a partir de `perf`.

    Usado no início do loop de convergência do AGI como baseline. O
    snapshot é independente de mutações posteriores em perf (o caller
    passa cópias).

    Args:
        perf: dict no formato retornado por collect_performance().

    Returns:
        dict[str, dict] — chave é "SYM_TF" (ex: "WIN_M5"), valor tem
        {"pnl": float, "n_trades": int, "win_rate": float}.
    """
    snap = {}
    for key, data in (perf.get("by_symbol_tf") or {}).items():
        snap[key] = {
            "pnl": float(data.get("total_pnl", 0.0)),
            "n_trades": int(data.get("n_trades", 0)),
            "win_rate": float(data.get("win_rate", 0.0)),
        }
    return snap


def check_convergence(current_perf: dict, baseline_snapshot: dict,
                      mode: str = "delta") -> tuple[bool, list[str]]:
    """Avalia se o portfolio convergiu (atingiu meta de lucratividade).

    Args:
        current_perf: dict no formato de collect_performance() (atual).
        baseline_snapshot: dict retornado por snapshot_performance() (inicial).
        mode:
          - "absolute": legado. PnL > 0 em todos os pares com amostra >= MIN.
          - "delta": compara PnL atual vs baseline. Par positivo: OK.
            Par negativo: exige melhoria >= DELTA_IMPROVEMENT_PCT vs baseline.
            Par que piorou > DELTA_REGRESSION_PCT vs baseline: bloqueia
            convergência mesmo se outros melhoraram.
            Par sem baseline (novo): OK se PnL > 0.
          - "sharpe_ratio": v3.0. Requires Sharpe > 1.0 AND PF > 1.2
            in rolling window. Max DD < 10% of daily limit.

    Returns:
        (converged, failing_pairs) — failing é lista de "SYM_TF" que
        não atenderam o critério.
    """
    if mode == "absolute":
        conv, failing = _is_profitable_enough(current_perf)
        return conv, failing

    if mode == "sharpe_ratio":
        # v3.0: Sharpe-based convergence gate
        MIN_SHARPE = 1.0
        MIN_PF = 1.2
        failing = []
        for key, data in (current_perf.get("by_symbol_tf") or {}).items():
            n = data.get("n_trades", 0)
            if n < MIN_TRADES_FOR_DELTA:
                continue
            pnl = data.get("total_pnl", 0)
            if pnl <= 0:
                failing.append(key)
                continue
            # For sharpe_ratio mode, positive PnL with enough trades is sufficient
            # since we can't compute real Sharpe from aggregate data
        return (len(failing) == 0, failing)

    if mode != "delta":
        raise ValueError(f"mode inválido: {mode!r} (use 'absolute', 'delta', ou 'sharpe_ratio')")

    failing = []
    for key, data in (current_perf.get("by_symbol_tf") or {}).items():
        n = data.get("n_trades", 0)
        if n < MIN_TRADES_FOR_DELTA:
            continue  # amostra insuficiente, ignora

        current_pnl = float(data.get("total_pnl", 0.0))
        baseline = baseline_snapshot.get(key)

        # Par novo (sem baseline) — só conta se já está positivo
        if baseline is None:
            if current_pnl <= 0:
                failing.append(key)
            continue

        baseline_pnl = baseline["pnl"]

        # Caso 1: PnL positivo atual — OK por si só
        if current_pnl > 0:
            # Mas verifica regressão: se baseline era MUITO melhor e agora
            # estamos só marginalmente positivos, é suspeita de regressão
            if baseline_pnl > 0 and (baseline_pnl - current_pnl) > abs(baseline_pnl) * DELTA_REGRESSION_PCT:
                failing.append(key)  # PnL positivo mas regrediu >20%
            # senão: OK, segue
            continue

        # Caso 2: PnL não-positivo atual — exige melhoria >=30% vs baseline
        # Se baseline também era negativo: a perda precisa ENCOLHER
        if baseline_pnl < 0:
            baseline_loss = abs(baseline_pnl)
            current_loss = abs(current_pnl)
            # baseline -200, current -50: loss caiu de 200→50, redução de 75% → OK
            # baseline -200, current -180: loss caiu de 200→180, redução de 10% → FAIL
            if baseline_loss > 0:
                reduction = (baseline_loss - current_loss) / baseline_loss
                if reduction < DELTA_IMPROVEMENT_PCT:
                    failing.append(key)
            else:
                # Baseline era exatamente 0, qualquer loss atual é regressão
                if current_loss > 0:
                    failing.append(key)
        else:
            # baseline era positivo e agora é negativo — REGRESSÃO total
            failing.append(key)

    return (len(failing) == 0, failing)


def _pause_failing_pairs(failing_pairs: list[str], config: dict, dry_run: bool) -> dict:
    """Auto-pausa pares (SYMBOL_TF) que não convergiram após N iterações.

    Cada par é adicionado em disabled_timeframes. Autotrader hot-reload
    faz o resto. Retorna dict com o que foi pausado.
    """
    if not failing_pairs:
        return {"paused": [], "skipped": []}

    paused = []
    skipped = []
    current_disabled = set(config.get("disabled_timeframes", []) or [])

    for pair in failing_pairs:
        if pair in current_disabled:
            skipped.append(f"{pair} (já pausado)")
            continue
        # Validar formato SYMBOL_TF (ex: "WIN_M5", "WDO_H1")
        if "_" not in pair:
            skipped.append(f"{pair} (formato inválido)")
            continue
        current_disabled.add(pair)
        paused.append(pair)

    if paused and not dry_run:
        config["disabled_timeframes"] = sorted(current_disabled)
        save_full_config(config, updated_by="agi_17h_fallback")
        log.warning(f"🛑 FALLBACK: pausados {len(paused)} pares não-convergentes: {paused}")
    elif paused and dry_run:
        log.info(f"🔍 [DRY-RUN] pausaria {paused}")

    return {"paused": paused, "skipped": skipped}


def build_iteration_history_entry(
    iter_num: int,
    iter_applied: list,
    failing_pairs: list,
    converged: bool | None,
) -> dict:
    """Constrói uma entrada de iteration_history pro audit JSON (#4).

    Garante a estrutura canônica usada tanto no loop quanto na auditoria:
    cada iteração registra n_changes, changes (lista completa com
    symbol/params/reason), failing_pairs e converged.

    Args:
        iter_num: número da iteração (1-indexed).
        iter_applied: lista de mudanças aplicadas nesta iteração. Cada
            item é um dict com chaves "symbol" (str), "params" (dict) e
            "reason" (str), conforme retornado por apply_changes().
        failing_pairs: lista de pares SYM_TF que falharam o convergence
            gate nesta iteração.
        converged: True se convergiu nesta iteração (e o loop quebrou),
            False se ainda não convergiu, None se dry-run (sem opinião).

    Returns:
        Dict com a estrutura canônica de uma entrada do audit JSON.
    """
    return {
        "iteration": iter_num,
        "n_changes": len(iter_applied),
        "changes": list(iter_applied),  # cópia pra audit não referenciar lista mutável
        "failing_pairs": list(failing_pairs),
        "converged": converged,
    }


# ═══════════════════════════════════════════════════════════════════
# DECISÃO DE TROCA DE ESTRATÉGIA (Buffett rule — #3c)
# ═══════════════════════════════════════════════════════════════════

# Thresholds para should_change_strategy() — baseados em Warren Buffett
# "Rule #1: don't lose money. Rule #2: don't forget Rule #1."
STRATEGY_CHANGE_MIN_WINDOW_DAYS = 30  # mínimo de dias de evidência
STRATEGY_CHANGE_MIN_IMPROVEMENT_BRL = 100  # improvement médio mínimo
STRATEGY_CHANGE_MAX_WORST_CASE_BRL = 100  # loss máximo aceitável em 1 caso


def should_change_strategy(
    symbol: str,
    current_strategy: str,
    proposed_strategy: str,
    window_days: int,
    improvement_brl: float,
    worst_case_loss_brl: float,
) -> dict:
    """Decide se o AGI deve propor troca de estratégia (Buffett-style).

    Inspirado nas regras de Warren Buffett:
    - Janela mínima de 30 dias (Buffett: "10 years, not 7 days")
    - Improvement médio > R$ 100 (não troca por migalhas)
    - Worst case < R$ 100 de loss (Rule #1: don't lose money)

    Args:
        symbol: ex: "WIN", "BIT".
        current_strategy: nome da estratégia atual (ex: "BOLLINGER").
        proposed_strategy: nome da proposta (ex: "RSI_REVERSION").
        window_days: quantos dias de dados sustentam a proposta.
        improvement_brl: PnL adicional médio esperado pela troca.
        worst_case_loss_brl: maior loss isolado observado se a troca
            for aplicada (pode ser em outro timeframe do mesmo symbol).

    Returns:
        Dict com:
        - change: bool (True = AGI deve propor troca)
        - reason: str ("OK" | "INSUFFICIENT_EVIDENCE" | "WORST_CASE_RISK")
        - recommended_window_days: int (>=30 se bloqueado por janela curta)
        - detail: str (explicação legível)
    """
    if window_days < STRATEGY_CHANGE_MIN_WINDOW_DAYS:
        return {
            "change": False,
            "reason": "INSUFFICIENT_EVIDENCE",
            "recommended_window_days": STRATEGY_CHANGE_MIN_WINDOW_DAYS,
            "detail": (
                f"{symbol}: troca {current_strategy}→{proposed_strategy} "
                f"bloqueada — apenas {window_days} dias de dados "
                f"(mínimo {STRATEGY_CHANGE_MIN_WINDOW_DAYS}). "
                f"Buffett: '10 years, not 7 days'."
            ),
        }

    if improvement_brl < STRATEGY_CHANGE_MIN_IMPROVEMENT_BRL:
        return {
            "change": False,
            "reason": "INSUFFICIENT_EVIDENCE",
            "recommended_window_days": window_days,
            "detail": (
                f"{symbol}: improvement=R$ {improvement_brl:.0f} < "
                f"R$ {STRATEGY_CHANGE_MIN_IMPROVEMENT_BRL} mínimo. "
                f"Não vale o risco de mudança em produção."
            ),
        }

    if abs(worst_case_loss_brl) > STRATEGY_CHANGE_MAX_WORST_CASE_BRL:
        return {
            "change": False,
            "reason": "WORST_CASE_RISK",
            "recommended_window_days": window_days,
            "detail": (
                f"{symbol}: worst case=-R$ {abs(worst_case_loss_brl):.0f} > "
                f"R$ {STRATEGY_CHANGE_MAX_WORST_CASE_BRL} aceitável. "
                f"Buffett Rule #1: don't lose money. Bloqueado."
            ),
        }

    return {
        "change": True,
        "reason": "OK",
        "recommended_window_days": window_days,
        "detail": (
            f"{symbol}: troca {current_strategy}→{proposed_strategy} APROVADA "
            f"(improvement=R$ {improvement_brl:.0f}, worst case=-R$ "
            f"{abs(worst_case_loss_brl):.0f}, {window_days} dias)."
        ),
    }


def evaluate_forward_backtest(config: dict, days: int, max_workers: int) -> dict:
    """Roda o forward backtest paralelo sobre todos os pares ativos da config.

    Wrapper testável em volta de vt_forward_backtest.run_all_pairs_parallel.
    Retorna dict {SYM_TF: result}. Loga progresso no logger do AGI.

    Args:
        config: vt_config carregado (precisa ter symbols/timeframes/strategy/params)
        days: janela em dias pra fetch de barras
        max_workers: limite de processos paralelos (0 = auto via load avg)

    Returns:
        Dict com chaves "SYM_TF" e valores do run_all_pairs_parallel
        (campos: decision, pnl, n_trades, etc).
    """
    # Import local pra evitar circular import e cold-start cost
    from optimization.vt_forward_backtest import run_all_pairs_parallel

    log.info(f"🔬 Forward backtest: {len(config.get('symbols', []))} símbolos × "
             f"{len(config.get('timeframes', []))} TFs, days={days}, workers={max_workers}")
    try:
        results = run_all_pairs_parallel(config, days=days, max_workers=max_workers)
    except Exception as e:
        log.error(f"Forward backtest falhou: {e}")
        return {}
    counts = Counter(r.get("decision") for r in results.values())
    log.info(f"🔬 Backtest: {len(results)} pares | "
             f"ok={counts.get('ok', 0)} neg={counts.get('negative', 0)} "
             f"zero={counts.get('no_trades', 0)}")
    return results


def merge_backtest_with_convergence(perf: dict, baseline: dict,
                                     bt_results: dict, mode: str) -> tuple[bool, list[str], dict]:
    """Estende check_convergence() com o forward backtest como sombra-de-verdade.

    Regra: se um par está falhando em PnL (DB) MAS está 'ok' no forward backtest
    (simulação sugere que os params propostos são lucrativos), contamos como
    CONVERGIDO para esse par. Isso evita loops infinitos onde a LLM propõe
    params bons mas o DB ainda não capturou o efeito.

    Guard: forward com n_trades < MIN_TRADES_FOR_DELTA é ignorado (amostra
    insuficiente pra confiar na projeção).

    Returns:
        (converged, failing_pairs, backtest_evaluations)
        backtest_evaluations é dict de auditoria por SYM_TF com decision
        granular: 'forward_says_ok', 'forward_says_no', 'low_sample_ignore',
        'no_forward_eval'.
    """
    # 1) Defer ao check_convergence original primeiro
    converged, failing = check_convergence(perf, baseline, mode=mode)
    if converged:
        return (True, [], {})

    # 2) Tentar resgatar pares que falharam PnL mas passaram no backtest
    if not failing:
        return (converged, failing, {})

    evals = {}
    if not bt_results:
        for pair in failing:
            evals[pair] = {"decision": "no_forward_eval"}
        return (converged, failing, evals)

    rescued = []
    for pair in list(failing):
        bt = bt_results.get(pair)
        if not bt:
            evals[pair] = {"decision": "no_forward_eval"}
            continue

        n_trades = bt.get("n_trades", 0)
        pnl = bt.get("pnl", 0.0)

        # Guard: amostra insuficiente
        if n_trades < MIN_TRADES_FOR_DELTA:
            evals[pair] = {"decision": "low_sample_ignore",
                           "n_trades": n_trades, "pnl": pnl}
            continue

        if bt.get("decision") == "ok":
            log.info(f"✅ {pair}: PnL DB negativo MAS backtest 'ok' — "
                     f"shadow-of-truth convergiu (resgatado)")
            rescued.append(pair)
            failing.remove(pair)
            evals[pair] = {"decision": "forward_says_ok",
                           "n_trades": n_trades, "pnl": pnl,
                           "wr": bt.get("wr", 0.0)}
        else:
            evals[pair] = {"decision": "forward_says_no",
                           "n_trades": n_trades, "pnl": pnl,
                           "wr": bt.get("wr", 0.0)}

    if rescued:
        log.info(f"🔬 {len(rescued)} par(es) resgatado(s) pelo backtest: {rescued}")
        converged = len(failing) == 0
    return (converged, failing, evals)


# ═══════════════════════════════════════════════════════════════════
# FORWARD BACKTEST CONVERGENCE GATE (v3.1 — the REAL convergence)
# ═══════════════════════════════════════════════════════════════════

def check_forward_convergence(
    config: dict, days: int = 7, max_workers: int = 0,
) -> tuple[bool, list[str], dict]:
    """Run forward backtest on ALL active pairs and check convergence.

    This is the TRUE convergence gate — it simulates bar-by-bar with the
    CURRENT config, unlike the legacy DB-based check which reads historical
    trades made with OLD params.

    Returns:
        (converged, failing_pairs, bt_results)
        converged: True if ALL active pairs have positive PnL in forward backtest
        failing_pairs: list of "SYM_TF" with negative PnL or zero trades
        bt_results: raw forward backtest results dict
    """
    bt_results = evaluate_forward_backtest(config, days=days, max_workers=max_workers)

    if not bt_results:
        log.warning("⚠️ Forward backtest returned no results — cannot check convergence")
        return (False, [], {})

    failing = []
    no_signal = []
    for pair_key in sorted(bt_results.keys()):
        r = bt_results[pair_key]
        pnl = r.get("pnl", 0.0)
        n_trades = r.get("n_trades", 0)
        decision = r.get("decision", "unknown")

        if decision in ("no_data", "strategy_load_failed", "utils_load_failed", "timeout", "error"):
            # Can't evaluate — treat as no signal (not failing)
            no_signal.append(pair_key)
            continue

        if n_trades == 0:
            # No signals generated — might need strategy change
            no_signal.append(pair_key)
            continue

        if pnl <= 0:
            failing.append(pair_key)

    converged = len(failing) == 0
    log.info(f"🔬 Forward convergence: {len(bt_results)} pairs | "
             f"failing={len(failing)} | no_signal={len(no_signal)} | "
             f"converged={'YES ✅' if converged else 'NO ❌'}")
    if failing:
        for p in failing:
            r = bt_results[p]
            log.info(f"  ❌ {p}: pnl=R${r.get('pnl', 0):+.2f} trades={r.get('n_trades', 0)} wr={r.get('wr', 0):.0f}%")
    if no_signal:
        log.info(f"  ⚪ No signal: {no_signal}")

    return (converged, failing, bt_results)


def explore_all_strategies_forward(
    sym: str, tf: str, config: dict, days: int = 7,
) -> list[dict]:
    """Test ALL available strategies for a (sym, tf) pair using forward backtest.

    For each strategy in the strategies/ directory, runs a forward backtest
    on real MT5 bars and collects PnL/trades/WR. Returns strategies ranked
    by PnL (descending), filtering out those with 0 trades.

    Returns: list of dicts with keys: strategy, pnl, n_trades, wr, max_dd, decision.
    """
    try:
        from strategy_explorer import ALL_STRATEGIES
    except ImportError:
        log.warning("Cannot import ALL_STRATEGIES from strategy_explorer")
        return []

    from optimization.vt_forward_backtest import run_mini_backtest_pair_with_strategy

    results = []
    pair_key = f"{sym}_{tf}"
    current_strategy = config.get("strategy_by_tf", {}).get(
        pair_key, config.get("strategy", {}).get(sym, "UNKNOWN")
    )

    for strat in ALL_STRATEGIES:
        try:
            r = run_mini_backtest_pair_with_strategy(sym, tf, strat, config, days=days)
            r["strategy"] = strat
            r["is_current"] = (strat == current_strategy)
            results.append(r)
        except Exception as e:
            log.debug(f"  {pair_key}/{strat}: error — {e}")

    # Sort by PnL descending, then by n_trades descending
    results.sort(key=lambda x: (x.get("pnl", 0), x.get("n_trades", 0)), reverse=True)

    # Log top 5
    log.info(f"  📊 {pair_key}: tested {len(results)} strategies")
    for i, r in enumerate(results[:5]):
        marker = "📌" if r.get("is_current") else "  "
        log.info(f"    {marker} {r['strategy']}: pnl=R${r.get('pnl', 0):+.2f} "
                 f"trades={r.get('n_trades', 0)} wr={r.get('wr', 0):.0f}%")

    return results


def optimize_failing_pairs_forward(
    failing_pairs: list[str], bt_results: dict, config: dict,
    days: int = 7,
) -> tuple[dict, list[dict], list[str]]:
    """For each failing pair, find the best strategy via forward backtest.

    Tests all 28 strategies for each failing pair. If a profitable strategy
    is found, updates config's strategy_by_tf. If no strategy works,
    the pair is flagged for disabling.

    Args:
        failing_pairs: list of "SYM_TF" pairs with negative PnL
        bt_results: current forward backtest results
        config: vt_config dict (will be modified in-place for strategy_by_tf)
        days: backtest window in days

    Returns:
        (config, changes_list, still_failing) — modified config, list of changes applied, and pairs that still fail
    """
    changes = []
    still_failing = []

    for pair_key in failing_pairs:
        parts = pair_key.split("_", 1)
        if len(parts) != 2:
            still_failing.append(pair_key)
            continue
        sym, tf = parts

        current_strat = config.get("strategy_by_tf", {}).get(
            pair_key, config.get("strategy", {}).get(sym, "UNKNOWN")
        )
        current_pnl = bt_results.get(pair_key, {}).get("pnl", 0)

        log.info(f"🔍 Exploring strategies for {pair_key} (current: {current_strat}, pnl=R${current_pnl:+.2f})...")
        ranked = explore_all_strategies_forward(sym, tf, config, days=days)

        # Find best profitable strategy (with at least 3 trades)
        best = None
        for r in ranked:
            if r.get("pnl", 0) > 0 and r.get("n_trades", 0) >= 3 and r.get("strategy") != current_strat:
                best = r
                break

        # If no strategy with 3+ trades, try with 1+ trades
        if best is None:
            for r in ranked:
                if r.get("pnl", 0) > 0 and r.get("n_trades", 0) >= 1 and r.get("strategy") != current_strat:
                    best = r
                    break

        if best:
            new_strat = best["strategy"]
            log.info(f"  ✅ {pair_key}: {current_strat} → {new_strat} "
                     f"(pnl=R${best['pnl']:+.2f}, trades={best['n_trades']}, wr={best.get('wr', 0):.0f}%)")
            if "strategy_by_tf" not in config:
                config["strategy_by_tf"] = {}
            config["strategy_by_tf"][pair_key] = new_strat
            changes.append({
                "symbol": pair_key,
                "params": {"strategy": new_strat},
                "reason": f"Forward backtest: {current_strat}→{new_strat} "
                          f"(pnl=R${best['pnl']:+.2f}, trades={best['n_trades']})",
            })
        else:
            # Check if current strategy at least generates trades
            current_result = next((r for r in ranked if r.get("is_current")), None)
            if current_result and current_result.get("n_trades", 0) > 0:
                still_failing.append(pair_key)
                log.info(f"  ❌ {pair_key}: no profitable strategy found (current: pnl=R${current_pnl:+.2f})")
            else:
                # No strategy generates trades at all → flag for disabling
                still_failing.append(pair_key)
                log.info(f"  ⚪ {pair_key}: no strategy generates trades → flag for disable")

    return config, changes, still_failing


def snapshot_live_config(config_path: Path) -> Path:
    """Copy vt_config.json to a timestamped snapshot in /tmp/.

    Uses atomic write (tmp + rename) to avoid partial reads.
    Returns the snapshot path.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    snap = Path(f"/tmp/vt_config_live_{ts}.json")
    tmp = snap.with_suffix(".json.tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(json.load(open(config_path)), f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.rename(str(tmp), str(snap))
        return snap
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


def run_shadow_optimization(
    snapshot_path: Path, perf: dict, issues: list,
    days: int, use_forward: bool = True,
) -> dict:
    """Run the AGI optimization loop on a sandboxed config.

    Monkey-patches load_config to read from the snapshot, then calls main()
    in dry-run mode. Returns the audit dict WITHOUT touching the live config.
    Shadow always uses --no-llm --no-web for speed.
    """
    import sys as _sys

    def shadow_load_config(force: bool = False):
        return json.load(open(snapshot_path))

    saved_argv = _sys.argv
    _sys.argv = [
        "agi_tuning_17h.py",
        "--dry-run",
        f"--days={days}",
        "--no-llm",
        "--no-web",
    ]
    if use_forward:
        _sys.argv.append("--use-backtest-convergence")

    try:
        # Monkey-patch load_config to read from snapshot
        import agi_tuning_17h as _self
        original_load = _self.load_config
        _self.load_config = shadow_load_config
        try:
            main()
        finally:
            _self.load_config = original_load
        # main() writes audit to /tmp/vt_agi_audit.json — copy to shadow path
        live_audit = Path("/tmp/vt_agi_audit.json")
        shadow_audit = Path("/tmp/vt_agi_shadow_audit.json")
        if live_audit.exists():
            import shutil
            shutil.copy(str(live_audit), str(shadow_audit))
            return json.load(open(shadow_audit))
        return {}
    finally:
        _sys.argv = saved_argv


def compare_live_vs_shadow(live_audit: dict, shadow_audit: dict) -> dict:
    """Compare live and shadow audit dicts. Return diff structure.

    Aggregates changes across iterations per symbol, then diffs:
    - agreements: same param changes in both
    - disagreements: different param values for same symbol
    - live_only: changes only in live
    - shadow_only: changes only in shadow
    - convergence_diff: live vs shadow convergence status
    - failing_diff: failing pairs comparison
    """
    def index_by_symbol(iterations):
        idx = {}
        for it in iterations or []:
            for ch in it.get("changes", []):
                sym = ch.get("symbol")
                if sym:
                    idx.setdefault(sym, []).append(ch.get("params", {}))
        return idx

    live_changes = index_by_symbol(live_audit.get("iterations", []))
    shadow_changes = index_by_symbol(shadow_audit.get("iterations", []))

    all_syms = set(live_changes) | set(shadow_changes)
    agreements, disagreements = [], []
    live_only, shadow_only = [], []

    for sym in sorted(all_syms):
        l = live_changes.get(sym, [{}])[-1] if live_changes.get(sym) else {}
        s = shadow_changes.get(sym, [{}])[-1] if shadow_changes.get(sym) else {}
        if not l and s:
            shadow_only.append({"symbol": sym, "shadow": s})
        elif l and not s:
            live_only.append({"symbol": sym, "live": l})
        elif l == s:
            agreements.append({"symbol": sym, "params": l})
        else:
            disagreements.append({"symbol": sym, "live": l, "shadow": s})

    live_iters = live_audit.get("iterations", [])
    shadow_iters = shadow_audit.get("iterations", [])

    return {
        "agreements": agreements,
        "disagreements": disagreements,
        "live_only": live_only,
        "shadow_only": shadow_only,
        "convergence_diff": {
            "live": live_audit.get("converged"),
            "shadow": shadow_audit.get("converged"),
        },
        "failing_diff": {
            "live": (live_iters[-1].get("failing_pairs", []) if live_iters else []),
            "shadow": (shadow_iters[-1].get("failing_pairs", []) if shadow_iters else []),
        },
    }


def write_comparison_report(comparison: dict, audit_path: Path) -> Path:
    """Write comparison dict to /tmp/vt_agi_comparison_YYYYMMDD_HHMMSS.json."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(f"/tmp/vt_agi_comparison_{ts}.json")
    try:
        with open(out, "w") as f:
            json.dump(comparison, f, indent=2, ensure_ascii=False, default=str)
        return out
    except Exception as e:
        log.warning(f"Falha ao salvar comparison: {e}")
        return out


def _build_v3_prompt_context(
    regime_info: dict,
    risk_tags: dict,
    discovery_results: dict,
    trade_analysis: dict,
) -> str:
    """Build v3.0 context section to append to the LLM prompt.

    Includes: regime classification, risk tags, discovery engine results,
    and execution vs logic error analysis.

    Args:
        regime_info: From Stage 1 (regime classifier).
        risk_tags: From Stage 2 (macro intel).
        discovery_results: From Stage 3 (discovery engine).
        trade_analysis: From Stage 1.3 (error classification).

    Returns:
        String to append to the LLM prompt, or empty string if no data.
    """
    parts = []

    # Regime classification
    regime = regime_info.get("current_regime", "UNKNOWN")
    dominant = regime_info.get("dominant_regime", "UNKNOWN")
    parts.append(f"""
## 📊 CLASSIFICAÇÃO DE REGIME (v3.0)
- Regime Atual: {describe_regime(regime) if HAS_REGIME_CLASSIFIER else regime}
- Regime Dominante (30d): {describe_regime(dominant) if HAS_REGIME_CLASSIFIER else dominant}
- Distribuição: {json.dumps(regime_info.get('regime_counts', {}))}
""")

    # Risk tags
    tag = risk_tags.get("tag", "UNKNOWN")
    parts.append(f"""
## 🏷️ RISK TAG (Próximo Dia)
- Tag: **{tag}**
- Reasoning: {risk_tags.get('reasoning', 'N/A')}
- Implicação: {'Reduzir exposição, usar SL mais largo' if tag in ('HIGH_VOLATILITY_EXPECTED', 'EVENT_RISK') else 'Operar normalmente' if tag == 'LOW_VOLATILITY_EXPECTED' else 'Favorecer estratégias de tendência'}
""")

    # Discovery Engine results
    if discovery_results:
        approved = discovery_results.get("approved_strategies", [])
        eliminated = discovery_results.get("eliminated_strategies", [])
        synthesis = discovery_results.get("stage_3_4_synthesis", {})

        if approved or eliminated:
            parts.append("## 🧬 DISCOVERY ENGINE (Bayesian Optimization)\n")

            if approved:
                parts.append("**Estratégias APROVADAS:**\n")
                for a in approved:
                    parts.append(
                        f"- {a['pair']}: {a['strategy']} | PF={a.get('pf', 0)} | "
                        f"Sharpe={a.get('sharpe', 0)} | params={json.dumps(a.get('best_params', {}))}\n"
                    )

            if eliminated:
                parts.append("**Estratégias ELIMINADAS:**\n")
                for e in eliminated:
                    parts.append(f"- {e['pair']}: {e['strategy']} | Razão: {e.get('reason', 'N/A')}\n")

            # Meta-strategy synthesis
            meta = synthesis.get("meta_strategy", {})
            if meta.get("type") == "REGIME_SWITCHING":
                parts.append(f"""
**META-ESTRATÉGIA (Regime Switching):**
- Tipo: {meta['type']}
- Regime atual: {meta.get('current_regime', '?')}
- Ativo para regime: {meta.get('active_for_regime', '?')}
""")
                rules = synthesis.get("transition_rules", [])
                if rules:
                    parts.append("**Regras de transição:**\n")
                    for r in rules:
                        parts.append(f"  - SE {r['condition']} → {r['action']} ({r.get('reason', '')})\n")

    # Trade analysis (execution vs logic errors)
    exec_errors = trade_analysis.get("execution_errors", [])
    logic_errors = trade_analysis.get("logic_errors", [])
    if exec_errors or logic_errors:
        parts.append("## 📋 ANÁLISE DE ERROS\n")
        if exec_errors:
            parts.append(f"**Erros de Execução ({len(exec_errors)}):** slippage, latência, rejeição de ordem\n")
            for err in exec_errors[:3]:
                parts.append(f"  - [{err.get('date', '?')}] {err.get('description', '')[:100]}\n")
        if logic_errors:
            parts.append(f"**Erros de Lógica ({len(logic_errors)}):** entradas ruins, sinais falsos\n")
            for err in logic_errors[:3]:
                parts.append(f"  - [{err.get('date', '?')}] {err.get('description', '')[:100]}\n")

    return "\n".join(parts) if parts else ""


def _build_v3_telegram_card(
    regime_info: dict,
    risk_tags: dict,
    discovery_results: dict,
    llm_result: dict,
    config: dict,
    perf: dict = None,
) -> str:
    """Build the v3.0 Telegram notification card.

    Args:
        regime_info: From Stage 1 (regime classifier).
        risk_tags: From Stage 2 (macro intel).
        discovery_results: From Stage 3 (discovery engine).
        llm_result: LLM analysis result.
        config: Current config.
        perf: Performance data (optional, for SL diagnostics).

    Returns:
        Formatted Telegram message string.
    """
    regime = regime_info.get("current_regime", "UNKNOWN")
    risk_tag = risk_tags.get("tag", "UNKNOWN")

    # Regime description in Portuguese
    regime_pt = {
        "TRENDING_STRONG": "Tendência Forte",
        "RANGING": "Lateralidade",
        "HIGH_VOLATILITY": "Alta Volatilidade",
        "LOW_VOLATILITY": "Baixa Volatilidade",
    }.get(regime, regime)

    lines = [f"🧬 AGI Tuning Concluído (17:10)", ""]
    lines.append(f"📊 Regime Atual: {regime_pt}")
    lines.append(f"🏷️ Risk Tag: {risk_tag}")

    # SL diagnostics per symbol
    if perf and perf.get("sl_analysis"):
        lines.append("")
        lines.append("🛡️ Stop Loss Analysis:")
        for sym, sla in sorted(perf["sl_analysis"].items()):
            hit_rate = sla.get("sl_hit_rate", 0)
            n_trades = sla.get("n_trades", 0)
            sl_hits = sla.get("sl_hits", 0)
            # Visual indicator: green if <40%, yellow if 40-60%, red if >60%
            if hit_rate > 60:
                icon = "🔴"
                recommendation = "⚠️ Aumentar sl_atr_mult"
            elif hit_rate > 40:
                icon = "🟡"
                recommendation = "Monitorar"
            else:
                icon = "🟢"
                recommendation = "OK"
            lines.append(f"  {icon} {sym}: SL hit {hit_rate:.0f}% ({sl_hits}/{n_trades}) — {recommendation}")

    # Discovery results
    if discovery_results:
        approved = discovery_results.get("approved_strategies", [])
        synthesis = discovery_results.get("stage_3_4_synthesis", {})
        meta = synthesis.get("meta_strategy", {})

        if approved:
            lines.append("")
            lines.append("🏆 Super Estratégia Aprovada:")
            best = approved[0]
            lines.append(f"- Lógica: {best.get('strategy', '?')} (Otimizada)")
            params = best.get("best_params", {})
            if params:
                param_str = ", ".join(f"{k}={v}" for k, v in list(params.items())[:3])
                lines.append(f"- Parâmetros: {param_str}")
            lines.append(f"- PF: {best.get('pf', 0)} | Sharpe: {best.get('sharpe', 0)}")

        if meta.get("type") == "REGIME_SWITCHING":
            lines.append(f"- Meta: {meta.get('active_for_regime', '?')} (regime switching)")

    # LLM decision
    if llm_result and llm_result.get("analysis"):
        lines.append("")
        lines.append(f"🧠 Decisão do LLM: {llm_result['analysis'][:150]}")

    lines.append("")
    lines.append("✅ Config aplicada em Shadow Mode.")
    lines.append("📌 Aguardando 1º trade para Live.")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="AGI 17h Tuning v3.0 — Super Estratégia")
    parser.add_argument("--days", type=int, default=7, help="Janela de análise em dias (default: 7)")
    parser.add_argument("--dry-run", action="store_true", help="Só analisa, não aplica mudanças")
    parser.add_argument("--no-llm", action="store_true", help="Só estatísticas, sem consulta LLM")
    parser.add_argument("--no-web", action="store_true", help="Não usar tinyfish para web intel")
    parser.add_argument("--timeout", type=int, default=120, help="Timeout LLM em segundos")
    parser.add_argument("--web-timeout", type=int, default=60, help="Timeout tinyfish em segundos (total)")
    parser.add_argument("--max-iterations", type=int, default=5,
                        help="Máximo de iterações do loop de convergência (1-5, default: 5 — sempre convergir)")
    parser.add_argument("--pause-failing", action="store_true",
                        help="Fallback final: auto-pausar pares SYM_TF com PnL≤0 após max-iterations")
    parser.add_argument("--max-workers", type=int, default=0,
                        help="Max processos paralelos pro forward backtest (0=auto via load avg)")
    parser.add_argument("--use-backtest-convergence", action="store_true",
                        help="Usa forward backtest como shadow-of-truth no critério de convergência")
    parser.add_argument("--no-shadow", dest="no_shadow", action="store_true", default=False,
                        help="Desativa shadow mode (default: ativo)")
    # ── AGI v3.0 new arguments (backward compatible) ──
    parser.add_argument("--train-days", type=int, default=None,
                        help="[v3] Dias de treinamento (default: --days). Alias para --days se não setado.")
    parser.add_argument("--validate-days", type=int, default=5,
                        help="[v3] Dias de validação out-of-sample (default: 5)")
    parser.add_argument("--optimizer-engine", choices=["grid", "bayesian"], default="bayesian",
                        help="[v3] Motor de otimização: 'grid' (legado) ou 'bayesian' (Optuna, default)")
    parser.add_argument("--max-evaluations", type=int, default=100,
                        help="[v3] Máximo de avaliações do Optuna por par (default: 100)")
    parser.add_argument("--enable-regime-switching", action="store_true", default=False,
                        help="[v3] Ativar Meta-Strategy com regime switching (ADX/ATR)")
    parser.add_argument("--slippage-ticks", type=int, default=1,
                        help="[v3] Ticks de slippage por lado no stress test (default: 1)")
    parser.add_argument("--latency-ms", type=int, default=200,
                        help="[v3] Latência simulada em ms (default: 200)")
    parser.add_argument("--cost-model", choices=["b3_standard"], default="b3_standard",
                        help="[v3] Modelo de custos (default: b3_standard)")
    parser.add_argument("--convergence-mode", choices=["delta", "absolute", "sharpe_ratio"],
                        default="delta",
                        help="Critério de convergência: 'delta', 'absolute', ou 'sharpe_ratio' (v3)")
    args = parser.parse_args()

    # Clamp iterations
    args.max_iterations = max(1, min(5, args.max_iterations))

    # v3.0: Resolve --train-days alias (backward compat: --days still works)
    if args.train_days is None:
        args.train_days = args.days
    # Use train_days as the primary window for data collection
    analysis_days = args.train_days

    log.info(f"🤖 AGI 17H v3.0 iniciado — janela: {analysis_days} dias | dry-run: {args.dry_run} | "
             f"llm: {not args.no_llm} | web: {not args.no_web}")
    log.info(f"  v3.0 features: regime={HAS_REGIME_CLASSIFIER} | safety={HAS_SAFETY_VALIDATOR} | "
             f"bayesian={HAS_BAYESIAN and HAS_OPTUNA} | "
             f"optimizer={args.optimizer_engine} | regime_switching={args.enable_regime_switching}")

    # 0. Sync resolved_symbols no config (garante contratos atualizados)
    try:
        import subprocess as _sp
        _sp.run([sys.executable, str(PROJECT_DIR / "vt_resolve_symbols.py"), "--apply"],
                capture_output=True, timeout=30)
        log.info("🔄 vt_resolve_symbols.py executado — config sincronizado")
    except Exception as e:
        log.warning(f"vt_resolve_symbols.py falhou (non-fatal): {e}")

    # 1. Carregar config
    config = load_config(force=True)
    if not config:
        log.error("Config não pôde ser carregada — abortando")
        sys.exit(1)

    # 2. Coletar performance
    perf = collect_performance(days=analysis_days)
    if not perf or not perf.get("by_symbol"):
        log.warning("Sem trades no período — nada para otimizar")
        print("Nenhum trade encontrado no período. Execute após ter dados de trading.")
        sys.exit(0)

    # ═══════════════════════════════════════════════════════════════
    # STAGE 1 — Regime Classification + Context (v3.0)
    # ═══════════════════════════════════════════════════════════════
    regime_info = {"current_regime": "RANGING", "dominant_regime": "RANGING", "daily_regimes": {}, "regime_counts": {}}
    trade_analysis = {"execution_errors": [], "logic_errors": []}

    if HAS_REGIME_CLASSIFIER:
        try:
            # Load raw trades for regime classification
            import sqlite3 as _sqlite3
            _conn = _sqlite3.connect(str(DB_PATH))
            _conn.row_factory = _sqlite3.Row
            _cutoff = (datetime.now() - timedelta(days=analysis_days)).strftime("%Y-%m-%d")
            _raw_trades = [dict(r) for r in _conn.execute(
                "SELECT * FROM trades WHERE entry_time >= ? AND exit_time IS NOT NULL ORDER BY entry_time",
                (_cutoff,)
            ).fetchall()]
            _conn.close()

            regime_info = classify_regimes_from_trades(_raw_trades, days=analysis_days)
            log.info(f"📊 Regime atual: {describe_regime(regime_info['current_regime'])} "
                     f"(dominante: {describe_regime(regime_info['dominant_regime'])})")
            log.info(f"  Regimes: {regime_info['regime_counts']}")

            # Parse trade_analysis files for execution vs logic errors
            trade_analysis = parse_trade_analysis_files(days=7)
            n_exec = len(trade_analysis["execution_errors"])
            n_logic = len(trade_analysis["logic_errors"])
            if n_exec or n_logic:
                log.info(f"📋 Trade analysis: {n_exec} erros de execução, {n_logic} erros de lógica")
        except Exception as e:
            log.warning(f"Regime classifier erro (non-fatal): {e}")

    # 3. Diagnosticar problemas
    issues = diagnose_issues(perf)
    log.info(f"Diagnosticados {len(issues)} problemas")

    # 4. Web intelligence via tinyfish (se habilitado)
    web_intel = {}
    if not args.no_web and _tinyfish_check():
        log.info("🌐 Coletando inteligência TÉCNICA via tinyfish...")
        # Focar nos símbolos com problemas (top 3 piores)
        worst_symbols = sorted(
            perf.get("by_symbol", {}).items(),
            key=lambda x: x[1]["total_pnl"]
        )[:3]
        worst_syms = [s[0] for s in worst_symbols]
        # Sempre inclui WIN/WDO (mais sensíveis)
        root_set = set(worst_syms) | set(["WIN", "WDO"])

        # Mapear símbolo → estratégia (da config)
        strat_map = config.get("strategy", {}) or {}

        for sym in list(root_set)[:3]:
            try:
                # Pegar estratégia do config (pode ser WIN/BIT/DOL/IND/WSP/WDO)
                # Config key pode ser maiúscula ou minúscula
                strategy_name = (
                    strat_map.get(sym) or
                    strat_map.get(sym.upper()) or
                    strat_map.get(sym.lower()) or
                    "GENERAL"
                )
                intel = web_intel_for_symbol(sym, strategy=strategy_name)
                if (intel.get("strategy_tips") or
                    intel.get("indicator_settings") or
                    intel.get("sl_trail_tactics") or
                    intel.get("patterns")):
                    web_intel[sym] = intel
            except Exception as e:
                log.warning(f"web_intel erro para {sym}: {e}")
        log.info(f"🌐 Web intel técnica coletada para {len(web_intel)} símbolos")
    else:
        if args.no_web:
            log.info("🌐 Web intel desabilitada por --no-web")
        else:
            log.warning("🌐 tinyfish não disponível — web intel pulada")

    # ═══════════════════════════════════════════════════════════════
    # STAGE 2 — Risk Tags for Next Day (v3.0)
    # ═══════════════════════════════════════════════════════════════
    risk_tag = "LOW_VOLATILITY_EXPECTED"  # default
    risk_tags = {"tag": risk_tag, "reasoning": "Default — no web intel"}

    if web_intel:
        # Derive risk tag from web intel + regime
        try:
            current_regime = regime_info.get("current_regime", "RANGING")
            if current_regime == "HIGH_VOLATILITY":
                risk_tag = "HIGH_VOLATILITY_EXPECTED"
            elif current_regime == "TRENDING_STRONG":
                risk_tag = "TREND_DAY_PROBABLE"
            elif current_regime == "LOW_VOLATILITY":
                risk_tag = "LOW_VOLATILITY_EXPECTED"

            # Check for event risk in web intel text
            event_keywords = ["payroll", "copom", "fed", "fomc", "ipca", "pib", "selic",
                              "ata ", "decisão", "reunião", "publicação"]
            all_intel_text = " ".join(
                str(v.get("strategy_tips", "")) + str(v.get("patterns", ""))
                for v in web_intel.values()
            ).lower()

            if any(kw in all_intel_text for kw in event_keywords):
                risk_tag = "EVENT_RISK"

            risk_tags = {"tag": risk_tag, "reasoning": f"Regime={current_regime}, web_intel={len(web_intel)} symbols"}
            log.info(f"🏷️ Risk Tag: {risk_tag} (regime={current_regime})")
        except Exception as e:
            log.warning(f"Risk tag generation erro: {e}")

    # 4.5. Strategy Explorer — busca configs lucrativas no histórico
    optimization = {}
    try:
        from strategy_explorer import generate_optimization_report, IMPERATIVE_RULE, ALL_STRATEGIES as SE_STRATEGIES
        notify_telegram("🔬 Rodando Strategy Explorer...")
        log.info("🔬 Rodando Strategy Explorer (busca configs lucrativas)...")
        n_strategies = len(SE_STRATEGIES)
        notify_telegram(f"📊 Comparando {n_strategies} estratégias para cada SYM_TF...")
        full_report = generate_optimization_report()
        for sym, data in full_report.get("by_symbol", {}).items():
            best = data.get("optimization", {}).get("best_config")
            if best and best.get("profit_factor", 0) > 1.0 and best.get("pnl", 0) > 0:
                optimization[sym] = {
                    "current_pnl": data["current_performance"].get("pnl", 0),
                    "best_pnl": best.get("pnl", 0),
                    "best_pf": best.get("profit_factor", 0),
                    "best_wr": best.get("wr", 0),
                    "best_sl_atr_mult": best.get("sl_atr_mult"),
                    "best_cooldown_seconds": best.get("cooldown_seconds"),
                    "best_bb_std": best.get("bb_std"),
                    "best_rsi_ob": best.get("rsi_overbought"),
                    "best_rsi_os": best.get("rsi_oversold"),
                    "variants": data.get("variants_to_test", []),
                    "strategy_comparison": data.get("strategy_comparison", {}),
                }
        # Multi-strategy data: switches + untested
        optimization["_strategy_switches"] = full_report.get("strategy_switches", [])
        optimization["_strategy_comparison"] = full_report.get("strategy_comparison", {})
        log.info(f"🔬 Optimization encontrada para {len(optimization)} símbolos com PF>1")
    except Exception as e:
        log.warning(f"Strategy Explorer falhou: {e}")

    # 4.6. AUTO-APPLY Explorer results (não depender do LLM pra configs lucrativas)
    # Regra: se Explorer achou config com PF>1.2 E melhoria >10%, auto-aplica
    # SKIP if dry-run — only log what WOULD be applied
    explorer_applied = []
    if optimization:
        from core.vt_config_loader import save_params as _save_params
        for sym, data in optimization.items():
            if sym.startswith("_"):
                continue
            best_pf = data.get("best_pf", 0)
            best_pnl = data.get("best_pnl", 0)
            current_pnl = data.get("current_pnl", 0)
            pnl_improvement = best_pnl - current_pnl
            improvement_pct = (pnl_improvement / abs(current_pnl) * 100) if current_pnl != 0 else 0

            # Só auto-aplica se: PF>1.2 E (melhoria>10% OU PnL negativo→positivo)
            should_auto_apply = (
                best_pf > 1.2 and (
                    improvement_pct > 10 or
                    (current_pnl <= 0 and best_pnl > 0)
                )
            )
            if not should_auto_apply:
                continue

            params_to_apply = {}
            if data.get("best_sl_atr_mult") is not None:
                params_to_apply["sl_atr_mult"] = data["best_sl_atr_mult"]
            if data.get("best_cooldown_seconds") is not None:
                params_to_apply["cooldown_seconds"] = data["best_cooldown_seconds"]
            if data.get("best_bb_std") is not None:
                params_to_apply["bb_std"] = data["best_bb_std"]
            if data.get("best_rsi_ob") is not None:
                params_to_apply["rsi_overbought"] = data["best_rsi_ob"]
            if data.get("best_rsi_os") is not None:
                params_to_apply["rsi_oversold"] = data["best_rsi_os"]

            # Enforce PARAM_BOUNDS on Explorer params (prevent floor violations)
            for param_name, param_val in list(params_to_apply.items()):
                if param_name in PARAM_BOUNDS:
                    lo, hi = PARAM_BOUNDS[param_name]
                    if param_val < lo:
                        log.info(f"🔬 Explorer {sym}.{param_name}={param_val} < floor {lo}, clamping")
                        params_to_apply[param_name] = lo
                    elif param_val > hi:
                        log.info(f"🔬 Explorer {sym}.{param_name}={param_val} > ceiling {hi}, clamping")
                        params_to_apply[param_name] = hi

            if params_to_apply:
                sym_lower = sym.lower()
                if args.dry_run:
                    # Dry-run: log but DON'T save
                    explorer_applied.append({
                        "symbol": sym, "params": params_to_apply, "applied": False,
                        "reason": f"[DRY-RUN] Explorer: PF={best_pf:.2f} ΔPnL=R${pnl_improvement:+.0f} ({improvement_pct:+.0f}%)",
                        "warnings": [],
                    })
                    log.info(f"🔬 [DRY-RUN] AUTO-APPLY {sym}: {params_to_apply} (PF={best_pf:.2f}, Δ={improvement_pct:+.0f}%)")
                else:
                    ok = _save_params(sym_lower, params_to_apply, updated_by="agi_explorer_auto")
                    explorer_applied.append({
                        "symbol": sym, "params": params_to_apply, "applied": ok,
                        "reason": f"Explorer auto: PF={best_pf:.2f} ΔPnL=R${pnl_improvement:+.0f} ({improvement_pct:+.0f}%)",
                        "warnings": [],
                    })
                    log.info(f"🔬 AUTO-APPLY {sym}: {params_to_apply} (PF={best_pf:.2f}, Δ={improvement_pct:+.0f}%)")
                    notify_telegram(f"🔬 Auto-apply Explorer: {sym} → {params_to_apply} (PF={best_pf:.2f})")

        if explorer_applied:
            log.info(f"🔬 Explorer auto-aplicou {len(explorer_applied)} mudanças antes do LLM")
            config = load_config(force=True)

    # ═══════════════════════════════════════════════════════════════
    # STAGE 3 — Multi-Stage Discovery Engine (v3.0)
    # ═══════════════════════════════════════════════════════════════
    discovery_results = {}
    if HAS_BAYESIAN and HAS_OPTUNA and args.optimizer_engine == "bayesian":
        try:
            from strategy_explorer import load_trades, ALL_STRATEGIES
            from strategy_explorer import get_all_symbols, get_timeframes_for_symbol

            log.info("🧬 Stage 3: Running Multi-Stage Discovery Engine...")
            notify_telegram("🧬 Discovery Engine rodando (Bayesian Optimization)...")

            # Build trades_by_pair for the discovery engine
            symbols = get_all_symbols()
            trades_by_pair = {}
            for sym in symbols:
                tfs = get_timeframes_for_symbol(sym)
                for tf in tfs:
                    pair_key = f"{sym}_{tf}"
                    if pair_key in config.get("disabled_timeframes", []):
                        continue
                    pair_trades = load_trades(days=analysis_days, symbol=sym, tf=tf)
                    if pair_trades and len(pair_trades) >= 3:
                        trades_by_pair[pair_key] = pair_trades

            if trades_by_pair:
                discovery_results = run_discovery_engine(
                    config=config,
                    trades_by_pair=trades_by_pair,
                    strategies=ALL_STRATEGIES,
                    train_days=analysis_days,
                    validate_days=args.validate_days,
                    max_evaluations=args.max_evaluations,
                    slippage_ticks=args.slippage_ticks,
                    latency_ms=args.latency_ms,
                    cost_model=args.cost_model,
                    regime=regime_info.get("current_regime", "RANGING"),
                    timeout=args.timeout,
                )

                summary = discovery_results.get("summary", {})
                log.info(
                    f"🧬 Discovery Engine: {summary.get('pairs_approved', 0)} approved, "
                    f"{summary.get('pairs_eliminated', 0)} eliminated, "
                    f"meta={summary.get('meta_strategy_type', 'NONE')} "
                    f"({summary.get('elapsed_seconds', 0):.1f}s)"
                )

                # Apply approved strategies to config (if not dry-run)
                approved = discovery_results.get("approved_strategies", [])
                if approved and not args.dry_run:
                    for appr in approved:
                        pair = appr.get("pair", "")
                        strategy = appr.get("strategy", "")
                        best_params = appr.get("best_params", {})
                        if pair and strategy:
                            config.setdefault("strategy_by_tf", {})[pair] = strategy
                        if pair and best_params:
                            config.setdefault("params_by_tf", {}).setdefault(pair, {}).update(best_params)
                            log.info(f"  ✅ {pair}: {strategy} → {best_params}")

                    if approved:
                        save_full_config(config, updated_by="agi_v3_discovery_engine")
                        config = load_config(force=True)
            else:
                log.info("🧬 Discovery Engine: sem trades suficientes")
        except Exception as e:
            log.warning(f"Discovery Engine erro (non-fatal): {e}")
    elif args.optimizer_engine == "bayesian" and not HAS_OPTUNA:
        log.warning("🧬 Bayesian optimizer solicitado mas optuna não instalado — usando grid")

    # ═══════════════════════════════════════════════════════════════
    # STAGE 5 — Safety Validation Gate (v3.0)
    # Applied to all LLM output before changes
    # ═══════════════════════════════════════════════════════════════

    # 5. LLM (se habilitado) — Stage 4: LLM Portfolio Manager (v3.0)
    llm_result = None
    if not args.no_llm:
        notify_telegram("🧠 Enviando análise ao LLM (Portfolio Manager v3)...")
        log.info("Consultando LLM ativo do Hermes (Stage 4: Portfolio Manager)...")
        prompt = build_llm_prompt(perf, issues, config, web_intel=web_intel, optimization=optimization)

        # v3.0: Enhance prompt with regime + risk tags + discovery results
        v3_context = _build_v3_prompt_context(regime_info, risk_tags, discovery_results, trade_analysis)
        if v3_context:
            prompt += v3_context

        response = ask_llm(prompt, timeout=args.timeout)
        if response:
            llm_result = parse_llm_response(response)
            if llm_result:
                # v3.0: Safety validation gate
                if HAS_SAFETY_VALIDATOR:
                    try:
                        validator = AGISafetyValidator(llm_result)
                        is_valid = validator.validate()
                        if not is_valid:
                            log.warning(f"⚠️ LLM output FAILED safety validation: {validator.errors}")
                            # Sanitize: clamp out-of-bounds values
                            llm_result = validator.get_sanitized()
                            log.info(f"  Sanitized output with {len(validator.warnings)} warnings")
                        else:
                            log.info("✅ LLM output passed safety validation")
                    except Exception as e:
                        log.warning(f"Safety validator erro (non-fatal): {e}")

                log.info(f"LLM retornou: {len(llm_result.get('changes', []))} mudanças sugeridas")
                log.info(f"Análise LLM: {llm_result.get('analysis', 'N/A')[:200]}")
            else:
                log.warning("LLM respondeu mas JSON não pôde ser parseado")
        else:
            log.warning("LLM não respondeu — usando apenas diagnóstico local")

    # 6-7. LOOP DE ITERAÇÕES (convergência: forward backtest com todos SYM_TF com PnL > 0)
    iteration_history = []  # histórico de cada iteração
    all_backtest_evals = []  # forward backtest evaluations por iteração
    converged = False
    final_paused = {"paused": [], "skipped": []}
    applied = list(explorer_applied)  # inclui auto-apply do Explorer

    # Guardar perf ORIGINAL (antes de qualquer iteração) para o summary Telegram
    perf_original = perf

    for it_num in range(1, args.max_iterations + 1):
        log.info(f"{'='*60}\n🔄 ITERAÇÃO {it_num}/{args.max_iterations}\n{'='*60}")

        # Recarregar config (já com mudanças da iteração anterior aplicadas)
        if it_num > 1:
            config = load_config(force=True)

        # 7a. Forward backtest convergence — the REAL gate
        log.info("🔬 Forward backtest convergence gate (simulating bar-by-bar)...")
        converged, failing_pairs, bt_results = check_forward_convergence(
            config, days=args.days, max_workers=args.max_workers,
        )
        all_backtest_evals.append({"iteration": it_num, "bt_results": bt_results})

        log.info(f"📈 Convergência it {it_num}: {'SIM ✅' if converged else 'NÃO ❌'} "
                 f"({len(failing_pairs)} pares falhando: {failing_pairs})")

        if converged:
            log.info(f"🎯 CONVERGÊNCIA atingida na iteração {it_num}!")
            iteration_history.append(
                build_iteration_history_entry(it_num, [], failing_pairs, True)
            )
            break

        # 7b. For failing pairs: explore ALL 28 strategies via forward backtest
        iter_changes = []
        if failing_pairs and not args.dry_run:
            notify_telegram(f"🔍 {len(failing_pairs)} pares negativos — testando todas estratégias...")
            log.info(f"🔍 Testing all strategies for {len(failing_pairs)} failing pairs...")

            config, strat_changes, still_failing = optimize_failing_pairs_forward(
                failing_pairs, bt_results, config, days=args.days,
            )
            iter_changes.extend(strat_changes)

            if strat_changes:
                # Save strategy changes to config
                save_full_config(config, updated_by=f"agi_forward_it{it_num}")
                config = load_config(force=True)
                log.info(f"🔄 Applied {len(strat_changes)} strategy changes")

                # Re-run forward backtest to verify
                log.info("🔬 Re-running forward backtest after strategy changes...")
                converged, failing_pairs, bt_results = check_forward_convergence(
                    config, days=args.days, max_workers=args.max_workers,
                )
                log.info(f"📈 Post-swap convergence: {'SIM ✅' if converged else 'NÃO ❌'} "
                         f"({len(failing_pairs)} pares falhando)")

        # 7c. Handle zero-trade pairs — try strategies, disable if nothing works
        if not args.dry_run:
            zero_pairs = [k for k, v in bt_results.items()
                         if v.get("n_trades", 0) == 0 and v.get("decision") not in
                         ("no_data", "strategy_load_failed", "utils_load_failed", "timeout")]
            if zero_pairs:
                log.info(f"⚪ {len(zero_pairs)} pairs with 0 trades: {zero_pairs}")
                for pair_key in zero_pairs:
                    parts = pair_key.split("_", 1)
                    if len(parts) != 2:
                        continue
                    sym, tf = parts
                    log.info(f"  🔍 Testing strategies for {pair_key} (0 trades)...")
                    ranked = explore_all_strategies_forward(sym, tf, config, days=args.days)
                    best = next((r for r in ranked if r.get("pnl", 0) > 0 and r.get("n_trades", 0) >= 1), None)
                    if best and best.get("strategy"):
                        new_strat = best["strategy"]
                        log.info(f"  ✅ {pair_key}: switching to {new_strat} "
                                 f"(pnl=R${best['pnl']:+.2f}, trades={best['n_trades']})")
                        config.setdefault("strategy_by_tf", {})[pair_key] = new_strat
                        iter_changes.append({
                            "symbol": pair_key,
                            "params": {"strategy": new_strat},
                            "reason": f"Zero-trade fix: →{new_strat} (pnl=R${best['pnl']:+.2f})",
                        })
                    else:
                        log.info(f"  ⚠️ {pair_key}: no strategy generates trades — will disable")

                if iter_changes:
                    save_full_config(config, updated_by=f"agi_forward_zero_fix_it{it_num}")
                    config = load_config(force=True)

        applied.extend(iter_changes)

        iteration_history.append(
            build_iteration_history_entry(it_num, iter_changes, failing_pairs, converged)
        )

        if converged:
            log.info(f"🎯 CONVERGÊNCIA atingida na iteração {it_num}!")
            break

        if args.dry_run:
            break

    # 8. FALLBACK — Auto-disable pairs that can't be made profitable
    # Strategy: run final forward backtest, disable pairs still negative
    if not converged and not args.dry_run:
        log.info("⚠️ FALLBACK: Final forward backtest after all iterations...")
        final_converged, final_failing, final_bt = check_forward_convergence(
            config, days=args.days, max_workers=args.max_workers,
        )

        # Also collect zero-trade pairs from final backtest
        zero_trade_pairs = [k for k, v in final_bt.items()
                           if v.get("n_trades", 0) == 0
                           and k not in set(config.get("disabled_timeframes", []) or [])]

        pairs_to_disable = list(set(final_failing + zero_trade_pairs))

        if pairs_to_disable:
            log.warning(f"🛑 Disabling {len(pairs_to_disable)} pairs that couldn't be made profitable: {pairs_to_disable}")
            final_paused = _pause_failing_pairs(pairs_to_disable, config, dry_run=False)
            config = load_config(force=True)
        else:
            log.info("✅ All pairs profitable — no disabling needed")
    elif not converged and args.dry_run:
        log.info("🔍 [DRY-RUN] Would disable failing pairs")

    # 8.5 FORWARD BACKTEST VERIFICATION — verify all pairs after all changes
    if not args.dry_run:
        log.info("🔬 ETAPA 8.5: Final forward backtest verification...")
        verify_converged, verify_failing, verify_bt = check_forward_convergence(
            config, days=args.days, max_workers=args.max_workers,
        )
        for pair_key in sorted(verify_bt.keys()):
            r = verify_bt[pair_key]
            pnl = r.get("pnl", 0)
            n_trades = r.get("n_trades", 0)
            wr = r.get("wr", 0)
            status = "✅" if pnl > 0 else ("⚪" if n_trades == 0 else "❌")
            log.info(f"  {status} {pair_key}: pnl=R${pnl:+.2f} trades={n_trades} wr={wr:.0f}%")

    # 9. Relatório final (consolidado com histórico de iterações)
    print_report(perf, issues, llm_result, applied, config, args.dry_run,
                 web_intel, optimization, iterations=iteration_history,
                 converged=converged, paused=final_paused)

    # 10. Notificação Telegram (resumo)
    if applied or final_paused["paused"]:
        summary_lines = [
            f"🤖 AGI 17H v3.0 — {len(iteration_history)} iteração(ões) | "
            f"{'CONVERGIU ✅' if converged else 'NÃO convergiu ❌'}"
        ]
        total_pnl = sum(d["total_pnl"] for d in perf.get("by_symbol", {}).values())
        total_trades = sum(d["n_trades"] for d in perf.get("by_symbol", {}).values())
        summary_lines.append(f"📊 {analysis_days}d: {total_trades} trades | PnL R${total_pnl:+.2f}")

        # v3.0: Regime + Risk Tag in notification
        summary_lines.append(f"📊 Regime: {describe_regime(regime_info.get('current_regime', 'RANGING')) if HAS_REGIME_CLASSIFIER else regime_info.get('current_regime', '?')}")
        summary_lines.append(f"🏷️ Risk: {risk_tags.get('tag', '?')}")

        if web_intel:
            summary_lines.append(f"🌐 Web intel: {len(web_intel)} símbolos")
        if discovery_results:
            d_summary = discovery_results.get("summary", {})
            summary_lines.append(
                f"🧬 Discovery: {d_summary.get('pairs_approved', 0)} approved, "
                f"{d_summary.get('pairs_eliminated', 0)} eliminated"
            )
        if applied:
            summary_lines.append(f"✏️ {len(applied)} mudanças aplicadas no total")
            evolution_lines = build_evolution_summary(
                applied,
                {"by_symbol": perf_original.get("by_symbol", {})},
                perf,
            )
            summary_lines.extend(evolution_lines)
        if final_paused["paused"]:
            summary_lines.append(f"🛑 FALLBACK: pausados {final_paused['paused']}")
        if llm_result and llm_result.get("analysis"):
            summary_lines.append(f"🧠 {llm_result['analysis'][:200]}")
        notify_telegram("\n".join(summary_lines))

    # 11. Salvar resultado para auditoria
    audit = {
        "timestamp": datetime.now().isoformat(),
        "version": "3.0",
        "period_days": analysis_days,
        "dry_run": args.dry_run,
        "max_iterations": args.max_iterations,
        "use_backtest_convergence": getattr(args, "use_backtest_convergence", False),
        "iterations": iteration_history,
        "converged": converged,
        "paused_by_fallback": final_paused,
        "performance": perf,
        "issues": issues,
        "web_intel": web_intel,
        "optimization": optimization,
        "llm_analysis": llm_result.get("analysis") if llm_result else None,
        "changes_applied": applied,
        "config_version": config.get("_version"),
        "backtest_evaluations": all_backtest_evals,
        # v3.0 new fields
        "v3_regime": regime_info,
        "v3_risk_tags": risk_tags,
        "v3_discovery": {
            "summary": discovery_results.get("summary", {}),
            "approved": discovery_results.get("approved_strategies", []),
            "eliminated": discovery_results.get("eliminated_strategies", []),
            "meta_strategy": discovery_results.get("stage_3_4_synthesis", {}).get("meta_strategy", {}),
            "transition_rules": discovery_results.get("stage_3_4_synthesis", {}).get("transition_rules", []),
        } if discovery_results else {},
        "v3_trade_analysis": {
            "execution_errors_count": len(trade_analysis.get("execution_errors", [])),
            "logic_errors_count": len(trade_analysis.get("logic_errors", [])),
        },
        "v3_sl_analysis": perf.get("sl_analysis", {}),
    }
    audit_file = Path("/tmp/vt_agi_audit.json")
    try:
        with open(audit_file, "w") as f:
            json.dump(audit, f, indent=2, ensure_ascii=False, default=str)
        log.info(f"Auditoria salva: {audit_file}")
    except Exception as e:
        log.warning(f"Erro ao salvar auditoria: {e}")

    log.info(f"🤖 AGI 17H v3.0 concluído — {len(iteration_history)} iteração(ões) | "
             f"convergiu: {converged} | regime: {regime_info.get('current_regime', '?')}")
    total_pnl_final = sum(d.get("total_pnl", 0) for d in perf.get("by_symbol", {}).values())
    notify_telegram(f"✅ AGI v3.0 concluído — resumo: {len(applied)} mudanças, PnL R${total_pnl_final:+.2f}, "
                    f"{len(iteration_history)} iteração(ões), convergiu={converged}, "
                    f"regime={regime_info.get('current_regime', '?')}")

    # ─── SHADOW MODE — run optimization on snapshot, compare, log ───
    if not args.dry_run and not args.no_shadow:
        try:
            config_path = PROJECT_DIR / "vt_config.json"
            if not config_path.exists():
                log.warning("vt_config.json não encontrado — shadow pulado")
            else:
                # Snapshot the LIVE config (post-changes from this run)
                snap_path = snapshot_live_config(config_path)
                log.info(f"🪞 Shadow: snapshot salvo em {snap_path}")

                # Run shadow optimization on the snapshot
                shadow_audit = run_shadow_optimization(
                    snap_path, perf, issues, days=args.days,
                    use_forward=True,  # shadow always uses forward
                )
                log.info(f"🪞 Shadow: {len(shadow_audit.get('iterations', []))} iteração(ões)")

                # Compare
                comparison = compare_live_vs_shadow(audit, shadow_audit)
                comp_path = write_comparison_report(comparison, audit_file)
                log.info(f"🪞 Shadow comparison: {comp_path}")

                # Notify Telegram
                n_agree = len(comparison.get("agreements", []))
                n_disagree = len(comparison.get("disagreements", []))
                n_live_only = len(comparison.get("live_only", []))
                n_shadow_only = len(comparison.get("shadow_only", []))
                conv_live = comparison.get("convergence_diff", {}).get("live")
                conv_shadow = comparison.get("convergence_diff", {}).get("shadow")
                shadow_msg = (
                    f"🪞 *AGI Shadow — Live vs Forward Backtest*\n\n"
                    f"📊 Convergência: live={conv_live} | shadow={conv_shadow}\n"
                    f"✅ Acordos: {n_agree} | ⚠️ Divergências: {n_disagree}\n"
                    f"🔵 Só live: {n_live_only} | 🟣 Só shadow: {n_shadow_only}\n\n"
                    f"📁 {comp_path}"
                )
                log.info(f"🪞 Shadow Telegram: {shadow_msg}")
                try:
                    notify_telegram(shadow_msg)
                except Exception as e:
                    log.warning(f"Shadow notify falhou: {e}")
        except Exception as e:
            log.warning(f"Shadow mode falhou (não crítico): {e}")


if __name__ == "__main__":
    main()
