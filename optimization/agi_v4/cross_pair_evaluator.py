"""cross_pair_evaluator.py — Avaliação cruzada de estratégias em múltiplos pares.

Motivação (Bruno 11/08/2026): o AGI gerava uma estratégia para o par failing
alvo (ex: WIN_M5) e a backtestava APENAS naquele par. Se desse 0 trades, era
rejeitada — mesmo que a mesma lógica tivesse edge excelente em WSP_H1 ou
BIT_M30. Esta camada permite testar uma estratégia (recém-gerada pelo Stage 4
ou órfã em strategies/_pending/) em VÁRIOS pares antes de descartar.

PRINCÍPIO DE SEGURANÇA: NENHUMA relaxação de gates. A avaliação cruzada reusa
o pipeline canônico — ast_gate + _runtime_smoke_gate + evaluate_candidate
(profitability PF/WR/n_trades/max_dd + walk-forward 4 janelas). Uma estratégia
só "vence" num par se passar no gate COMPLETO desse par. O fallback cruzado
apenas SALVA estratégias que seriam rejeitadas; nunca aprova algo que os gates
reprovariam.

Reuso (não duplica lógica):
  - core.vt_strategy_loader._strategies (registry) — injeta a estratégia para
    o lookup por nome enxergá-la via get_strategy_func (backtest_combo).
  - backtest_evaluator.evaluate_candidate — simulação bar-by-bar 30d + WF.
  - gates.ast_gate + stage4_generate._runtime_smoke_gate — filtro de sanidade.

Uso típico (Stage 4 fallback):
    from optimization.agi_v4.cross_pair_evaluator import cross_evaluate
    winner = cross_evaluate(name, path, active_pairs, config, thresholds,
                            exclude={target_pair})
    if winner:  # salvou num par diferente do alvo
        ...

Uso típico (varredura _pending/ — ver scripts/sweep_pending_strategies.py).
"""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

log = logging.getLogger("agi_v4.cross_pair")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ═══════════════════════════════════════════════════════════════════
# Loading + registro no registry do loader (para lookup por nome)
# ═══════════════════════════════════════════════════════════════════

def load_strategy_module(path: str | Path):
    """Carrega um arquivo .py de estratégia via importlib (sem __import__).

    Mesmo padrão de vt_strategy_loader.load_strategies e _runtime_smoke_gate.
    Não registra no registry — use register_strategy() para isso.

    Returns:
        module (com STRATEGY_NAME + check_entry) ou None se falhou.
    """
    path = Path(path)
    try:
        spec = importlib.util.spec_from_file_location(
            f"_cross_eval.{path.stem}", str(path)
        )
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception as e:
        log.debug(f"load_strategy_module {path.name}: falhou ({e})")
        return None


def register_strategy(name: str, path: str | Path) -> bool:
    """Carrega e injeta a estratégia no registry do vt_strategy_loader.

    Necessário porque backtest_combo/evaluate_candidate resolvem a estratégia
    por NOME via get_strategy_func, que lê o dict module-global _strategies.
    Estratégias em strategies/_pending/ (prefixo _) NÃO são carregadas pelo
    load_strategies (glob não-recursivo), então precisam de injeção explícita.

    GOTCHA: chama load_strategies() ANTES da injeção (só assim _loaded=True e
    o get_strategy_func não re-carrega limpando a injeção). Após a injeção,
    NÃO chamar load_strategies(force=True) — limparia a entrada.

    Returns:
        True se injetou com sucesso, False se o arquivo não tem check_entry.
    """
    import core.vt_strategy_loader as loader

    module = load_strategy_module(path)
    if module is None:
        return False
    check_entry = getattr(module, "check_entry", None)
    if check_entry is None:
        return False

    # Garante que o registry base está populado e _loaded=True (evita reload
    # que limparia nossa injeção). Se já _loaded, load_strategies() é no-op.
    loader.load_strategies()
    loader._strategies[name] = {
        "module": module,
        "check_entry": check_entry,
        "name": name,
        "file": str(path),
    }
    return True


# ═══════════════════════════════════════════════════════════════════
# Filtro de sanidade (reusa gates existentes)
# ═══════════════════════════════════════════════════════════════════

