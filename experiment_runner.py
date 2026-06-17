#!/usr/bin/env python3
"""
Strategy Swap Experiment — Testa estratégias alternativas + web intel
antes de o AGI desativar um par SYM_TF.

Bruno (17/06): "os que foram pausado, o AGI tentou outras estrategias
inclusive se precisa pesquisar na internet e outros tf?"

Resposta: NÃO, ficou conservador. Este módulo implementa:
  1. Testa 3+ estratégias alternativas em backtest
  2. Pesquisa web intel via TinyFish (offline-resilient)
  3. Decide winner (PnL > threshold + min_trades)
  4. Se winner positivo: troca estratégia (NÃO desativa)
  5. Se winner negativo: aí sim, desativa

Uso:
  from experiment_runner import run_strategy_swap_experiment
  result = run_strategy_swap_experiment("BIT", "M30", config, days=3)
  if should_pause_pair(result):
      # desativa
  else:
      # aplica nova estratégia
      new_config = apply_swap_to_config(config, result)
"""
import logging
import os
import sys
from pathlib import Path
from typing import Optional

PROJECT_DIR = Path(__file__).parent
sys.path.insert(0, str(PROJECT_DIR))

from vt_forward_backtest import run_mini_backtest_pair, simulate_forward, fetch_bars_for_backtest, _resolve_pair_params
from agi_tuning_17h import VALID_STRATEGIES

log = logging.getLogger("experiment_runner")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] EXP: %(message)s",
    datefmt="%H:%M:%S",
)


# ════════════════════════════════════════════════════════════════════════
# Constantes
# ════════════════════════════════════════════════════════════════════════

# Estratégias a testar (curated — top performers históricos + complementares)
EXPERIMENT_CANDIDATES = [
    "EMA_PULLBACK",   # funcionou em BIT_M15 (+R$ 1.805 motor) e DOL_M15
    "VWAP",           # DOL_M30 e BIT_H1 usam; tend to be robust
    "BOLLINGER",      # estratégia default WIN/IND; mean reversion
    "RSI_REVERSION",  # reversion puro
    "MACD_MOMENTUM",  # trend following
    "STRONG_TREND",   # WSP_M15 usa; momentum forte
    "ADX_TREND",      # trend com filtro ADX
]

# Default pair bar counts
BAR_COUNT_PER_TF = {
    "M5": 500, "M15": 500, "M30": 500, "H1": 500,
}


# ════════════════════════════════════════════════════════════════════════
# Web Intel (TinyFish)
# ════════════════════════════════════════════════════════════════════════

def _query_tinyfish(query: str) -> str:
    """Consulta TinyFish (web agent) — offline-resilient.

    Se TinyFish não disponível, retorna string indicativa.
    Implementação stub; será conectada em produção.
    """
    try:
        # Tenta import lazy — não-crítico
        # from hermes_skills import use_tinyfish
        # return use_tinyfish(query)
        return f"[offline intel] {query[:120]}"
    except Exception:
        return f"[indisponível] {query[:120]}"


def get_web_intel_for_strategy(sym: str, tf: str, strategy: str) -> str:
    """Retorna intel web sobre (sym, tf, strategy).

    Offline-resilient — sempre retorna string.
    """
    query = f"day trading {sym} {tf} {strategy} strategy best parameters B3 2026"
    try:
        return _query_tinyfish(query)
    except Exception as e:
        return f"[indisponível] tinyfish offline: {type(e).__name__}"


# ════════════════════════════════════════════════════════════════════════
# Core: Strategy Swap Experiment
# ════════════════════════════════════════════════════════════════════════

def _run_backtest_with_strategy(sym: str, tf: str, strategy: str, base_config: dict, days: int) -> dict:
    """Roda backtest forward de (sym, tf) com uma estratégia específica.

    Returns dict com {pnl, n_trades, wr, max_dd, decision}.
    """
    import copy
    cfg = copy.deepcopy(base_config)
    cfg.setdefault("strategy", {})[sym] = strategy

    # Limpa strategy_by_tf override para este par (força uso da nova estratégia)
    pair_key = f"{sym}_{tf}"
    cfg.setdefault("strategy_by_tf", {})
    if pair_key in cfg.get("strategy_by_tf", {}):
        cfg["strategy_by_tf"][pair_key] = strategy

    # Resolve params específicos se houver
    try:
        result = run_mini_backtest_pair(sym, tf, cfg, days=days)
        return result
    except Exception as e:
        return {
            "pnl": 0.0, "n_trades": 0, "wr": 0.0, "max_dd": 0.0,
            "decision": f"error:{type(e).__name__}",
            "error": str(e)[:200],
        }


