"""
Strategy Explorer — Testa múltiplas combinações de parâmetros contra o histórico
e identifica configurações com edge positivo.

Não usa backtest tradicional (que exigiria dados intrabar) — usa o que TEMOS:
trades reais já executados. A ideia é: "se eu tivesse usado X param, o trade
teria saído no SL/TP em vez de X?"

Otimização: dado o que o bot JÁ fez, qual config teria maximizado PnL?

MULTI-STRATEGY: Antes de otimizar parâmetros, testa TODAS as estratégias
disponíveis para cada símbolo/timeframe. Encontra a melhor estratégia primeiro,
depois otimiza parâmetros dentro dela.
"""
import sqlite3
import logging
import re
from datetime import datetime, timedelta
from typing import Optional
from pathlib import Path
import json

log = logging.getLogger("strategy_explorer")

DB_PATH = Path(__file__).parent.parent / "vt_trades.db"
CONFIG_PATH = Path(__file__).parent.parent / "vt_config.json"

# ── Dynamic strategy discovery — scans strategies/ directory ────────────────
def discover_strategies() -> list[str]:
    """Scan strategies/ directory for all available strategies.
    Reads STRATEGY_NAME from each .py file without importing (fast).
    """
    strategies = []
    strategies_dir = Path(__file__).parent.parent / "strategies"
    for py_file in sorted(strategies_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        try:
            content = py_file.read_text()
            match = re.search(r'STRATEGY_NAME\s*=\s*["\']([^"\']+)["\']', content)
            if match:
                strategies.append(match.group(1))
        except Exception:
            continue
    return strategies

ALL_STRATEGIES = discover_strategies()
log.info(f"Discovered {len(ALL_STRATEGIES)} strategies: {ALL_STRATEGIES}")


def load_config() -> dict:
    """Carrega vt_config.json."""
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"Erro ao carregar config: {e}")
        return {}


def get_current_strategies() -> dict:
    """
    Retorna o mapa strategy_by_tf atual do config.
    Ex: {"WIN_M5": "DONCHIAN_BREAKOUT", "BIT_M5": "KELTNER_CHANNEL", ...}
    """
    config = load_config()
    return config.get("strategy_by_tf", {})


def get_all_symbols() -> list[str]:
    """Retorna lista de símbolos do config."""
    config = load_config()
    return config.get("symbols", ["WIN", "BIT", "WSP", "WDO"])


def get_timeframes_for_symbol(symbol: str) -> list[str]:
    """Retorna timeframes para um símbolo."""
    config = load_config()
    tfs_by_sym = config.get("timeframes_by_symbol", {})
    return tfs_by_sym.get(symbol, config.get("timeframes", ["M5", "M15", "M30", "H1"]))


def load_trades(days: int = 30, symbol: Optional[str] = None, tf: Optional[str] = None) -> list[dict]:
    """Carrega trades do SQLite com filtros opcionais."""
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = (cutoff - timedelta(days=days)).isoformat()

    query = "SELECT * FROM trades WHERE entry_time >= ?"
    params = [cutoff]
    if symbol:
        query += " AND symbol LIKE ?"
        params.append(f"%{symbol}%")
    if tf:
        query += " AND timeframe = ?"
        params.append(tf)
    query += " ORDER BY entry_time"

    cur.execute(query, params)
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return rows


def compute_stats(trades: list[dict]) -> dict:
    """Calcula métricas de um conjunto de trades."""
    if not trades:
        return {"n": 0, "wr": 0, "pnl": 0, "avg": 0, "best": 0, "worst": 0, "profit_factor": 0}
    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] <= 0]
    gross_profit = sum(t["net_pnl"] for t in wins)
    gross_loss = abs(sum(t["net_pnl"] for t in losses))

    # Max drawdown: cumulative PnL peak-to-trough
    cum_pnl = 0
    peak = 0
    max_dd = 0
    for t in sorted(trades, key=lambda x: x["entry_time"]):
        cum_pnl += t["net_pnl"]
        if cum_pnl > peak:
            peak = cum_pnl
        dd = peak - cum_pnl
        if dd > max_dd:
            max_dd = dd

    return {
        "n": len(trades),
        "wr": round(len(wins) / len(trades) * 100, 1),
        "pnl": round(sum(t["net_pnl"] for t in trades), 2),
        "avg": round(sum(t["net_pnl"] for t in trades) / len(trades), 2),
        "best": round(max(t["net_pnl"] for t in trades), 2),
        "worst": round(min(t["net_pnl"] for t in trades), 2),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else 99.0,
        "max_drawdown": round(max_dd, 2),
    }