def smoke_check(path: str | Path):
    """Valida que a estratégia é segura + executável (ast + runtime_smoke).

    Reusa gates.ast_gate (syntax + STRATEGY_NAME + check_entry + Lei 3 SL +
    sandbox sem imports perigosos) e stage4_generate._runtime_smoke_gate
    (executa check_entry uma vez em barras sintéticas p/ capturar TypeError /
    bug de contrato). Não contatada com params de produção — só "não quebra".

    Returns:
        GateResult (passed/frozen). .passed=True → strategy é backtestável.
    """
    from optimization.agi_v4 import gates as _gates
    from optimization.agi_v4 import stage4_generate as _stage4

    path = Path(path)
    g_ast = _gates.ast_gate(path)
    if not g_ast:
        return g_ast
    return _stage4._runtime_smoke_gate(path)


# ═══════════════════════════════════════════════════════════════════
# Pares ativos (helper)
# ═══════════════════════════════════════════════════════════════════

def active_pairs(config: dict) -> list[str]:
    """Lista os pares (SYM_TF) ativos: symbols × timeframes_by_symbol, menos
    disabled_timeframes. São os pares que o daemon opera e onde faria sentido
    promover uma estratégia."""
    symbols = config.get("symbols", [])
    tfs_by_sym = config.get("timeframes_by_symbol", {})
    global_tfs = config.get("timeframes", [])
    disabled = set(config.get("disabled_timeframes", []))
    pairs: list[str] = []
    for sym in symbols:
        for tf in tfs_by_sym.get(sym, global_tfs):
            pair = f"{sym}_{tf}"
            if pair not in disabled:
                pairs.append(pair)
    return pairs


# ═══════════════════════════════════════════════════════════════════
# Avaliação cruzada — núcleo reusável
# ═══════════════════════════════════════════════════════════════════

def cross_evaluate(
    strategy_name: str,
    strategy_path: str | Path,
    candidate_pairs: list[str],
    config: dict,
    thresholds: dict | None = None,
    *,
    exclude: set[str] | None = None,
) -> dict | None:
    """Avalia a estratégia em vários pares e retorna o MELHOR que passou.

    Para cada par candidato (exceto os `exclude`), roda evaluate_candidate
    (30d bar-by-bar + walk-forward 4 janelas). Retorna o resultado do par com
    MAIOR total_pnl entre os que passaram (passed=True). Se nenhum passa,
    retorna None.

    A estratégia é injetada no registry (register_strategy) para que o
    evaluate_candidate a encontre por nome. params={} — a estratégia usa seus
    defaults internos (mesmo critério do Stage 4._simulate_generated).

    Args:
        strategy_name: STRATEGY_NAME dentro do arquivo.
        strategy_path: caminho do .py (ex: strategies/_pending/_agi4_xxx.py).
        candidate_pairs: pares "SYM_TF" para testar (ex: active_pairs(config)).
        config: config do vt_config.json.
        thresholds: gates thresholds (default: load_thresholds(config)).
        exclude: pares a pular (ex: o par alvo que já falhou).

    Returns:
        {"pair", "strategy", "params", "full", "walk_forward"} do melhor par
        aprovado, ou None se nenhum par passou.
    """
    if not register_strategy(strategy_name, strategy_path):
        log.warning(f"cross_evaluate: não consegui registrar {strategy_name} ({strategy_path})")
        return None

    from optimization.agi_v4.backtest_evaluator import evaluate_candidate

    if thresholds is None:
        from optimization.agi_v4.gates import load_thresholds
        thresholds = load_thresholds(config)

    exclude = exclude or set()
    best: dict | None = None
    best_pnl = -float("inf")

    for pair in candidate_pairs:
        if pair in exclude:
            continue
        parts = pair.split("_", 1)
        if len(parts) != 2:
            continue
        sym, tf = parts
        try:
            result = evaluate_candidate(sym, tf, strategy_name, {}, config,
                                        thresholds=thresholds)
        except Exception as e:
            log.debug(f"cross_evaluate {strategy_name} {pair}: erro {e}")
            continue

        if not result.get("passed"):
            continue

        # Passou no gate completo (profitability + walk-forward) deste par.
        pnl = (result.get("full") or {}).get("total_pnl", -float("inf"))
        if pnl > best_pnl:
            best_pnl = pnl
            best = {
                "pair": pair,
                "strategy": strategy_name,
                "params": {},
                "full": result["full"],
                "walk_forward": result.get("walk_forward", []),
                "gates_passed": ["ast", "profitability_full", "walk_forward"],
                "generated": True,  # marca p/ o Stage 5 promover (move do _pending)
                "pending_path": str(strategy_path),
            }
            log.info(
                f"cross_evaluate ✓ {strategy_name} APROVADA em {pair}: "
                f"PnL=R${pnl:.2f} PF={result['full'].get('pf', 0):.2f}"
            )

    if best is None:
        log.info(f"cross_evaluate: {strategy_name} não passou em nenhum par alternativo")
    return best
