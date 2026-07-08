"""
stage6_report.py — Relatório Telegram + audit JSON.

Gera 2 saídas:
  1. /tmp/vt_agi_v4_audit.json — audit completo (forensics): cada stage,
     cada candidato testado, gates que passaram/falharam, mudanças aplicadas.
  2. Telegram (via notify_telegram/hermes_send) — resumo humano-legível:
     o que mudou, projeção, gates.

Reusa helpers existentes (Lei 1: não duplica infraestrutura de notificação):
  - core.vt_hermes_helper.hermes_send (Telegram + LLM bridge)
  - TELEGRAM_TARGET do autotrader (thread-id :1 evita anti-loop do hermes)

Fail-safe: se Telegram/audit falhar, NÃO derruba a AGI (loga + segue).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger("agi_v4.stage6")

# Audit JSON path (separado do audit antigo /tmp/vt_agi_audit.json)
AUDIT_PATH = Path("/tmp/vt_agi_v4_audit.json")

# Telegram target — mesmo do autotrader (thread :1 evita anti-loop hermes)
TELEGRAM_TARGET = "telegram:-1004284773048:1"


def run(ctx: dict) -> dict:
    """Gera relatório Telegram + escreve audit JSON.

    Args:
        ctx: contexto completo do pipeline.

    Returns:
        dict com "audit_path" e "summary".
    """
    # 1. Audit JSON (sempre — forensics)
    audit_path = _write_audit(ctx)

    # 2. Telegram (best-effort)
    telegram_sent = _send_telegram(ctx)

    applied = len(ctx.get("applied_changes", []))
    rejected = len(ctx.get("rejected_changes", []))
    converged = ctx.get("converged", False)
    n_failing = len(ctx.get("failing_pairs", []))

    summary = (f"audit={audit_path.name}, telegram={'ok' if telegram_sent else 'skip'}, "
               f"applied={applied} rejected={rejected} "
               f"converged={converged} failing={n_failing}")
    return {"audit_path": str(audit_path), "summary": summary}


# ═══════════════════════════════════════════════════════════════════
# Audit JSON — registro completo para forensics
# ═══════════════════════════════════════════════════════════════════

def _write_audit(ctx: dict) -> Path:
    """Escreve audit JSON completo. Fail-safe: nunca levanta."""
    audit = {
        "tag": ctx.get("tag", "W871"),
        "version": "4.0",
        "started_at": ctx.get("started_at"),
        "ended_at": ctx.get("ended_at"),
        "duration_s": round(ctx.get("duration_s", 0), 2),
        "days": ctx.get("days"),
        "dry_run": ctx.get("dry_run"),
        "max_iterations": ctx.get("max_iterations"),
        "converged": ctx.get("converged", False),
        "failing_pairs": ctx.get("failing_pairs", []),
        "performance_summary": _summarize_performance(ctx),
        "search_results": ctx.get("search_results", []),
        "generated_strategies": ctx.get("generated_strategies", []),
        "applied_changes": ctx.get("applied_changes", []),
        "rejected_changes": ctx.get("rejected_changes", []),
        "stage_audit": ctx.get("audit", []),
    }
    try:
        AUDIT_PATH.write_text(json.dumps(audit, ensure_ascii=False, indent=2, default=str),
                              encoding="utf-8")
        log.info(f"Audit escrito: {AUDIT_PATH}")
    except Exception as e:
        log.error(f"Falha ao escrever audit {AUDIT_PATH}: {e}")
    return AUDIT_PATH


def _summarize_performance(ctx: dict) -> dict:
    """Extrai resumo compacto da performance para o audit."""
    perf = ctx.get("performance", {})
    by_symbol = perf.get("by_symbol", {}) if isinstance(perf, dict) else {}
    return {
        sym: {
            "n_trades": s.get("n_trades", 0),
            "win_rate": s.get("win_rate", 0),
            "total_pnl": s.get("total_pnl", 0),
        }
        for sym, s in by_symbol.items()
        if isinstance(s, dict)
    }


# ═══════════════════════════════════════════════════════════════════
# Telegram — resumo humano-legível
# ═══════════════════════════════════════════════════════════════════

def _send_telegram(ctx: dict) -> bool:
    """Envia resumo ao Telegram. Fail-safe: retorna False se falhar."""
    try:
        from core.vt_hermes_helper import hermes_send
    except ImportError:
        log.debug("vt_hermes_helper não disponível — Telegram skip")
        return False

    msg = _build_telegram_message(ctx)
    if not msg:
        return False

    try:
        return hermes_send(TELEGRAM_TARGET, msg, timeout=30)
    except Exception as e:
        log.warning(f"Telegram falhou (não crítico): {e}")
        return False


def _build_telegram_message(ctx: dict) -> str:
    """Constrói mensagem Telegram compacta e informativa."""
    converged = ctx.get("converged", False)
    applied = ctx.get("applied_changes", [])
    rejected = ctx.get("rejected_changes", [])
    failing = ctx.get("failing_pairs", [])
    dry_run = ctx.get("dry_run", True)
    perf = ctx.get("performance", {})
    by_symbol = perf.get("by_symbol", {}) if isinstance(perf, dict) else {}

    mode = "🔍 DRY-RUN" if dry_run else "⚡ APLICADO"
    icon = "✅" if converged else "🔄"
    ts = datetime.now().strftime("%H:%M:%S")

    lines = [
        f"{icon} *AGI v4 — {mode}*",
        f"• Convergiu: {'sim' if converged else 'não (ainda iterando)'} | {ts}",
    ]

    # Performance resumida por símbolo
    if by_symbol:
        perf_strs = []
        for sym, s in sorted(by_symbol.items()):
            if isinstance(s, dict):
                pnl = s.get("total_pnl", 0)
                emoji = "🟢" if pnl >= 0 else "🔴"
                perf_strs.append(f"{sym} {emoji}R$ {pnl:.0f} ({s.get('n_trades',0)}t)")
        if perf_strs:
            lines.append(f"• PnL 7d: {' | '.join(perf_strs[:4])}")

    # Mudanças
    if applied:
        lines.append(f"• ✅ {len(applied)} mudança(s) aprovada(s):")
        for a in applied[:3]:
            ch = a.get("change", {})
            c = a.get("candidate", {})
            lines.append(f"  → {ch.get('pair','')} {ch.get('strategy','')} "
                        f"(PF {c.get('backtest_result',{}).get('pf',0):.2f})")

    if rejected:
        gates = {}
        for r in rejected:
            g = r.get("gate", "?")
            gates[g] = gates.get(g, 0) + 1
        gates_str = ", ".join(f"{g}:{n}" for g, n in gates.items())
        lines.append(f"• ❌ {len(rejected)} rejeitada(s) por gates ({gates_str})")

    if failing and not converged:
        pair_strs = [f["pair"] if isinstance(f, dict) else f for f in failing[:5]]
        lines.append(f"• 🔻 {len(failing)} par(es) perdedor(es): {', '.join(pair_strs)}")

    lines.append(f"• Audit: /tmp/vt_agi_v4_audit.json")

    return "\n".join(lines)
