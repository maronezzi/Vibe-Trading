#!/usr/bin/env python3
"""
scripts/check_symbols_active.py
================================
Auditoria de Integridade de Escopo (Fase 2.3 — Lei 2).

Regra Bruno (2026-07-01): "todos os índices e TFs devem ser testados; apenas
exclua tudo relacionado ao IND (índice cheio — não operado, não testado, nada)."

Valida que TODAS as 16 combinações WIN/BIT/WSP/WDO × M5/M15/M30/H1 estão:
  1. Presentes em `strategy_by_tf` (com estratégia atribuída)
  2. Presentes em `params_by_tf` (com params)
  3. Não listadas em `disabled_symbols` (nível símbolo)
  4. Não listadas em `disabled_timeframes` (nível SYM_TF)
  5. Presentes em `timeframes_by_symbol[sym]`

IND é **completamente ignorado** — não esperado, não validado, não reportado
(mesmo que apareça no config). É o único hard-kill legítimo (Bruno 2026-06-30).

Princípio (Lei 2): este script **NUNCA modifica** o config nem bloqueia trading.
Ele só AUDITA e ALERTA (Telegram). Bruno decide se reativa. Se auto-corrigisse,
violaria a Lei 2 (a AGI/automação não pode desabilitar símbolo/TF).

Uso
---
    python3 scripts/check_symbols_active.py            # audita + Telegram se drift
    python3 scripts/check_symbols_active.py --quiet    # só exit code, sem Telegram
    python3 scripts/check_symbols_active.py --json     # output JSON estruturado

Exit codes: 0 = clean (16/16 ativas), 1 = violations encontradas.
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# sys.path (espelha monitoring/vt_pre_flight.py)
_PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT))
sys.path.insert(0, str(_PROJECT / "core"))

# ── Constantes (Lei 1: definição de escopo, não parâmetro de estratégia) ─────
EXPECTED_SYMBOLS = ["WIN", "BIT", "WSP", "WDO"]   # IND excluído por design
EXPECTED_TIMEFRAMES = ["M5", "M15", "M30", "H1"]
TELEGRAM_TARGET = "telegram:-1004284773048"

log = logging.getLogger("check_symbols")
if not log.handlers:
    logging.basicConfig(level=logging.INFO,
                        format="[%(asctime)s] [SCOPE-AUDIT] %(message)s",
                        datefmt="%H:%M:%S")


# ── Report ──────────────────────────────────────────────────────────────────
@dataclass
class ScopeViolation:
    pair: str               # ex. "BIT_M5"
    kind: str               # missing_strategy | missing_params | disabled_symbol
                            # | disabled_timeframe | missing_from_timeframes_by_symbol
    detail: str


@dataclass
class ScopeReport:
    expected_pairs: List[str] = field(default_factory=list)
    violations: List[ScopeViolation] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.violations

    @property
    def active_count(self) -> int:
        """Número de pares esperados sem violação."""
        violated_pairs = {v.pair for v in self.violations}
        return sum(1 for p in self.expected_pairs if p not in violated_pairs)

    def to_dict(self) -> dict:
        return {
            "expected_count": len(self.expected_pairs),
            "active_count": self.active_count,
            "violation_count": len(self.violations),
            "clean": self.clean,
            "violations": [
                {"pair": v.pair, "kind": v.kind, "detail": v.detail}
                for v in self.violations
            ],
        }


# ── Auditoria (pura — recebe config, retorna report) ───────────────────────
def audit_scope(config: dict,
                expected_symbols: Optional[List[str]] = None,
                expected_timeframes: Optional[List[str]] = None) -> ScopeReport:
    """Audita o config contra o escopo esperado (16 pares, exclui IND).

    Lei 2: nunca modifica o config. Só reporta violações.
    IND é ignorado completamente (não esperado, não validado).
    """
    syms = expected_symbols or EXPECTED_SYMBOLS
    tfs = expected_timeframes or EXPECTED_TIMEFRAMES
    expected_pairs = [f"{s}_{t}" for s in syms for t in tfs]

    strategy_by_tf = config.get("strategy_by_tf", {}) or {}
    params_by_tf = config.get("params_by_tf", {}) or {}
    disabled_symbols = set(config.get("disabled_symbols", []) or [])
    disabled_timeframes = set(config.get("disabled_timeframes", []) or [])
    timeframes_by_symbol = config.get("timeframes_by_symbol", {}) or {}

    report = ScopeReport(expected_pairs=expected_pairs)

    for sym in syms:
        sym_disabled = sym in disabled_symbols
        sym_tfs = set(timeframes_by_symbol.get(sym, []) or [])
        for tf in tfs:
            pair = f"{sym}_{tf}"
            # 1. estratégia atribuída
            strat = strategy_by_tf.get(pair)
            if not strat:
                report.violations.append(ScopeViolation(
                    pair, "missing_strategy",
                    f"{pair} sem strategy em strategy_by_tf"))
            # 2. params presentes
            if pair not in params_by_tf:
                report.violations.append(ScopeViolation(
                    pair, "missing_params",
                    f"{pair} sem params em params_by_tf"))
            # 3. símbolo desabilitado
            if sym_disabled:
                report.violations.append(ScopeViolation(
                    pair, "disabled_symbol",
                    f"{sym} está em disabled_symbols (Lei 2: deve estar ativo)"))
            # 4. TF desabilitado
            if pair in disabled_timeframes:
                report.violations.append(ScopeViolation(
                    pair, "disabled_timeframe",
                    f"{pair} está em disabled_timeframes (Lei 2: deve estar ativo)"))
            # 5. ausente de timeframes_by_symbol
            if tf not in sym_tfs:
                report.violations.append(ScopeViolation(
                    pair, "missing_from_timeframes_by_symbol",
                    f"{tf} ausente de timeframes_by_symbol[{sym}]"))

    return report


# ── Telegram ────────────────────────────────────────────────────────────────
def _notify_telegram(msg: str) -> bool:
    """Envia alerta via hermes. Nunca levanta."""
    try:
        from core.vt_hermes_helper import hermes_send
        return hermes_send(TELEGRAM_TARGET, msg)
    except Exception as e:  # pragma: no cover
        log.error("notify falhou: %s", e)
        return False


def format_alert(report: ScopeReport) -> str:
    """Formata relatório de violações para Telegram."""
    lines = [
        f"⚠️ [SCOPE-AUDIT] Lei 2 — Integridade de Escopo",
        f"{report.active_count}/{len(report.expected_pairs)} pares ativos "
        f"({len(report.violations)} violação(ões)):",
        "",
    ]
    # Agrupa por par para leitura
    by_pair: Dict[str, List[ScopeViolation]] = {}
    for v in report.violations:
        by_pair.setdefault(v.pair, []).append(v)
    for pair in sorted(by_pair):
        kinds = ", ".join(v.kind for v in by_pair[pair])
        lines.append(f"• {pair}: {kinds}")
    lines.append("")
    lines.append("Bruno decide se reativa. Auditoria NÃO modifica config (Lei 2).")
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────────────
def run(config: Optional[dict] = None, alert: bool = True) -> ScopeReport:
    """Roda auditoria. Se config=None, lê do vt_config.json.

    Args:
        config: config dict (testes). None → load_config().
        alert: se True e houver violações, envia Telegram.
    """
    if config is None:
        try:
            from core.vt_config_loader import load_config
            config = load_config()
        except Exception as e:
            log.error("falha ao ler config: %s", e)
            return ScopeReport()  # vazio, não saudável nem reportável

    report = audit_scope(config)

    if not report.clean and alert:
        _notify_telegram(format_alert(report))

    return report


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    quiet = "--quiet" in argv
    as_json = "--json" in argv

    report = run(alert=not quiet)

    if as_json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    elif not quiet:
        if report.clean:
            print(f"✅ [SCOPE-AUDIT] {report.active_count}/"
                  f"{len(report.expected_pairs)} pares ativos. Nenhuma violação.")
        else:
            print(format_alert(report))

    return 0 if report.clean else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