def group_trades_by_strategy(trades: list[dict]) -> dict[str, list[dict]]:
    """Agrupa trades por estratégia (coluna 'strategy' do DB)."""
    groups = {}
    for t in trades:
        strat = t.get("strategy", "UNKNOWN")
        groups.setdefault(strat, []).append(t)
    return groups


def compare_strategies_for_pair(symbol: str, tf: str, days: int = 30) -> dict:
    """
    Compara TODAS as estratégias que já operaram em um symbol+timeframe.
    Retorna stats por estratégia, rankeadas por PnL (PF como desempate).

    Se uma estratégia não tem trades no histórico, retorna n=0 para ela.
    """
    trades = load_trades(days=days, symbol=symbol, tf=tf)
    if not trades:
        return {
            "symbol": symbol, "tf": tf, "n_trades": 0,
            "strategy_stats": {},
            "best_strategy": None,
            "current_strategy": None,
            "recommendation": None,
        }

    # Get current strategy from config
    config = load_config()
    current_strat = config.get("strategy_by_tf", {}).get(f"{symbol}_{tf}", "UNKNOWN")

    # Group by strategy
    by_strategy = group_trades_by_strategy(trades)

    # Compute stats per strategy
    strategy_stats = {}
    for strat, strat_trades in by_strategy.items():
        stats = compute_stats(strat_trades)
        stats["strategy"] = strat
        stats["is_current"] = (strat == current_strat)
        strategy_stats[strat] = stats

    # Rank by PnL (profit_factor as tiebreaker)
    ranked = sorted(
        strategy_stats.values(),
        key=lambda s: (s["pnl"], s["profit_factor"]),
        reverse=True,
    )

    best = ranked[0] if ranked else None

    # Determine recommendation
    recommendation = None
    current_stats = strategy_stats.get(current_strat)

    if best and current_stats and best["strategy"] != current_strat:
        # A different strategy has better stats
        if best["pnl"] > 0 and best["wr"] > 40 and best["profit_factor"] > 1.2:
            recommendation = {
                "action": "SWITCH",
                "from": current_strat,
                "to": best["strategy"],
                "reason": (
                    f"Estratégia {best['strategy']} tem resultados positivos "
                    f"(PnL R${best['pnl']:+.2f}, WR {best['wr']}%, PF {best['profit_factor']}) "
                    f"enquanto {current_strat} tem PnL R${current_stats['pnl']:+.2f}"
                ),
                "best_stats": best,
            }
        elif best["pnl"] > 0 and (current_stats["pnl"] <= 0 or current_stats["wr"] < 30):
            recommendation = {
                "action": "CONSIDER_SWITCH",
                "from": current_strat,
                "to": best["strategy"],
                "reason": (
                    f"Estratégia {best['strategy']} tem PnL positivo "
                    f"(R${best['pnl']:+.2f}, WR {best['wr']}%) "
                    f"enquanto {current_strat} está perdendo (PnL R${current_stats['pnl']:+.2f})"
                ),
                "best_stats": best,
            }

    # Identify untested strategies (no trades in history)
    tested_strategies = set(by_strategy.keys())
    untested = [s for s in ALL_STRATEGIES if s not in tested_strategies]

    return {
        "symbol": symbol,
        "tf": tf,
        "n_trades": len(trades),
        "current_strategy": current_strat,
        "strategy_stats": strategy_stats,
        "ranked_strategies": [s["strategy"] for s in ranked],
        "best_strategy": best["strategy"] if best else None,
        "untested_strategies": untested,
        "recommendation": recommendation,
    }


def compare_strategies_for_symbol(symbol: str, days: int = 30) -> dict:
    """
    Compara estratégias para todos os timeframes de um símbolo.
    """
    config = load_config()
    tfs = get_timeframes_for_symbol(symbol)
    strategy_by_tf = config.get("strategy_by_tf", {})

    results = {}
    for tf in tfs:
        pair_key = f"{symbol}_{tf}"
        if pair_key in config.get("disabled_timeframes", []):
            results[tf] = {
                "symbol": symbol, "tf": tf,
                "status": "DISABLED",
                "current_strategy": strategy_by_tf.get(pair_key, "UNKNOWN"),
            }
            continue

        comparison = compare_strategies_for_pair(symbol, tf, days=days)
        results[tf] = comparison

    return results