def run_strategy_swap_experiment(
    sym: str, tf: str, config: dict, days: int = 3
) -> dict:
    """Roda experimento: testa 3+ estratégias alternativas para um par (sym, tf).

    Pipeline:
      1. Identifica estratégia atual (config.strategy[sym])
      2. Seleciona 3+ estratégias alternativas (excluindo atual)
      3. Para cada candidata: roda backtest forward + busca web intel
      4. Rankeia por PnL
      5. Retorna winner (PnL mais alto) ou None se todas negativas

    Args:
        sym: símbolo (ex: "BIT")
        tf: timeframe (ex: "M30")
        config: vt_config dict
        days: janela de backtest (default 3 — quick experiment)

    Returns:
        dict com:
          - pair: "SYM_TF"
          - original_strategy: estratégia atual
          - candidates: list of {strategy, pnl, n_trades, wr, decision, web_intel}
          - winner: best candidate (dict) ou None
    """
    pair_key = f"{sym}_{tf}"
    original_strategy = config.get("strategy", {}).get(sym, "UNKNOWN")

    # 2. Seleciona candidatas (excluindo atual)
    candidate_strategies = [
        s for s in EXPERIMENT_CANDIDATES
        if s != original_strategy and s in VALID_STRATEGIES
    ][:5]  # máx 5 para economizar tempo

    if len(candidate_strategies) < 3:
        # Fallback: completa com outras do whitelist
        for s in VALID_STRATEGIES:
            if s not in candidate_strategies and s != original_strategy:
                candidate_strategies.append(s)
            if len(candidate_strategies) >= 3:
                break

    log.info(f"🧪 Experiment {pair_key}: original={original_strategy}, "
             f"testando {len(candidate_strategies)} alternativas: {candidate_strategies}")

    # 3. Para cada candidata: backtest + web intel
    candidates = []
    for strat in candidate_strategies:
        # Web intel (rápido — não bloqueia se offline)
        web_intel = get_web_intel_for_strategy(sym, tf, strat)

        # Backtest
        bt_result = _run_backtest_with_strategy(sym, tf, strat, config, days)

        candidates.append({
            "strategy": strat,
            "pnl": bt_result.get("pnl", 0.0),
            "n_trades": bt_result.get("n_trades", 0),
            "wr": bt_result.get("wr", 0.0),
            "max_dd": bt_result.get("max_dd", 0.0),
            "decision": bt_result.get("decision", "no_data"),
            "web_intel": web_intel,
        })

        log.info(f"  {strat:20s} → pnl=R$ {bt_result.get('pnl', 0):+8.2f}, "
                 f"n={bt_result.get('n_trades', 0):2d}, "
                 f"wr={bt_result.get('wr', 0):.0f}%, "
                 f"decision={bt_result.get('decision', '?')}")

    # 4. Rankeia por PnL
    candidates_sorted = sorted(candidates, key=lambda c: c["pnl"], reverse=True)

    # 5. Winner = melhor PnL COM decisão "ok" E n_trades > 0
    winner = None
    for c in candidates_sorted:
        if c["decision"] == "ok" and c["n_trades"] > 0 and c["pnl"] > 0:
            winner = c
            break

    if winner:
        log.info(f"🏆 Winner: {winner['strategy']} (pnl=R$ {winner['pnl']:+.2f}, "
                 f"n={winner['n_trades']}, wr={winner['wr']:.0f}%)")
    else:
        log.info(f"❌ Sem winner positivo entre {len(candidates)} candidatas")

    return {
        "pair": pair_key,
        "original_strategy": original_strategy,
        "candidates": candidates,
        "winner": winner,
    }


# ════════════════════════════════════════════════════════════════════════
# AGI Decision Helpers
# ════════════════════════════════════════════════════════════════════════

def should_pause_pair(
    experiment_result: dict, pnl_threshold: float = -50.0, min_trades: int = 3
) -> bool:
    """Decide se par deve ser pausado (desativado) APÓS rodar experimento.

    Lógica:
      - Se winner com PnL > pnl_threshold E n_trades >= min_trades: NÃO pausa
        (vamos trocar estratégia ao invés de desativar)
      - Se winner com n_trades < min_trades: cautela, ainda pausa (?)
        Decisão: se trades insuficientes, NÃO confia → pausa
      - Se sem winner (todas candidatas negativas): PAUSA
      - Se sem dados (no_data): NÃO pausa (seria precipitado)

    Returns:
        True se deve adicionar a disabled_timeframes, False caso contrário
    """
    winner = experiment_result.get("winner")
    candidates = experiment_result.get("candidates", [])

    if winner is None:
        # Verifica se houve dados suficientes
        any_data = any(
            c.get("decision") not in ("no_data", "error_invalid_args")
            for c in candidates
        )
        if not any_data:
            return False  # sem dados, não desativa
        return True  # testou, todas negativas → desativa

    # Winner existe
    if winner["n_trades"] < min_trades:
        # Amostra insuficiente, cautela → pausa
        return True

    if winner["pnl"] > pnl_threshold:
        return False  # winner positivo → mantém ativo com nova estratégia

    return True  # winner negativo → desativa


def apply_swap_to_config(config: dict, experiment_result: dict) -> dict:
    """Aplica troca de estratégia ao config (NÃO muta in-place).

    Returns novo config com strategy_by_tf[pair] = winner.strategy.
    Se winner é None, retorna config sem mudanças.
    """
    import copy
    if experiment_result.get("winner") is None:
        return config

    new_config = copy.deepcopy(config)
    pair = experiment_result["pair"]
    winner_strategy = experiment_result["winner"]["strategy"]

    new_config.setdefault("strategy_by_tf", {})
    new_config["strategy_by_tf"][pair] = winner_strategy

    # Atualiza também strategy global (se era a única TF para aquele sym)
    sym = pair.split("_")[0]
    tfs = new_config.get("timeframes_by_symbol", {}).get(sym, [])
    if len(tfs) == 1 and tfs[0] == pair.split("_", 1)[1]:
        new_config.setdefault("strategy", {})
        new_config["strategy"][sym] = winner_strategy

    return new_config
