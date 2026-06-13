#!/usr/bin/env python3
"""
AGI 17h Tuning — Análise dinâmica + LLM para otimização de parâmetros.

Fluxo:
  1. Lê performance real do SQLite (vt_trades.db) — últimos N dias
  2. Agrega por símbolo, timeframe, estratégia e exit_reason
  3. Identifica problemas (WR baixa, drawdown, SL excessivo, etc)
  4. Envia análise consolidada ao LLM ativo no Hermes (via `hermes -z`)
  5. LLM sugere ajustes de parâmetros em JSON
  6. Aplica mudanças via save_params() com BOUNDS CHECKING
  7. Loga auditoria + notifica Telegram

Uso:
  python3 agi_tuning_17h.py              # análise dos últimos 7 dias
  python3 agi_tuning_17h.py --days 3     # janela customizada
  python3 agi_tuning_17h.py --dry-run    # só analisa, não aplica
  python3 agi_tuning_17h.py --no-llm     # só estatísticas, sem LLM
"""

import argparse
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

sys.path.insert(0, str(Path(__file__).parent))

from vt_config_loader import load_config, save_params, save_full_config

# ─── Constants ───
PROJECT_DIR = Path(__file__).parent
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
    "sl_atr_mult":          (0.8, 3.0),
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

    return {
        "by_symbol": by_symbol,
        "by_symbol_tf": by_symbol_tf,
        "exit_reasons": exit_reasons,
        "today": today_perf,
        "streaks": streaks,
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


def web_intel_for_symbol(symbol: str) -> dict:
    """Coleta inteligência de mercado via tinyfish para um símbolo B3.

    Inclui:
    - Calendário econômico (BCB, FOMC, payroll)
    - Notícias recentes do ativo
    - Volatilidade histórica recente
    - Melhores práticas SL/ATR para o ativo

    Retorna dict com chaves: market_news, calendar_events, best_practices, sources
    """
    result = {
        "symbol": symbol,
        "market_news": [],
        "calendar_events": "",
        "best_practices": "",
        "sources": [],
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

    # 1. Buscar calendário econômico recente
    try:
        cal_results = tinyfish_search(
            f"calendário econômico Brasil {datetime.now().strftime('%B %Y')} FOMC BCB",
            num_results=3,
            timeout=15,
        )
        if cal_results:
            urls = [r.get("url", r.get("link", "")) for r in cal_results if r.get("url") or r.get("link")]
            urls = [u for u in urls if u][:2]
            if urls:
                text = tinyfish_fetch(urls, max_chars=1500, timeout=20)
                result["calendar_events"] = text
                result["sources"].extend(urls)
    except Exception as e:
        log.warning(f"web intel calendar erro: {e}")

    # 2. Buscar notícias do ativo + volatilidade recente
    try:
        news_results = tinyfish_search(
            f"{name} volatilidade análise técnica {datetime.now().strftime('%B')}",
            num_results=4,
            timeout=15,
        )
        for r in news_results:
            result["market_news"].append({
                "title": r.get("title", ""),
                "url": r.get("url", r.get("link", "")),
                "snippet": r.get("snippet", r.get("description", "")),
            })
            if r.get("url") or r.get("link"):
                result["sources"].append(r.get("url") or r.get("link"))
    except Exception as e:
        log.warning(f"web intel news erro: {e}")

    # 3. Buscar melhores práticas SL/ATR para o tipo de ativo
    try:
        bp_results = tinyfish_search(
            f"stop loss ATR mini índice WIN mini dólar melhores práticas day trade",
            num_results=3,
            timeout=15,
        )
        urls = [r.get("url", r.get("link", "")) for r in bp_results if r.get("url") or r.get("link")]
        urls = [u for u in urls if u][:2]
        if urls:
            text = tinyfish_fetch(urls, max_chars=1500, timeout=20)
            result["best_practices"] = text
    except Exception as e:
        log.warning(f"web intel best practices erro: {e}")

    log.info(f"🌐 Web intel para {symbol}: {len(result['market_news'])} notícias, "
             f"calendar: {len(result['calendar_events'])} chars, "
             f"best_practices: {len(result['best_practices'])} chars")
    return result


def ask_llm(prompt: str, timeout: int = 120) -> str | None:
    """Consulta o LLM ativo no Hermes via CLI não-interativo."""
    hermes_bin = _find_hermes()
    if not hermes_bin:
        log.warning("hermes CLI não encontrado no sistema")
        return None
    try:
        result = subprocess.run(
            [hermes_bin, "-z", prompt],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(PROJECT_DIR),
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        else:
            log.warning(f"LLM erro (rc={result.returncode}): {result.stderr[:300]}")
            return None
    except subprocess.TimeoutExpired:
        log.warning("LLM timeout (120s)")
        return None
    except FileNotFoundError:
        log.warning("hermes CLI não encontrado")
        return None
    except Exception as e:
        log.warning(f"LLM exceção: {e}")
        return None


def build_llm_prompt(perf: dict, issues: list, config: dict, web_intel: dict = None) -> str:
    """Constrói prompt para o LLM com performance + config atual + problemas + web intel."""
    web_intel = web_intel or {}

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

    prompt = f"""Você é o AGI de tuning do bot Vibe-Trading (B3 futuros). Analise a performance abaixo e sugira ajustes CIRÚRGICOS nos parâmetros.

## PERFORMANCE ({perf['period_days']} dias, desde {perf['cutoff_date']})
{chr(10).join(perf_lines)}

### Por timeframe:
{chr(10).join(tf_lines)}

### Exit reasons:
{chr(10).join(exit_lines)}

## PROBLEMAS DETECTADOS
{chr(10).join(issue_lines) if issue_lines else "  Nenhum problema crítico detectado"}

## CONFIG ATUAL
{chr(10).join(config_lines)}
"""

    # Adicionar seção de web intelligence (se houver)
    if web_intel:
        prompt += "\n## 🌐 INTELIGÊNCIA DE MERCADO (pesquisa web via tinyfish)\n"

        if web_intel.get("calendar_events"):
            prompt += f"""
### Calendário Econômico Relevante
{web_intel['calendar_events'][:1500]}

⚠️ Se houver evento de alto impacto (FOMC, COPOM, payroll, IPCA) agendado, considerar:
- Reduzir max_daily_trades para evitar volatilidade imprevisível
- Aumentar sl_atr_mult (SL mais largo) se evento for hoje/amanhã
"""

        if web_intel.get("market_news"):
            prompt += "\n### Notícias Recentes dos Ativos\n"
            for n in web_intel["market_news"][:5]:
                if n.get("title"):
                    prompt += f"- **{n['title']}**\n  {n.get('snippet', '')[:200]}\n"

        if web_intel.get("best_practices"):
            prompt += f"""
### Melhores Práticas SL/ATR (referência web)
{web_intel['best_practices'][:1500]}

Use essas referências para validar suas sugestões (ex: se a maioria dos traders usa 1.5x ATR para SL, mantenha nesse range).
"""

        if web_intel.get("sources"):
            prompt += f"\n### Fontes consultadas: {len(web_intel['sources'])} URLs\n"

    prompt += """
## REGRAS
1. Foque no símbolo com PIORES resultados primeiro
2. Se WR < 30%: tornar filtros MAIS restritivos (elevar thresholds, aumentar cooldown, reduzir max_daily_trades)
3. Se SL_SERVIDOR é a causa principal: ajustar sl_atr_mult (geralmente aumentar 0.2-0.5)
4. Se worst trade > -500R$: apertar SL (reduzir sl_atr_mult)
5. Se streak >= 4 perdas: aumentar cooldown_seconds + reduzir max_daily_trades
6. NUNCA sugerir mudanças maiores que 30% do valor atual por parâmetro
7. Para símbolos lucrativos (PnL > 0): NÃO mudar ou apenas micro-ajustes
8. Priorize REDUZIR perdas sobre aumentar ganhos
9. Se a web intel indicar evento macro iminente, considerar parâmetros defensivos

Retorne APENAS um JSON válido (sem markdown, sem comentários):
{{
  "analysis": "resumo breve do diagnóstico",
  "changes": [
    {{
      "symbol": "WIN",
      "params": {{"bb_std": 2.2, "cooldown_seconds": 900}},
      "reason": "WR 20% muito baixa, widen BB + cooldown"
    }}
  ]
}}

O JSON deve ter obrigatoriamente "analysis" (string) e "changes" (array). Cada change tem "symbol" (3 letras), "params" (dict de parâmetros), "reason" (string)."""

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
        # Tipo deve ser número
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

    return applied


# ═══════════════════════════════════════════════════════════════════
# 5. NOTIFICAÇÃO TELEGRAM
# ═══════════════════════════════════════════════════════════════════

def notify_telegram(msg: str):
    """Envia notificação para o Telegram (fila que o autotrader processa)."""
    notif_file = Path("/tmp/vt_notifications.jsonl")
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "event": "AGI_TUNING",
        "message": msg,
    }
    try:
        with open(notif_file, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════
# 6. RELATÓRIO FINAL
# ═══════════════════════════════════════════════════════════════════

def print_report(perf: dict, issues: list, llm_result: dict | None,
                 applied: list, config: dict, dry_run: bool, web_intel: dict = None):
    """Imprime relatório consolidado."""
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
        print(f"\n🌐 INTELIGÊNCIA DE MERCADO (tinyfish):")
        for sym, intel in web_intel.items():
            n_news = len(intel.get("market_news", []))
            has_cal = bool(intel.get("calendar_events"))
            has_bp = bool(intel.get("best_practices"))
            n_sources = len(intel.get("sources", []))
            print(f"  {sym}: {n_news} notícias | calendário: {'sim' if has_cal else 'não'} | "
                  f"best_practices: {'sim' if has_bp else 'não'} | {n_sources} fontes")
            # Top 3 títulos de notícias
            for n in intel.get("market_news", [])[:3]:
                if n.get("title"):
                    print(f"    • {n['title'][:80]}")

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

    # Config version
    print(f"\n📌 Config atual: v{config.get('_version', '?')} by {config.get('_updated_by', '?')}")
    print("=" * 60)


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="AGI 17h Tuning — otimização dinâmica de parâmetros")
    parser.add_argument("--days", type=int, default=7, help="Janela de análise em dias (default: 7)")
    parser.add_argument("--dry-run", action="store_true", help="Só analisa, não aplica mudanças")
    parser.add_argument("--no-llm", action="store_true", help="Só estatísticas, sem consulta LLM")
    parser.add_argument("--no-web", action="store_true", help="Não usar tinyfish para web intel")
    parser.add_argument("--timeout", type=int, default=120, help="Timeout LLM em segundos")
    parser.add_argument("--web-timeout", type=int, default=60, help="Timeout tinyfish em segundos (total)")
    args = parser.parse_args()

    log.info(f"🤖 AGI 17H iniciado — janela: {args.days} dias | dry-run: {args.dry_run} | "
             f"llm: {not args.no_llm} | web: {not args.no_web}")

    # 1. Carregar config
    config = load_config(force=True)
    if not config:
        log.error("Config não pôde ser carregada — abortando")
        sys.exit(1)

    # 2. Coletar performance
    perf = collect_performance(days=args.days)
    if not perf or not perf.get("by_symbol"):
        log.warning("Sem trades no período — nada para otimizar")
        print("Nenhum trade encontrado no período. Execute após ter dados de trading.")
        sys.exit(0)

    # 3. Diagnosticar problemas
    issues = diagnose_issues(perf)
    log.info(f"Diagnosticados {len(issues)} problemas")

    # 4. Web intelligence via tinyfish (se habilitado)
    web_intel = {}
    if not args.no_web and _tinyfish_check():
        log.info("🌐 Coletando inteligência de mercado via tinyfish...")
        # Focar nos símbolos com problemas (top 3 piores)
        worst_symbols = sorted(
            perf.get("by_symbol", {}).items(),
            key=lambda x: x[1]["total_pnl"]
        )[:3]
        worst_syms = [s[0] for s in worst_symbols]
        # Sempre inclui o ativo raiz mais negociado para calendário geral
        root_set = set(worst_syms) | set(["WIN", "WDO"])  # WIN/WDO são os mais sensíveis a macro

        for sym in list(root_set)[:3]:  # Limita a 3 símbolos para não demorar muito
            try:
                intel = web_intel_for_symbol(sym)
                if intel.get("market_news") or intel.get("calendar_events") or intel.get("best_practices"):
                    web_intel[sym] = intel
            except Exception as e:
                log.warning(f"web_intel erro para {sym}: {e}")
        log.info(f"🌐 Web intel coletada para {len(web_intel)} símbolos")
    else:
        if args.no_web:
            log.info("🌐 Web intel desabilitada por --no-web")
        else:
            log.warning("🌐 tinyfish não disponível — web intel pulada")

    # 5. LLM (se habilitado)
    llm_result = None
    if not args.no_llm:
        log.info("Consultando LLM ativo do Hermes...")
        prompt = build_llm_prompt(perf, issues, config, web_intel=web_intel)
        response = ask_llm(prompt, timeout=args.timeout)
        if response:
            llm_result = parse_llm_response(response)
            if llm_result:
                log.info(f"LLM retornou: {len(llm_result.get('changes', []))} mudanças sugeridas")
                log.info(f"Análise LLM: {llm_result.get('analysis', 'N/A')[:200]}")
            else:
                log.warning("LLM respondeu mas JSON não pôde ser parseado")
        else:
            log.warning("LLM não respondeu — usando apenas diagnóstico local")

    # 6. Aplicar mudanças
    applied = []
    if llm_result and llm_result.get("changes") and not args.dry_run:
        applied = apply_changes(llm_result, config, dry_run=False)
        # Recarregar config após mudanças
        config = load_config(force=True)
    elif llm_result and llm_result.get("changes") and args.dry_run:
        applied = apply_changes(llm_result, config, dry_run=True)

    # 7. Relatório
    print_report(perf, issues, llm_result, applied, config, args.dry_run, web_intel)

    # 8. Notificação Telegram (resumo)
    if applied:
        summary_lines = [f"🤖 AGI 17H — {len(applied)} mudanças {'aplicadas' if not args.dry_run else 'sugeridas'}"]
        total_pnl = sum(d["total_pnl"] for d in perf.get("by_symbol", {}).values())
        summary_lines.append(f"📊 Período {args.days}d: {sum(d['n_trades'] for d in perf.get('by_symbol', {}).values())} trades | PnL R${total_pnl:+.2f}")
        if web_intel:
            summary_lines.append(f"🌐 Web intel: {len(web_intel)} símbolos analisados")
        for a in applied:
            params_str = ", ".join(f"{k}={v}" for k, v in a["params"].items())
            summary_lines.append(f"  {'✅' if a['applied'] else '🔍'} {a['symbol']}: {params_str}")
        if llm_result and llm_result.get("analysis"):
            summary_lines.append(f"🧠 {llm_result['analysis'][:300]}")
        notify_telegram("\n".join(summary_lines))

    # 9. Salvar resultado para auditoria
    audit = {
        "timestamp": datetime.now().isoformat(),
        "period_days": args.days,
        "dry_run": args.dry_run,
        "performance": perf,
        "issues": issues,
        "web_intel": web_intel,
        "llm_analysis": llm_result.get("analysis") if llm_result else None,
        "changes_applied": applied,
        "config_version": config.get("_version"),
    }
    audit_file = Path("/tmp/vt_agi_audit.json")
    try:
        with open(audit_file, "w") as f:
            json.dump(audit, f, indent=2, ensure_ascii=False, default=str)
        log.info(f"Auditoria salva: {audit_file}")
    except Exception as e:
        log.warning(f"Erro ao salvar auditoria: {e}")

    log.info("🤖 AGI 17H concluído")


if __name__ == "__main__":
    main()