def filter_by_params(trades: list[dict], sim_params: dict) -> list[dict]:
    """
    Filtra trades que teriam SOBREVIVIDO com os params simulados.
    Critério simples: se net_pnl > 0 OU se loss < (sl_atr_mult * 50), passou.
    (Heurística: o que importa é manter winners e cortar losers piores)
    """
    if not sim_params:
        return trades

    sl_atr_mult = sim_params.get("sl_atr_mult", 1.0)
    max_loss_threshold = -abs(sl_atr_mult) * 50  # R$ -50 a -200 dependendo de mult

    # Manter winners + losers menores que threshold
    return [t for t in trades if t["net_pnl"] > 0 or t["net_pnl"] > max_loss_threshold]


def find_best_config(symbol: str, tf: Optional[str] = None, days: int = 30) -> dict:
    """
    Testa várias combinações de parâmetros no histórico e retorna a melhor.

    Para cada par de (sl_atr_mult, cooldown_seconds), simula o efeito no
    PnL histórico e ranqueia por profit factor + PnL total.
    """
    trades = load_trades(days=days, symbol=symbol, tf=tf)
    if len(trades) < 3:
        return {"symbol": symbol, "tf": tf, "n_trades": len(trades),
                "message": "Poucos trades para otimizar (< 3)", "best_config": None}

    log.info(f"Testando combinações para {symbol} {tf or '*'}: {len(trades)} trades")

    # Grid de params para testar
    sl_mults = [0.6, 0.8, 1.0, 1.2, 1.5, 2.0]
    cooldowns = [180, 300, 600, 900, 1500, 2400]
    # Filtros por estratégia (sempre inicializados para evitar warning)
    bb_stds = []
    rsi_filters = []
    vwap_thresholds = []
    adx_thresholds = []
    if symbol.upper() in ("WIN", "IND"):
        bb_stds = [1.8, 2.0, 2.3, 2.5, 2.7, 3.0, 3.3]
        rsi_filters = [(70, 30), (75, 25), (80, 20), (85, 15)]
    elif symbol.upper() == "BIT":
        vwap_thresholds = [(1.005, 0.995), (1.010, 0.990), (1.015, 0.985), (1.020, 0.980)]
    elif symbol.upper() == "WDO":
        vwap_thresholds = [(1.005, 0.995), (1.010, 0.990), (1.015, 0.985)]
    elif symbol.upper() in ("DOL", "WSP"):
        adx_thresholds = [15, 18, 20, 25]

    best = None
    results = []

    for sl in sl_mults:
        for cd in cooldowns:
            sim = {"sl_atr_mult": sl, "cooldown_seconds": cd}
            filtered = filter_by_params(trades, sim)
            stats = compute_stats(filtered)
            stats["sl_atr_mult"] = sl
            stats["cooldown_seconds"] = cd
            results.append(stats)

            if best is None or (
                stats["profit_factor"] > best["profit_factor"] and stats["pnl"] > 0
            ):
                best = stats

    # Adicionar testes específicos de estratégia
    if symbol.upper() in ("WIN", "IND"):
        for bb in bb_stds:
            for rsi_ob, rsi_os in rsi_filters:
                sim = {"bb_std": bb, "rsi_overbought": rsi_ob, "rsi_oversold": rsi_os,
                       "sl_atr_mult": best["sl_atr_mult"] if best else 1.0}
                filtered = filter_by_params(trades, sim)
                stats = compute_stats(filtered)
                stats["bb_std"] = bb
                stats["rsi_overbought"] = rsi_ob
                stats["rsi_oversold"] = rsi_os
                results.append(stats)

                if best is None or (stats["profit_factor"] > best["profit_factor"] and stats["pnl"] > 0):
                    best = stats

    # Ordenar top 5
    results.sort(key=lambda x: (x["profit_factor"], x["pnl"]), reverse=True)
    top5 = results[:5]

    return {
        "symbol": symbol,
        "tf": tf,
        "n_trades_original": len(trades),
        "best_config": best,
        "top5": top5,
    }


