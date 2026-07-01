"""
optimization/agi_synthesizer.py
===============================
Loop de Síntese de Estratégia (Fase 2.1 — Regra 1 / Lei 5).

Objetivo
--------
Quando o AGI esgota a busca exaustiva e *nenhuma* estratégia é lucrativa para um
par (symbol, timeframe), este módulo entra em modo SÍNTESE: em vez de desistir
ou desabilitar o par (o que violaria a Lei 2 — Integridade de Escopo), ele gera
**variações de parâmetros** sobre as estratégias existentes e testa cada
variação via backtest até achar um edge ou esgotar as iterações.

Por que variação de params (e não híbridos/plugins novos)
---------------------------------------------------------
O handoff original mencionava gerar estratégias híbridas, mas a realidade do
código é que:
  - o "Regra 1" atual (`_create_new_strategy` em agi_tuning_17h.py:1709) já
    cria templates, mas `save_template()` NÃO está implementado (file_path=None);
  - gerar plugin novo exige escrever código Python de produção em runtime,
    o que é arriscado e não é testável via backtest sem deploy.

Variação de params é:
  - Totalmente testável via `simulate_forward()` (sem side-effects);
  - Composta apenas de valores dentro de `PARAM_BOUNDS` (já validados);
  - Idempotente: nunca desabilita símbolo/TF (Lei 2);
  - Conservadora: nunca inventa mágica nova — só recombina o que existe.

Contrato
--------
    >>> from optimization.agi_synthesizer import synthesize_strategy
    >>> result = synthesize_strategy(symbol="WIN", timeframe="M5",
    ...                              base_strategies=["ADX_TREND", "RSI_REVERSION"],
    ...                              max_iterations=50, min_pf=1.2)
    >>> result.summary.decision
    'edge_found'   # ou 'no_edge', 'disabled_symbol', 'no_bars', 'error'

Integração com código existente (sem duplicar)
----------------------------------------------
  - `simulate_forward(symbol, tf, bars, strategy_name, params, config)`
        → `optimization/vt_forward_backtest.py:314` (backtest atômico)
  - `fetch_bars_for_backtest(symbol, tf, count)`
        → `optimization/vt_forward_backtest.py:232` (bars via Wine/MT5)
  - `ALL_STRATEGIES`, `UNIVERSAL_PARAMS`, `STRATEGY_PARAM_GRIDS`
        → `optimization/exhaustive_strategy_search.py:25,43,49`
  - `is_permanently_disabled(symbol)`
        → `core/vt_autotrader.py:531` (hard-kill IND — Lei 2 defesa)
  - `save_params(symbol_root, params, updated_by)`
        → `core/vt_config_loader.py:446` (writer whitelisted)

Logs estruturados: cada iteração registra (strategy, params_hash, pnl, wr,
n_trades, max_dd, decision) para auditoria. Telegram opcional a cada N
iterações (hook, não acoplado — caller decide).
"""
from __future__ import annotations

import hashlib
import itertools
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# ── Imports do projeto (APIs reais, não inventadas) ─────────────────────────
# fetch/simulate vêm do módulo de backtest canônico
from optimization.vt_forward_backtest import (
    fetch_bars_for_backtest,
    simulate_forward,
)
from optimization.exhaustive_strategy_search import (
    ALL_STRATEGIES,
    ALL_SYMBOLS,
    ALL_TIMEFRAMES,
    UNIVERSAL_PARAMS,
    STRATEGY_PARAM_GRIDS,
)

log = logging.getLogger("agi_synthesizer")
if not log.handlers:
    logging.basicConfig(level=logging.INFO,
                        format="[%(asctime)s] [SYNTH] %(message)s",
                        datefmt="%H:%M:%S")


# ── Lei 2 (Integridade de Escopo): IND hard-kill — nunca testar/sintetizar ──
try:
    from core.vt_autotrader import is_permanently_disabled
except Exception:  # pragma: no cover — fallback defensivo se import circular
    def is_permanently_disabled(symbol: str) -> bool:
        """Fallback: IND é o único hard-kill conhecido (Bruno 2026-06-30)."""
        return bool(symbol) and "IND" in symbol.upper()


