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
    "sl_atr_mult":          (0.5, 3.0),
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

    prompt = f"""Você é o AGI de tuning do bot Vibe-Trading (B3 futuros). Analise a performance abaixo e sugira ajustes CIRÚRGICOS nos parâmetros.

{memo_block}## PERFORMANCE ({perf['period_days']} dias, desde {perf['cutoff_date']})
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

    # Adicionar seção de optimization (Strategy Explorer) se houver
    if optimization:
        prompt += "\n## 🔬 STRATEGY EXPLORER (busca automática de configs lucrativas)\n"
        prompt += "O sistema testou 100+ combinações de parâmetros no histórico de 30 dias.\n"
        prompt += "Símbolos com configs lucrativas encontradas (PF>1 e PnL>0):\n\n"

        for sym, opt in optimization.items():
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
4. Se SL_SERVIDOR é a causa principal: ajustar sl_atr_mult (geralmente aumentar 0.2-0.5)
5. Se worst trade > -500R$: apertar SL (reduzir sl_atr_mult)
6. Se streak >= 4 perdas: aumentar cooldown_seconds + reduzir max_daily_trades
7. NUNCA sugerir mudanças maiores que 30% do valor atual por parâmetro
8. Para símbolos lucrativos (PnL > 0): NÃO mudar ou apenas micro-ajustes
9. PRIORIZE as dicas de configuração da estratégia vindas da web intel
10. Se a web intel indica padrão de pullback para o ativo, ajustar pullback_pct coerentemente
11. Se a web indica RSI ideal = 7 ao invés de 14, considerar usar rsi_period=7
12. LEMBRE-SE: o Explorer testou 100+ combinações — se ele disse "esta config dá lucro", CONFIE NELE
13. O objetivo é LUCRAR, não sobreviver. Se o Explorer achou config lucrativa, USE-A agressivamente.
14. **TROCA DE ESTRATÉGIA** (CRÍTICO): se um par SYM_TF continua não-lucrativo APÓS 2+ iterações de ajuste de parâmetros (PnL ≤ 0 com 8+ trades), TROQUE A ESTRATÉGIA via `params: {"strategy": "NOVA_ESTRATÉGIA"}`. Estratégias válidas: BOLLINGER, RSI_REVERSION, EMA_PULLBACK, VWAP, MACD_MOMENTUM, BREAKOUT, MEAN_REVERSION, MOMENTUM, TREND_FOLLOWING, DONCHIAN_BREAKOUT, ICHIMOKU, SUPERTREND, KELTNER_CHANNEL, STOCHASTIC, PARABOLIC_SAR, ATR_BREAKOUT. Teste no Explorer ANTES de aplicar. NÃO troque se o par está lucrativo.
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
        for sym, opt in optimization.items():
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

    Returns:
        (converged, failing_pairs) — failing é lista de "SYM_TF" que
        não atenderam o critério.
    """
    if mode == "absolute":
        # Reutiliza o legado
        conv, failing = _is_profitable_enough(current_perf)
        return conv, failing

    if mode != "delta":
        raise ValueError(f"mode inválido: {mode!r} (use 'absolute' ou 'delta')")

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
    from vt_forward_backtest import run_all_pairs_parallel

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