def explore_strategy_variants(symbol: str, days: int = 30) -> list[dict]:
    """
    Retorna variantes de configuração para A/B test no autotrader.

    Gera 2-3 configs: uma "agressiva", uma "moderada", uma "conservadora".
    """
    best = find_best_config(symbol, days=days)
    if not best.get("best_config"):
        return []

    cfg = best["best_config"]
    variants = []

    # Conservadora (PnL protegido, baixa frequência)
    variants.append({
        "label": "conservador",
        "params": {
            "sl_atr_mult": max(0.8, cfg.get("sl_atr_mult", 1.0)),
            "cooldown_seconds": max(1200, cfg.get("cooldown_seconds", 1500)),
            "max_daily_trades": 2,
            "breakeven_minutes": 10,
        },
        "expected_pnl": cfg.get("pnl", 0) * 0.7,  # estimativa conservadora
    })

    # Moderada (a melhor encontrada)
    variants.append({
        "label": "moderado",
        "params": {
            "sl_atr_mult": cfg.get("sl_atr_mult", 1.0),
            "cooldown_seconds": cfg.get("cooldown_seconds", 900),
            "max_daily_trades": 4,
            "breakeven_minutes": 15,
            **({"bb_std": cfg["bb_std"]} if "bb_std" in cfg else {}),
            **({"rsi_overbought": cfg["rsi_overbought"]} if "rsi_overbought" in cfg else {}),
            **({"rsi_oversold": cfg["rsi_oversold"]} if "rsi_oversold" in cfg else {}),
        },
        "expected_pnl": cfg.get("pnl", 0),
    })

    # Agressiva (mais trades, edge mais sensível)
    variants.append({
        "label": "agressivo",
        "params": {
            "sl_atr_mult": max(0.6, cfg.get("sl_atr_mult", 1.0) - 0.2),
            "cooldown_seconds": max(300, cfg.get("cooldown_seconds", 600) - 300),
            "max_daily_trades": 6,
            "breakeven_minutes": 20,
        },
        "expected_pnl": cfg.get("pnl", 0) * 0.5,  # mais volátil
    })

    return variants


def generate_strategy_comparison_report(days: int = 30) -> dict:
    """
    Gera relatório de comparação de estratégias para TODOS os símbolos/timeframes.

    Para cada SYM_TF:
    1. Agrupa trades por estratégia
    2. Calcula stats (WR, PnL, PF, avg_pnl, max_drawdown)
    3. Rankeia estratégias
    4. Identifica se uma troca de estratégia é recomendada
    5. Lista estratégias nunca testadas

    Retorna dict com recomendações e dados brutos.
    """
    symbols = get_all_symbols()
    report = {
        "generated_at": datetime.now().isoformat(),
        "by_symbol": {},
        "strategy_switches": [],  # Recomendações de troca
        "untested_pairs": [],     # SYM_TF com estratégias nunca testadas
    }

    for sym in symbols:
        sym_comparison = compare_strategies_for_symbol(sym, days=days)
        report["by_symbol"][sym] = sym_comparison

        for tf, data in sym_comparison.items():
            if isinstance(data, dict) and data.get("status") == "DISABLED":
                continue

            # Collect strategy switch recommendations
            if data.get("recommendation"):
                rec = data["recommendation"]
                report["strategy_switches"].append({
                    "pair": f"{sym}_{tf}",
                    "action": rec["action"],
                    "from": rec["from"],
                    "to": rec["to"],
                    "reason": rec["reason"],
                    "best_stats": rec.get("best_stats", {}),
                })

            # Collect untested strategies
            if data.get("untested_strategies"):
                report["untested_pairs"].append({
                    "pair": f"{sym}_{tf}",
                    "current": data.get("current_strategy"),
                    "untested": data["untested_strategies"],
                })

    return report


def generate_optimization_report() -> dict:
    """
    Gera relatório completo de exploração para todos os símbolos.

    INCLUA:
    1. Performance atual por símbolo
    2. Comparação de estratégias (multi-strategy)
    3. Melhor config de parâmetros (dentro da melhor estratégia)
    4. Variantes A/B
    """
    symbols = get_all_symbols()
    report = {
        "generated_at": datetime.now().isoformat(),
        "by_symbol": {},
        "strategy_comparison": {},  # NOVO: comparação de estratégias
        "strategy_switches": [],    # NOVO: recomendações de troca
    }

    # Phase 1: Strategy comparison for all symbols
    log.info("🔬 Fase 1: Comparando estratégias para todos os símbolos/timeframes...")
    strategy_report = generate_strategy_comparison_report(days=30)
    report["strategy_comparison"] = strategy_report
    report["strategy_switches"] = strategy_report.get("strategy_switches", [])

    if report["strategy_switches"]:
        log.info(f"🔬 {len(report['strategy_switches'])} trocas de estratégia recomendadas!")
        for sw in report["strategy_switches"]:
            log.info(f"  📊 {sw['pair']}: {sw['from']} → {sw['to']} ({sw['reason'][:80]})")

    # Phase 2: Parameter optimization per symbol
    for sym in symbols:
        trades = load_trades(days=30, symbol=sym)
        current = compute_stats(trades)

        best = find_best_config(sym, days=30)
        variants = explore_strategy_variants(sym, days=30)

        # Get strategy comparison for this symbol
        sym_comparison = strategy_report.get("by_symbol", {}).get(sym, {})

        report["by_symbol"][sym] = {
            "current_performance": current,
            "optimization": best,
            "variants_to_test": variants,
            "strategy_comparison": sym_comparison,  # NOVO
        }

    return report