# ── Constantes de síntese (Lei 1: zero hardcode em produção) ────────────────
# Estas são constantes *de controle do loop*, não parâmetros de estratégia.
# Thresholds financeiros (min_pf etc.) vêm como argumento do caller.
DEFAULT_MAX_ITERATIONS = 50
DEFAULT_MIN_PROFIT_FACTOR = 1.2
DEFAULT_MIN_WIN_RATE = 0.50
DEFAULT_MIN_NET = 0.0          # > custo de transação (qualquer lucro líquido)
DEFAULT_BARS_COUNT = 500
TELEGRAM_HOOK_EVERY_N = 10     # caller pode pedir callback a cada 10 iters


@dataclass
class StrategyResult:
    """Resultado de uma única variação testada."""
    strategy: str
    params: Dict[str, Any]
    pnl: float
    n_trades: int
    win_rate: float
    max_dd: float
    profit_factor: float
    decision: str               # espelha simulate_forward.decision
    params_hash: str = ""

    def __post_init__(self) -> None:
        if not self.params_hash:
            self.params_hash = _hash_params(self.params)


@dataclass
class SynthesisReport:
    """Relatório completo de uma sessão de síntese."""
    symbol: str
    timeframe: str
    decision: str               # edge_found | no_edge | disabled_symbol | no_bars | error
    iterations_run: int = 0
    best: Optional[StrategyResult] = None
    tested: List[StrategyResult] = field(default_factory=list)
    elapsed_sec: float = 0.0
    reason: str = ""

    @property
    def summary(self) -> "SynthesisReport":
        return self


# ── Helpers ─────────────────────────────────────────────────────────────────
def _hash_params(params: Dict[str, Any]) -> str:
    """Hash estável de um dict de params (para auditoria/dedup)."""
    raw = ",".join(f"{k}={params[k]}" for k in sorted(params))
    return hashlib.md5(raw.encode()).hexdigest()[:8]


def _profit_factor(pnl: float, n_trades: int, win_rate: float) -> float:
    """Aproximação de profit factor via win_rate (simulate_forward não dá PF).

    PF ≈ (wins * avg_win) / (losses * avg_loss). Sem deals individuais,
    usamos uma proxy conservadora: se wr >= 1.0 → inf; se wr <= 0 → 0;
    senão PF ≈ wr / (1 - wr) (assume avg_win == avg_loss em módulo).
    Estratégias reais com SL/TP assimétricos terão PF diferente, mas esta
    proxy serve como gate de síntese — o gate final de produção é
    `_should_apply_changes` em agi_tuning_17h.py.
    """
    if n_trades == 0:
        return 0.0
    if win_rate >= 1.0:
        return float("inf")
    if win_rate <= 0.0:
        return 0.0
    return win_rate / (1.0 - win_rate)


