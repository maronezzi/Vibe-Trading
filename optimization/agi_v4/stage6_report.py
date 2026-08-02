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


def _shadow_today_summary() -> str | None:
    """Lê forward_sim_trades (walker shadow) do pregão atual e retorna resumo.

    O forward_walker simula o config atual contra barras LIVE sem enviar ordens.
    Este é um sinal SHADOW soft: mostra como o config vigente está indo hoje
    (até agora). NÃO bloqueia nem decide — só contexto no relatório.

    Returns:
        String compacta (ex. "👁 shadow hoje: WIN -R$80 (5t) | WDO +R$40 (3t)")
        ou None se não houver dados shadow hoje (walker não rodou / sem trades).
    """
    import sqlite3
    try:
        db = Path(__file__).resolve().parent.parent.parent / "vt_trades.db"
        if not db.exists():
            return None
        con = sqlite3.connect(str(db))
        try:
            cutoff = datetime.now().strftime("%Y-%m-%d")
            rows = con.execute(
                "SELECT substr(symbol,1,3) AS root, COUNT(*), "
                "ROUND(SUM(net_pnl_brl),2) "
                "FROM forward_sim_trades "
                "WHERE exit_time IS NOT NULL AND entry_time >= ? "
                "GROUP BY root ORDER BY root",
                (cutoff,),
            ).fetchall()
        finally:
            con.close()
    except Exception as e:
        log.debug(f"shadow summary indisponível: {e}")
        return None

    if not rows:
        return None
    parts = []
    for root, n, pnl in rows:
        icon = "🟢" if (pnl or 0) >= 0 else "🔴"
        parts.append(f"{root} {icon}R$ {pnl or 0:.0f} ({n}t)")
    return "👁 shadow hoje: " + " | ".join(parts)


# ═══════════════════════════════════════════════════════════════════
# Telegram — resumo humano-legível
# ═══════════════════════════════════════════════════════════════════

def _send_telegram(ctx: dict) -> bool:
    """Envia resumo ao Telegram. Fail-safe: retorna False se falhar.

    Wave noturno-generoso (Bruno 01/08): retry duplo com backoff. O cron roda
    às 17:10 desassistido — se a primeira tentativa falha (rede, hermes
    cold-start), retenta 2x com 10s de pausa. Antes, uma falha isolada
    silenciava a notificação da noite inteira.
    """
    import time as _time
    try:
        from core.vt_hermes_helper import hermes_send
    except ImportError:
        log.debug("vt_hermes_helper não disponível — Telegram skip")
        return False

    msg = _build_telegram_message(ctx)
    if not msg:
        return False

    # 3 tentativas com 10s de backoff (total ~20s de espera máxima).
    for attempt in range(1, 4):
        try:
            ok = hermes_send(TELEGRAM_TARGET, msg, timeout=30)
            if ok:
                if attempt > 1:
                    log.info(f"Telegram enviado na tentativa {attempt}")
                return True
            log.warning(f"Telegram tentativa {attempt}/3 falhou (hermes retornou False)")
        except Exception as e:
            log.warning(f"Telegram tentativa {attempt}/3 exceção: {e}")
        if attempt < 3:
            _time.sleep(10)

    log.warning("Telegram: 3 tentativas falharam — notificação perdida (não crítico)")
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

    # Wave AGI-soberano (01/08): decisões de entra/sai do AGI.
    reactivated = ctx.get("reactivated", [])
    deactivated = ctx.get("deactivated", [])

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

    # Sinal SHADOW do pregão atual (forward_walker, soft — não decide).
    shadow = _shadow_today_summary()
    if shadow:
        lines.append(f"• {shadow}")

    # ── Decisões soberanas do AGI (entra/sai) ──
    # Wave AGI-soberano (01/08): o AGI decide quais pares operam.
    if reactivated:
        pairs_str = ", ".join(reactivated[:6])
        mais = f" (+{len(reactivated)-6})" if len(reactivated) > 6 else ""
        lines.append(f"• 🔓 REATIVOU {len(reactivated)} par(es) lucrativo(s): {pairs_str}{mais}")
    if deactivated:
        pairs_str = ", ".join(deactivated[:6])
        mais = f" (+{len(deactivated)-6})" if len(deactivated) > 6 else ""
        lines.append(f"• 🔒 DESATIVOU {len(deactivated)} par(es) failing: {pairs_str}{mais}")

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
