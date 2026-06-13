"""
Strategy Explorer — Testa múltiplas combinações de parâmetros contra o histórico
e identifica configurações com edge positivo.

Não usa backtest tradicional (que exigiria dados intrabar) — usa o que TEMOS:
trades reais já executados. A ideia é: "se eu tivesse usado X param, o trade
teria saído no SL/TP em vez de X?"

Otimização: dado o que o bot JÁ fez, qual config teria maximizado PnL?
"""
import sqlite3
import logging
from datetime import datetime
from typing import Optional
from pathlib import Path

log = logging.getLogger("strategy_explorer")

DB_PATH = Path(__file__).parent / "vt_trades.db"


def load_trades(days: int = 30, symbol: Optional[str] = None, tf: Optional[str] = None) -> list[dict]:
    """Carrega trades do SQLite com filtros opcionais."""
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    from datetime import timedelta
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
    return {
        "n": len(trades),
        "wr": round(len(wins) / len(trades) * 100, 1),
        "pnl": round(sum(t["net_pnl"] for t in trades), 2),
        "avg": round(sum(t["net_pnl"] for t in trades) / len(trades), 2),
        "best": round(max(t["net_pnl"] for t in trades), 2),
        "worst": round(min(t["net_pnl"] for t in trades), 2),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else 99.0,
    }


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


def generate_optimization_report() -> dict:
    """Gera relatório completo de exploração para todos os símbolos."""
    symbols = ["WIN", "IND", "WDO", "DOL", "WSP", "BIT"]
    report = {"generated_at": datetime.now().isoformat(), "by_symbol": {}}

    for sym in symbols:
        trades = load_trades(days=30, symbol=sym)
        current = compute_stats(trades)

        best = find_best_config(sym, days=30)
        variants = explore_strategy_variants(sym, days=30)

        report["by_symbol"][sym] = {
            "current_performance": current,
            "optimization": best,
            "variants_to_test": variants,
        }

    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("🔍 Explorando configurações lucrativas...\n")

    report = generate_optimization_report()

    for sym, data in report["by_symbol"].items():
        cur = data["current_performance"]
        opt = data["optimization"]
        variants = data["variants_to_test"]

        print(f"\n{'='*60}")
        print(f"📊 {sym}")
        print(f"  Atual: WR {cur['wr']}% | PnL R$ {cur['pnl']:+.2f} | PF {cur['profit_factor']}")
        if opt.get("best_config"):
            best = opt["best_config"]
            print(f"  Melhor encontrada: SL {best.get('sl_atr_mult', '?')} | CD {best.get('cooldown_seconds', '?')}s")
            print(f"    → WR {best['wr']}% | PnL R$ {best['pnl']:+.2f} | PF {best['profit_factor']}")
        if variants:
            print(f"  Variantes para A/B test:")
            for v in variants:
                print(f"    • {v['label']}: esperado R$ {v['expected_pnl']:+.2f}")