def _generate_variations(strategy: str,
                         base_params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Gera combinações de params para UMA estratégia.

    Combina UNIVERSAL_PARAMS (sl_atr_mult, cooldown_seconds) com o grid
    específico da estratégia (STRATEGY_PARAM_GRIDS). Cada combinação parte de
    base_params (params atuais do config) e sobrescreve as chaves do grid.
    """
    grid: Dict[str, List[Any]] = {}
    grid.update(UNIVERSAL_PARAMS)
    grid.update(STRATEGY_PARAM_GRIDS.get(strategy, {}))

    keys = list(grid.keys())
    value_lists = [grid[k] for k in keys]
    variations: List[Dict[str, Any]] = []
    for combo in itertools.product(*value_lists):
        var = dict(base_params)  # começa do base (preserva params não-grid)
        var.update(dict(zip(keys, combo)))
        variations.append(var)
    return variations


def _is_profitable(res: StrategyResult, *,
                   min_pf: float, min_wr: float, min_net: float) -> bool:
    """Critério de lucratividade para uma variação testada."""
    if res.decision != "ok":
        return False
    if res.n_trades == 0:
        return False
    if res.pnl < min_net:
        return False
    if res.win_rate < min_wr:
        return False
    if res.profit_factor < min_pf:
        return False
    return True


# ── API pública ─────────────────────────────────────────────────────────────
def synthesize_strategy(
    symbol: str,
    timeframe: str,
    base_strategies: Optional[List[str]] = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    min_pf: float = DEFAULT_MIN_PROFIT_FACTOR,
    min_wr: float = DEFAULT_MIN_WIN_RATE,
    min_net: float = DEFAULT_MIN_NET,
    bars_count: int = DEFAULT_BARS_COUNT,
    config: Optional[dict] = None,
    base_params: Optional[Dict[str, Any]] = None,
    on_iteration: Optional[Callable[[StrategyResult, int], None]] = None,
) -> SynthesisReport:
    """Loop de síntese até achar edge ou esgotar iterações.

    Lei 2: se `symbol` for hard-killed (IND), retorna imediatamente com
    decision='disabled_symbol' — nunca testa nem sugere reativação.

    Args:
        symbol: root do contrato, ex. 'WIN', 'WDO'. (resolvido p/ MT5 internamente)
        timeframe: 'M5' | 'M15' | 'M30' | 'H1'.
        base_strategies: estratégias candidatas (default: ALL_STRATEGIES).
        max_iterations: teto de variações testadas.
        min_pf / min_wr / min_net: critério de lucratividade.
        bars_count: qtde de bars p/ backtest.
        config: config completo (passa p/ simulate_forward resolver limites).
        base_params: params base p/ variations (default: UNIVERSAL_PARAMS mínimos).
        on_iteration: callback opcional p/ Telegram/audit (res, n).

    Returns:
        SynthesisReport com .decision ∈ {edge_found, no_edge, disabled_symbol,
        no_bars, error} e .best = melhor StrategyResult (ou None).
    """
    t0 = time.time()
    symbol_up = (symbol or "").upper()

    # ── Lei 2: hard-kill short-circuit ─────────────────────────────────────
    if is_permanently_disabled(symbol_up):
        msg = (f"[SYNTH] {symbol_up}_{timeframe} SKIPPED — hard-killed "
               f"(PERMANENTLY_DISABLED). Lei 2: índice cheio, não operado.")
        log.warning(msg)
        return SynthesisReport(
            symbol=symbol_up, timeframe=timeframe,
            decision="disabled_symbol", reason="permanently_disabled",
        )

    if symbol_up not in ALL_SYMBOLS:
        log.warning("[SYNTH] símbolo %s fora de ALL_SYMBOLS", symbol_up)
    if timeframe not in ALL_TIMEFRAMES:
        log.warning("[SYNTH] timeframe %s fora de ALL_TIMEFRAMES", timeframe)

    strategies = list(base_strategies) if base_strategies else list(ALL_STRATEGIES)
    # Filtra estratégias inexistentes (defensivo)
    strategies = [s for s in strategies if s in ALL_STRATEGIES]
    if not strategies:
        return SynthesisReport(
            symbol=symbol_up, timeframe=timeframe,
            decision="error", reason="no_valid_strategies",
            elapsed_sec=time.time() - t0,
        )

    # ── Fetch bars uma vez (caro — via Wine/MT5) ───────────────────────────
    try:
        bars = fetch_bars_for_backtest(symbol_up, timeframe, count=bars_count)
    except Exception as e:  # pragma: no cover — Wine indisponível em CI
        log.error("[SYNTH] fetch_bars falhou p/ %s_%s: %s", symbol_up, timeframe, e)
        return SynthesisReport(
            symbol=symbol_up, timeframe=timeframe,
            decision="no_bars", reason=f"fetch_error: {e}",
            elapsed_sec=time.time() - t0,
        )
    if not bars:
        return SynthesisReport(
            symbol=symbol_up, timeframe=timeframe,
            decision="no_bars", reason="empty_bars",
            elapsed_sec=time.time() - t0,
        )

    # ── Base params mínimos p/ variations ──────────────────────────────────
    if base_params is None:
        base_params = {
            "sl_atr_mult": UNIVERSAL_PARAMS["sl_atr_mult"][1],   # 1.5 default
            "cooldown_seconds": UNIVERSAL_PARAMS["cooldown_seconds"][1],  # 300
        }

    tested: List[StrategyResult] = []
    best: Optional[StrategyResult] = None
    iterations = 0

    for strat in strategies:
        if iterations >= max_iterations:
            break
        try:
            variations = _generate_variations(strat, base_params)
        except Exception as e:  # pragma: no cover
            log.error("[SYNTH] variation gen failed %s: %s", strat, e)
            continue

        for params in variations:
            if iterations >= max_iterations:
                break
            iterations += 1
            try:
                raw = simulate_forward(
                    symbol_up, timeframe, bars, strat, params, config=config,
                )
            except Exception as e:  # pragma: no cover
                log.error("[SYNTH] backtest fail %s %s: %s", strat,
                          _hash_params(params), e)
                continue

            res = StrategyResult(
                strategy=strat,
                params=params,
                pnl=float(raw.get("pnl", 0.0)),
                n_trades=int(raw.get("n_trades", 0)),
                win_rate=float(raw.get("wr", 0.0)),
                max_dd=float(raw.get("max_dd", 0.0)),
                profit_factor=_profit_factor(
                    float(raw.get("pnl", 0.0)),
                    int(raw.get("n_trades", 0)),
                    float(raw.get("wr", 0.0)),
                ),
                decision=str(raw.get("decision", "error")),
                params_hash=_hash_params(params),
            )
            tested.append(res)

            if best is None or res.pnl > best.pnl:
                best = res

            if on_iteration is not None:
                try:
                    on_iteration(res, iterations)
                except Exception:  # pragma: no cover — callback nunca derruba
                    pass

            if _is_profitable(res, min_pf=min_pf, min_wr=min_wr, min_net=min_net):
                log.info(
                    "[SYNTH] EDGE FOUND %s_%s strat=%s pnl=%.2f wr=%.2f "
                    "pf=%.2f n=%d params=%s (iter %d/%d)",
                    symbol_up, timeframe, strat, res.pnl, res.win_rate,
                    res.profit_factor, res.n_trades, res.params_hash,
                    iterations, max_iterations,
                )
                return SynthesisReport(
                    symbol=symbol_up, timeframe=timeframe,
                    decision="edge_found", iterations_run=iterations,
                    best=res, tested=tested, elapsed_sec=time.time() - t0,
                    reason=f"profitable at iteration {iterations}",
                )

    decision = "edge_found" if (best and _is_profitable(
        best, min_pf=min_pf, min_wr=min_wr, min_net=min_net)) else "no_edge"
    log.info(
        "[SYNTH] DONE %s_%s decision=%s iterations=%d best_pnl=%.2f "
        "(%s) elapsed=%.1fs",
        symbol_up, timeframe, decision, iterations,
        best.pnl if best else 0.0,
        best.strategy if best else "none", time.time() - t0,
    )
    return SynthesisReport(
        symbol=symbol_up, timeframe=timeframe,
        decision=decision, iterations_run=iterations,
        best=best, tested=tested, elapsed_sec=time.time() - t0,
        reason=("best not profitable" if decision == "no_edge" else "profitable"),
    )


def synthesize_all_pairs(
    config: Optional[dict] = None,
    symbols: Optional[List[str]] = None,
    timeframes: Optional[List[str]] = None,
    **kwargs: Any,
) -> Dict[str, SynthesisReport]:
    """Roda synthesize_strategy para todos os pares (exceto hard-killed).

    Útil para integrar no cron AGI 17:10 quando run_exhaustive_search retornar
    all_negative_pairs. Retorna dict keyed por f"{SYM}_{TF}".

    Lei 2: pares hard-killed (IND*) são pulados automaticamente.
    """
    syms = symbols or list(ALL_SYMBOLS)
    tfs = timeframes or list(ALL_TIMEFRAMES)
    reports: Dict[str, SynthesisReport] = {}
    for sym in syms:
        for tf in tfs:
            key = f"{sym}_{tf}"
            reports[key] = synthesize_strategy(sym, tf, config=config, **kwargs)
    return reports


def _self_test() -> None:
    """Smoke test rápido (rodar manualmente, não via pytest)."""
    print("Testando synthesize_strategy com mock mínimo...")
    # Usa fetch real apenas se Wine disponível; senão falha gracefully.
    rep = synthesize_strategy("IND", "M5", max_iterations=3)
    print(f"  IND (hard-kill): decision={rep.decision}  (esperado: disabled_symbol)")
    rep = synthesize_strategy("XXX", "M5", max_iterations=3)
    print(f"  XXX (inválido): decision={rep.decision}  (esperado: no_bars/error)")


if __name__ == "__main__":  # pragma: no cover
    _self_test()