# ── Imperative Rule (added to AGI prompt) ──────────────────────────────────
IMPERATIVE_RULE = (
    "REGRA IMPERATIVA: Antes de otimizar parâmetros de uma estratégia, "
    "testar TODAS as estratégias disponíveis para cada símbolo/timeframe. "
    "Se uma estratégia alternativa tiver resultados positivos "
    "(PnL > 0, WR > 40%, PF > 1.2), trocar para ela. "
    "Só depois otimizar parâmetros dentro da estratégia escolhida."
)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("🔍 Explorando configurações lucrativas...\n")

    report = generate_optimization_report()

    # Show strategy switches first
    if report.get("strategy_switches"):
        print("\n" + "=" * 60)
        print("🔄 TROCAS DE ESTRATÉGIA RECOMENDADAS")
        print("=" * 60)
        for sw in report["strategy_switches"]:
            print(f"\n  📊 {sw['pair']}")
            print(f"     De: {sw['from']}")
            print(f"     Para: {sw['to']}")
            print(f"     Razão: {sw['reason']}")
            bs = sw.get("best_stats", {})
            if bs:
                print(f"     Stats: PnL R${bs.get('pnl', 0):+.2f} | WR {bs.get('wr', 0)}% | PF {bs.get('profit_factor', 0)}")

    # Show per-symbol details
    for sym, data in report["by_symbol"].items():
        cur = data["current_performance"]
        opt = data["optimization"]
        variants = data["variants_to_test"]
        strat_comp = data.get("strategy_comparison", {})

        print(f"\n{'='*60}")
        print(f"📊 {sym}")
        print(f"  Atual: WR {cur['wr']}% | PnL R$ {cur['pnl']:+.2f} | PF {cur['profit_factor']}")

        # Show strategy comparison per timeframe
        if strat_comp:
            for tf, tf_data in sorted(strat_comp.items()):
                if isinstance(tf_data, dict) and tf_data.get("status") == "DISABLED":
                    print(f"  {tf}: DISABLED")
                    continue
                if not isinstance(tf_data, dict) or not tf_data.get("strategy_stats"):
                    continue
                current_strat = tf_data.get("current_strategy", "?")
                best_strat = tf_data.get("best_strategy", "?")
                n_trades = tf_data.get("n_trades", 0)
                print(f"\n  {tf} (atual: {current_strat}) — {n_trades} trades")
                ranked = tf_data.get("ranked_strategies", [])
                for i, strat_name in enumerate(ranked[:5]):
                    stats = tf_data["strategy_stats"].get(strat_name, {})
                    marker = "⭐" if strat_name == best_strat else ("📌" if strat_name == current_strat else "  ")
                    print(f"    {marker} {strat_name}: {stats.get('n', 0)}t | WR {stats.get('wr', 0)}% | PnL R${stats.get('pnl', 0):+.2f} | PF {stats.get('profit_factor', 0)}")
                untested = tf_data.get("untested_strategies", [])
                if untested:
                    print(f"    ❓ Não testadas: {', '.join(untested[:8])}")

                rec = tf_data.get("recommendation")
                if rec:
                    print(f"    🔄 RECOMENDAÇÃO: {rec['action']} → {rec['to']}")

        if opt.get("best_config"):
            best = opt["best_config"]
            print(f"  Melhor config params: SL {best.get('sl_atr_mult', '?')} | CD {best.get('cooldown_seconds', '?')}s")
            print(f"    → WR {best['wr']}% | PnL R$ {best['pnl']:+.2f} | PF {best['profit_factor']}")
        if variants:
            print(f"  Variantes para A/B test:")
            for v in variants:
                print(f"    • {v['label']}: esperado R$ {v['expected_pnl']:+.2f}")
