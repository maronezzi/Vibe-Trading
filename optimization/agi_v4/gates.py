"""
gates.py — Gates de segurança centralizados da AGI v4.

Todos os estágios que decidem APLICAR algo no vt_config.json ou PROMOVER
uma estratégia nova devem passar por estes gates. Centralização garante que
nenhum stage bypassa as Leis de Ouro.

Gates implementados:
  - profitability_gate: PF, WR, n_trades, max_dd mínimos (sobre trades SIMULADOS)
  - walk_forward_gate: divide 30d em janelas, exige consistência (anti-overfit)
  - ast_gate: plugin .py válido (syntax + STRATEGY_NAME + check_entry + SL)
  - regra1_gate: PnL simulado do candidato > PnL simulado do baseline (Lei 5)

  NOTA (2026-07-04): gap_gate (backtest vs DB) foi REMOVIDO. Trades do DB
  pertenciam à estratégia antiga — não são referência honesta. Toda
  avaliação é por simulação bar-by-bar em 30d do MT5 (ver backtest_
  evaluator.py).

Thresholds vêm de vt_config.json (chave "agi_v4_gates") com fallback
conservador — Lei 1 (zero hardcode em produção, defaults só p/ bootstrap).
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("agi_v4.gates")

# ── Defaults conservadores (override via vt_config.json["agi_v4_gates"]) ──
# Estes NÃO são hardcode de produção — são bootstrap defaults usados só
# quando o config não define a chave. Em produção o config é autoritativo.
_DEFAULT_THRESHOLDS = {
    "min_profit_factor": 1.15,   # Wave 880.A2: 1.05→1.15 (PF 1.05 mal cobre spread+comissão)
    "min_win_rate": 0.35,        # 35% — fração (Wave 880.A4: unificado entre gates.py e backtest_evaluator.py)
    "min_trades": 20,            # trades suficientes p/ significância
    "max_drawdown_pct": -25.0,   # floor de drawdown
    "max_backtest_db_gap_x": 1.5,  # bug B3/B4 do exhaustive antigo
    "min_walk_forward_consistency": 0.65,  # Wave 880.C3: 60%→65% das janelas positivas
    "min_30d_projection_improvement": 0.0,  # Regra 1: candidato > baseline
    # Wave hoje-conta-mais: bônus no comparativo do stage5 para o PnL do pregão
    # atual. today_weight=0.3 = hoje conta 30% extra no score de desempate.
    # today_min_trades = nº mínimo de trades hoje p/ o bônus ser aplicado
    # (evita overfit numa meia-sessão com poucos trades). today_weight=0 desliga.
    "today_weight": 0.3,
    "today_min_trades": 3,
}


def load_thresholds(config: dict | None = None) -> dict:
    """Carrega thresholds do config ou usa defaults.

    Lei 1: em produção, vt_config.json["agi_v4_gates"] é autoritativo.
    Defaults só garantem que a AGI funcione antes do config ser populado.
    """
    thresholds = dict(_DEFAULT_THRESHOLDS)
    if config and isinstance(config.get("agi_v4_gates"), dict):
        overrides = config["agi_v4_gates"]
        for k in thresholds:
            if k in overrides:
                try:
                    thresholds[k] = type(thresholds[k])(overrides[k])
                except (ValueError, TypeError):
                    log.warning(f"[gates] threshold inválido ignorado: {k}={overrides[k]!r}")
    return thresholds


# ═══════════════════════════════════════════════════════════════════
# Resultado de gate — imutável para auditoria
# ═══════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class GateResult:
    """Resultado de um gate de segurança. Frozen para auditoria forense."""
    passed: bool
    gate_name: str
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.passed


# ═══════════════════════════════════════════════════════════════════
# Gate 1: Profitability — métricas mínimas do backtest
# ═══════════════════════════════════════════════════════════════════

def profitability_gate(
    backtest_result: dict,
    thresholds: dict | None = None,
) -> GateResult:
    """Valida que um backtest atende PF/WR/n_trades/max_dd mínimos.

    Args:
        backtest_result: dict retornado por backtest_v944.backtest_combo()
            ou compatível. Deve ter: pf, wr, n_trades (ou "trades"), max_dd.
        thresholds: dict de thresholds (default: load_thresholds()).

    Returns:
        GateResult com passed=True se todos os critérios forem atendidos.
    """
    th = thresholds or _DEFAULT_THRESHOLDS
    pf = float(backtest_result.get("pf", 0) or 0)
    wr = float(backtest_result.get("wr", 0) or 0)
    #Compat com engines que usam "trades" ou "n_trades"
    n = int(backtest_result.get("n_trades", backtest_result.get("trades", 0)) or 0)
    max_dd = float(backtest_result.get("max_dd", 0) or 0)

    failures = []
    if pf < th["min_profit_factor"]:
        failures.append(f"PF={pf:.2f} < {th['min_profit_factor']}")
    if wr < th["min_win_rate"]:
        failures.append(f"WR={wr:.1%} < {th['min_win_rate']:.1%}")
    if n < th["min_trades"]:
        failures.append(f"n_trades={n} < {th['min_trades']}")
    if max_dd < th["max_drawdown_pct"]:
        failures.append(f"max_dd={max_dd:.1f}% < {th['max_drawdown_pct']:.1f}%")

    if failures:
        return GateResult(
            passed=False,
            gate_name="profitability",
            reason="; ".join(failures),
            details={"pf": pf, "wr": wr, "n_trades": n, "max_dd": max_dd},
        )
    return GateResult(
        passed=True,
        gate_name="profitability",
        details={"pf": pf, "wr": wr, "n_trades": n, "max_dd": max_dd},
    )


# ═══════════════════════════════════════════════════════════════════
# REMOVIDO: gap_gate (backtest vs DB) — 2026-07-04
# ═══════════════════════════════════════════════════════════════════
# Era: comparar backtest do candidato contra trades passados do DB.
# Por que foi removido: trades no vt_trades.db foram executados pela
# estratégia ANTIGA. Não são referência honesta para julgar um candidato
# novo (estratégia/params diferentes gerariam trades diferentes). Usar
# o histórico de UMA estratégia para validar OUTRA é desonesto.
#
# Substituído por: avaliação 100% por simulação bar-by-bar (backtest_
# evaluator.py) + walk-forward por janelas. O baseline correto para
# julgar um candidato é a SIMULAÇÃO da estratégia atual do config nas
# MESMAS 30d de mercado — não o histórico do DB.



# ═══════════════════════════════════════════════════════════════════
# Gate 3: Walk-forward — performance consistente entre janelas
# ═══════════════════════════════════════════════════════════════════

def walk_forward_gate(
    window_results: list[dict],
    thresholds: dict | None = None,
) -> GateResult:
    """Valida que a estratégia é lucrativa em múltiplas janelas (não overfit).

    Args:
        window_results: lista de backtest_results, um por janela de tempo.
            Cada dict deve ter "pnl" (ou "ret" %).
        thresholds: dict de thresholds.

    Returns:
        GateResult passed=True se >= min_walk_forward_consistency das janelas
        são positivas.
    """
    th = thresholds or _DEFAULT_THRESHOLDS
    min_consistency = th["min_walk_forward_consistency"]

    if not window_results:
        return GateResult(
            passed=False,
            gate_name="walk_forward",
            reason="nenhuma janela fornecida",
        )

    def _pnl_of(w: dict) -> float:
        # Compat: "pnl" (R$) ou "ret" (%)
        if "pnl" in w:
            return float(w["pnl"] or 0)
        return float(w.get("ret", 0) or 0)

    positives = sum(1 for w in window_results if _pnl_of(w) > 0)
    consistency = positives / len(window_results)

    if consistency < min_consistency:
        return GateResult(
            passed=False,
            gate_name="walk_forward",
            reason=f"consistência={consistency:.0%} < {min_consistency:.0%} "
                   f"({positives}/{len(window_results)} janelas positivas)",
            details={"consistency": consistency, "n_windows": len(window_results),
                     "n_positive": positives},
        )
    return GateResult(
        passed=True,
        gate_name="walk_forward",
        details={"consistency": consistency, "n_windows": len(window_results),
                 "n_positive": positives},
    )


# ═══════════════════════════════════════════════════════════════════
# Gate 4: AST — valida plugin .py gerado (Lei 3: SL obrigatório)
# ═══════════════════════════════════════════════════════════════════

# Indicadores de SL no corpo de check_entry. Lei 3 (SL mandatory): toda
# estratégia gerada DEVE retornar sl_pts > 0 em sinais. Verificamos que
# o código fonte referencia SL de alguma forma.
_SL_MARKERS = ("sl_pts", "sl_price", "calc_sl", "stop_loss", "sl =")

# Imports/builtins perigosos — estratégia NUNCA deve chamar diretamente.
# Ela recebe tudo via params/utils; não deve importar mt5, subprocess, etc.
_FORBIDDEN_IMPORTS = {
    "subprocess", "os.system", "mt5", "MetaTrader5",
    "socket", "urllib", "requests", "httpx",
    "shutil", "pathlib",  # file I/O — estratégia é stateless
}


def ast_gate(plugin_path: str | Path) -> GateResult:
    """Valida que um plugin .py gerado é seguro e bem-formado.

    Checks (Lei 3 — SL mandatory + sandbox):
      1. Syntax: arquivo parseia como Python válido
      2. Contrato: tem STRATEGY_NAME (str) e check_entry (callable)
      3. Lei 3: corpo de check_entry referencia sl_pts (ou equivalente)
      4. Sandbox: não importa módulos perigosos (subprocess, mt5, socket...)

    Args:
        plugin_path: caminho para o arquivo .py.

    Returns:
        GateResult passed=True se o plugin é seguro para backtest.
    """
    plugin_path = Path(plugin_path)
    if not plugin_path.exists():
        return GateResult(passed=False, gate_name="ast",
                          reason=f"arquivo não existe: {plugin_path}")

    src = plugin_path.read_text(encoding="utf-8")

    # 1. Syntax
    try:
        tree = ast.parse(src, filename=str(plugin_path))
    except SyntaxError as e:
        return GateResult(passed=False, gate_name="ast",
                          reason=f"SyntaxError: {e.msg} (linha {e.lineno})",
                          details={"syntax_error": str(e)})

    # 2. Contrato: STRATEGY_NAME + check_entry
    has_name = any(
        isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "STRATEGY_NAME" for t in node.targets)
        for node in ast.walk(tree)
    )
    has_check_entry = any(
        isinstance(node, ast.FunctionDef) and node.name == "check_entry"
        for node in ast.walk(tree)
    )
    if not has_name:
        return GateResult(passed=False, gate_name="ast",
                          reason="faltou STRATEGY_NAME = \"...\" no nível do módulo")
    if not has_check_entry:
        return GateResult(passed=False, gate_name="ast",
                          reason="faltou def check_entry(...) — contrato do plugin")

    # 3. Lei 3: SL referenciado no corpo de check_entry
    check_entry_node = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "check_entry"),
        None,
    )
    if check_entry_node is not None:
        body_src = ast.get_source_segment(src, check_entry_node) or ""
        if not any(marker in body_src for marker in _SL_MARKERS):
            return GateResult(
                passed=False, gate_name="ast",
                reason="LEI 3 VIOLADA: check_entry não referencia sl_pts/sl_price/calc_sl "
                       "(SL obrigatório em todo sinal)",
            )

    # 4. Sandbox: sem imports perigosos
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _FORBIDDEN_IMPORTS or alias.name.split(".")[0] in _FORBIDDEN_IMPORTS:
                    return GateResult(
                        passed=False, gate_name="ast",
                        reason=f"import proibido em plugin: {alias.name} "
                               "(estratégia deve ser stateless, recebe tudo via utils/params)",
                    )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod in _FORBIDDEN_IMPORTS or mod.split(".")[0] in _FORBIDDEN_IMPORTS:
                return GateResult(
                    passed=False, gate_name="ast",
                    reason=f"import proibido em plugin: from {mod} import ...",
                )

    return GateResult(passed=True, gate_name="ast",
                      details={"file": str(plugin_path)})


# ═══════════════════════════════════════════════════════════════════
# Gate 5: Regra 1 — projeção 30d do candidato > baseline (Lei 5)
# ═══════════════════════════════════════════════════════════════════

def regra1_gate(
    candidate_projection_30d: float,
    baseline_projection_30d: float,
    thresholds: dict | None = None,
) -> GateResult:
    """Valida Regra 1: candidato deve melhorar a projeção 30d vs baseline.

    Lei 5 (AGI iterates until profitable): uma mudança só é aplicada se a
    projeção forward 30d do candidato for melhor que a do baseline atual.
    Isto é a versão v4 do _should_apply_changes global (agi_tuning_17h:1637)
    que protegeu contra reverter commits bons.

    Args:
        candidate_projection_30d: projeção R$ do candidato.
        baseline_projection_30d: projeção R$ do config atual.
        thresholds: dict de thresholds.

    Returns:
        GateResult passed=True se candidato > baseline + min_improvement.
    """
    th = thresholds or _DEFAULT_THRESHOLDS
    min_improvement = th["min_30d_projection_improvement"]

    delta = candidate_projection_30d - baseline_projection_30d
    if delta < min_improvement:
        return GateResult(
            passed=False,
            gate_name="regra1",
            reason=f"projeção candidata {candidate_projection_30d:.2f} não melhora "
                   f"baseline {baseline_projection_30d:.2f} "
                   f"(delta={delta:.2f} < min {min_improvement})",
            details={"delta": delta, "candidate": candidate_projection_30d,
                     "baseline": baseline_projection_30d},
        )
    return GateResult(
        passed=True,
        gate_name="regra1",
        details={"delta": delta, "candidate": candidate_projection_30d,
                 "baseline": baseline_projection_30d},
    )