def main():
    parser = argparse.ArgumentParser(description="AGI 17h Tuning — otimização dinâmica de parâmetros")
    parser.add_argument("--days", type=int, default=7, help="Janela de análise em dias (default: 7)")
    parser.add_argument("--dry-run", action="store_true", help="Só analisa, não aplica mudanças")
    parser.add_argument("--no-llm", action="store_true", help="Só estatísticas, sem consulta LLM")
    parser.add_argument("--no-web", action="store_true", help="Não usar tinyfish para web intel")
    parser.add_argument("--timeout", type=int, default=120, help="Timeout LLM em segundos")
    parser.add_argument("--web-timeout", type=int, default=60, help="Timeout tinyfish em segundos (total)")
    parser.add_argument("--max-iterations", type=int, default=1,
                        help="Máximo de iterações do loop de convergência (1-5, default: 1 — single-shot)")
    parser.add_argument("--pause-failing", action="store_true",
                        help="Fallback final: auto-pausar pares SYM_TF com PnL≤0 após max-iterations")
    parser.add_argument("--convergence-mode", choices=["delta", "absolute"], default="delta",
                        help="Critério de convergência: 'delta' (compara PnL vs baseline inicial, "
                             "permite melhoria parcial) ou 'absolute' (legado: PnL>0 em todos). "
                             "Default: 'delta' — recomendado pra loops iterativos.")
    parser.add_argument("--max-workers", type=int, default=0,
                        help="Max processos paralelos pro forward backtest (0=auto via load avg)")
    parser.add_argument("--use-backtest-convergence", action="store_true",
                        help="Usa forward backtest como shadow-of-truth no critério de convergência")
    parser.add_argument("--no-shadow", dest="no_shadow", action="store_true", default=False,
                        help="Desativa shadow mode (default: ativo)")
    args = parser.parse_args()

    # Clamp iterations
    args.max_iterations = max(1, min(5, args.max_iterations))

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

    # 4.5. Strategy Explorer — busca configs lucrativas no histórico
    optimization = {}
    try:
        from strategy_explorer import generate_optimization_report
        log.info("🔬 Rodando Strategy Explorer (busca configs lucrativas)...")
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
                }
        log.info(f"🔬 Optimization encontrada para {len(optimization)} símbolos com PF>1")
    except Exception as e:
        log.warning(f"Strategy Explorer falhou: {e}")

    # 5. LLM (se habilitado)
    llm_result = None
    if not args.no_llm:
        log.info("Consultando LLM ativo do Hermes...")
        prompt = build_llm_prompt(perf, issues, config, web_intel=web_intel, optimization=optimization)
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

    # 6-7. LOOP DE ITERAÇÕES (convergência: todos SYM_TF com PnL > 0)
    iteration_history = []  # histórico de cada iteração
    all_backtest_evals = []  # forward backtest evaluations por iteração
    converged = False
    final_paused = {"paused": [], "skipped": []}
    applied = []

    # Captura snapshot ANTES do loop (baseline imutável pra convergence por delta)
    baseline_snapshot = snapshot_performance(perf) if args.convergence_mode == "delta" else {}
    log.info(f"📸 Baseline snapshot: {len(baseline_snapshot)} pares (modo={args.convergence_mode})")

    # Guardar perf ORIGINAL (antes de qualquer iteração) para o summary Telegram
    # Bruno 17/06: precisa ver evolução (PnL antes/depois + delta + %)
    perf_original = perf

    for it_num in range(1, args.max_iterations + 1):
        log.info(f"{'='*60}\n🔄 ITERAÇÃO {it_num}/{args.max_iterations}\n{'='*60}")

        # Recarregar perf fresca (do DB) a cada iteração, exceto na 1ª
        if it_num > 1:
            perf = collect_performance(days=args.days)
            issues = diagnose_issues(perf)
            # Recarregar config (já com mudanças da iteração anterior aplicadas)
            config = load_config(force=True)

        # 5. LLM (se habilitado) — reconstrói prompt a cada iteração
        llm_result = None
        if not args.no_llm:
            log.info(f"Consultando LLM (iteração {it_num})...")
            prompt = build_llm_prompt(perf, issues, config, web_intel=web_intel,
                                       optimization=optimization)
            response = ask_llm(prompt, timeout=args.timeout)
            if response:
                llm_result = parse_llm_response(response)
                if llm_result:
                    log.info(f"LLM iteração {it_num}: {len(llm_result.get('changes', []))} mudanças")
                else:
                    log.warning(f"LLM it {it_num} respondeu mas JSON inválido")
            else:
                log.warning(f"LLM it {it_num} sem resposta")

        # 6. Aplicar mudanças desta iteração
        iter_applied = []
        if llm_result and llm_result.get("changes"):
            if args.dry_run:
                iter_applied = apply_changes(llm_result, config, dry_run=True)
            else:
                iter_applied = apply_changes(llm_result, config, dry_run=False)
                config = load_config(force=True)
        applied.extend(iter_applied)

        # 7. Validar convergência (só faz sentido se não dry-run, pq dry-run não muda o DB)
        iter_paused = {"paused": [], "skipped": []}
        if not args.dry_run:
            # Re-ler perf atualizada (após mudanças)
            perf_after = collect_performance(days=args.days)
            if args.use_backtest_convergence:
                # Roda forward backtest em paralelo como sombra-de-verdade
                log.info("🔬 --use-backtest-convergence: rodando forward backtest shadow-of-truth...")
                bt_results = evaluate_forward_backtest(config, days=args.days,
                                                       max_workers=args.max_workers)
                converged, failing_pairs, bt_evals = merge_backtest_with_convergence(
                    perf_after, baseline_snapshot, bt_results, mode=args.convergence_mode
                )
                all_backtest_evals.append({"iteration": it_num, "evaluations": bt_evals})
            else:
                converged, failing_pairs = check_convergence(
                    perf_after, baseline_snapshot, mode=args.convergence_mode
                )
            log.info(f"📈 Convergência it {it_num}: {'SIM ✅' if converged else 'NÃO ❌'} "
                     f"({len(failing_pairs)} pares falhando: {failing_pairs})")
            perf = perf_after  # usa essa pra relatório

            # Registrar no histórico (helper testável — #4)
            iteration_history.append(
                build_iteration_history_entry(
                    it_num, iter_applied, failing_pairs, converged
                )
            )

            if converged:
                log.info(f"🎯 CONVERGÊNCIA atingida na iteração {it_num}!")
                break
        else:
            # dry-run: converged=None, failing=[]
            iteration_history.append(
                build_iteration_history_entry(it_num, iter_applied, [], None)
            )
            # Em dry-run, single-shot é suficiente — sem iteração real
            break

    # 8. FALLBACK — Auto-pausar pares não-convergentes
    # Regra Bruno 17/06: ANTES de desativar, testar outras estratégias + web intel
    if not converged and args.pause_failing and not args.dry_run:
        # Pegar pares que falharam na última iteração
        last_iter = iteration_history[-1] if iteration_history else {}
        failing = last_iter.get("failing_pairs", [])
        if failing:
            log.warning(f"⚠️ FALLBACK ATIVADO: {len(failing)} pares não-convergentes")

            # ── EXPERIMENT: testar estratégias alternativas antes de desativar ──
            from experiment_runner import run_strategy_swap_experiment, should_pause_pair, apply_swap_to_config

            still_failing = []
            swapped = []
            experiment_results = []

            for pair in failing:
                parts = pair.split("_", 1)
                if len(parts) != 2:
                    still_failing.append(pair)
                    continue
                sym, tf = parts
                exp_result = run_strategy_swap_experiment(sym, tf, config, days=args.days)
                experiment_results.append(exp_result)

                if should_pause_pair(exp_result):
                    still_failing.append(pair)
                    log.info(f"  🛑 {pair}: experimento não encontrou alternativa viável → pausando")
                else:
                    # Winner encontrado — aplica swap (não desativa)
                    config = apply_swap_to_config(config, exp_result)
                    winner = exp_result["winner"]
                    swapped.append({
                        "pair": pair,
                        "old": exp_result["original_strategy"],
                        "new": winner["strategy"],
                        "pnl": winner["pnl"],
                        "n_trades": winner["n_trades"],
                        "wr": winner["wr"],
                    })
                    log.info(f"  🔄 {pair}: {exp_result['original_strategy']} → {winner['strategy']} "
                             f"(pnl=R$ {winner['pnl']:+.2f}, n={winner['n_trades']}, wr={winner['wr']:.0f}%)")

            if swapped:
                save_full_config(config, updated_by="agi_17h_experiment_swap")
                config = load_config(force=True)
                log.info(f"🔄 SWAP: {len(swapped)} pares tiveram estratégia trocada (não desativados)")

            # Apenas os que realmente falharam no experimento são pausados
            final_paused = _pause_failing_pairs(still_failing, config, dry_run=False)
            config = load_config(force=True)

            # Salvar resultados do experimento no audit
            if experiment_results:
                audit_path = Path(f"/tmp/vt_agi_experiment_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
                import json as _json
                with open(audit_path, "w") as f:
                    _json.dump({
                        "timestamp": datetime.now().isoformat(),
                        "swapped": swapped,
                        "still_failing": still_failing,
                        "experiment_results": experiment_results,
                    }, f, indent=2, default=str)
                log.info(f"📋 Experiment audit: {audit_path}")
        else:
            log.info("✅ Nenhum par falhando — fallback não necessário")

    # 8.5 OTIMIZAÇÃO DE PARÂMETROS — Grid search paralelo para todos os pares ativos
    # Bruno 17/06: além de trocar estratégias, otimizar parâmetros de cada par
    # Bruno 17/06: paralelizar backtests em múltiplas CPUs (LLM stays sequential)
    if not args.dry_run:
        log.info("🔧 ETAPA 8.5: Otimização de parâmetros via grid search PARALELO")
        from agi_parallel import parallel_optimize_all_pairs
        from experiment_runner import PARAM_GRID

        param_results = parallel_optimize_all_pairs(
            config, PARAM_GRID, days=args.days
        )

        optimized_count = 0
        total_delta = 0
        param_changes = []

        for result in param_results:
            if result["delta"] > 50:  # Só aplica se delta > R$ 50
                pair = result["pair"]
                config.setdefault("params_by_tf", {}).setdefault(pair, {}).update(
                    result["best_params"]
                )
                optimized_count += 1
                total_delta += result["delta"]
                param_changes.append({
                    "pair": pair,
                    "strategy": result["strategy"],
                    "params": result["best_params"],
                    "delta": result["delta"],
                })
                log.info(
                    f"  ✅ {pair} ({result['strategy']}): +R$ {result['delta']:.2f} "
                    f"com {result['best_params']}"
                )

        if optimized_count > 0:
            save_full_config(config, updated_by="agi_17h_param_optimization")
            config = load_config(force=True)
            log.info(
                f"🔧 Otimização: {optimized_count} pares melhorados, "
                f"delta total R$ {total_delta:+.2f}"
            )

            # Save audit
            audit_path = Path(
                f"/tmp/vt_agi_param_opt_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            )
            import json as _json
            with open(audit_path, "w") as f:
                _json.dump(
                    {
                        "timestamp": datetime.now().isoformat(),
                        "optimized_count": optimized_count,
                        "total_delta": total_delta,
                        "changes": param_changes,
                    },
                    f,
                    indent=2,
                    default=str,
                )
            log.info(f"📋 Param optimization audit: {audit_path}")
        else:
            log.info("🔧 Nenhum parâmetro otimizado (delta < R$ 50)")

    # 9. Relatório final (consolidado com histórico de iterações)
    print_report(perf, issues, llm_result, applied, config, args.dry_run,
                 web_intel, optimization, iterations=iteration_history,
                 converged=converged, paused=final_paused)

    # 10. Notificação Telegram (resumo)
    if applied or final_paused["paused"]:
        summary_lines = [
            f"🤖 AGI 17H — {len(iteration_history)} iteração(ões) | "
            f"{'CONVERGIU ✅' if converged else 'NÃO convergiu ❌'}"
        ]
        total_pnl = sum(d["total_pnl"] for d in perf.get("by_symbol", {}).values())
        total_trades = sum(d["n_trades"] for d in perf.get("by_symbol", {}).values())
        summary_lines.append(f"📊 {args.days}d: {total_trades} trades | PnL R${total_pnl:+.2f}")
        if web_intel:
            summary_lines.append(f"🌐 Web intel: {len(web_intel)} símbolos")
        if applied:
            summary_lines.append(f"✏️ {len(applied)} mudanças aplicadas no total")
            # NOVO: evolução por symbol (PnL antes/depois + delta + %)
            # baseline_perf é o perf["by_symbol"] ANTES do loop
            # perf é o perf ATUAL (após iterações)
            # baseline_snapshot tem por SYM_TF; precisamos do by_symbol
            # Recriar baseline_by_symbol do perf ORIGINAL (perf_before)
            # → Guardamos isso na variável `perf_original` no início
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
        "period_days": args.days,
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
    }
    audit_file = Path("/tmp/vt_agi_audit.json")
    try:
        with open(audit_file, "w") as f:
            json.dump(audit, f, indent=2, ensure_ascii=False, default=str)
        log.info(f"Auditoria salva: {audit_file}")
    except Exception as e:
        log.warning(f"Erro ao salvar auditoria: {e}")

    log.info(f"🤖 AGI 17H concluído — {len(iteration_history)} iteração(ões) | "
             f"convergiu: {converged}")

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
