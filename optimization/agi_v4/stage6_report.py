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
        "rollover_state": ctx.get("rollover_state", {}),
        "series_sanity": ctx.get("series_sanity", {}),
        "risk_calibration": ctx.get("risk_calibration", {}),
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


def send_brief(msg: str, retries: int = 0) -> bool:
    """Envia mensagem Telegram curta (lifecycle: START/FATAL). Fail-safe.

    Wave AGI-alerts (Bruno 12/08): notificações de lifecycle do runner (início da
    run e crash). Diferente do relatório final (montado por _build_telegram_message
    + _send_telegram com 3 tentativas), esta envia texto direto sem montagem.

    Args:
        msg: texto a enviar (manter curto — <4000 chars, o limite do Telegram).
        retries: tentativas extras com 10s de backoff. START usa 0 (não-bloqueante);
            FATAL usa 2 (importante — vale o backoff). Default 0.

    Returns:
        True se enviou, False caso contrário. NUNCA levanta.
    """
    import time as _time
    try:
        from core.vt_hermes_helper import hermes_send
    except ImportError:
        log.debug("vt_hermes_helper não disponível — send_brief skip")
        return False

    for attempt in range(1, retries + 2):  # 1 + retries tentativas
        try:
            if hermes_send(TELEGRAM_TARGET, msg, timeout=30):
                return True
            log.warning(f"send_brief tentativa {attempt}/{retries+1} falhou")
        except Exception as e:
            log.warning(f"send_brief tentativa {attempt}/{retries+1} exceção: {e}")
        if attempt <= retries:
            _time.sleep(10)
    return False


def _build_telegram_message(ctx: dict) -> str:
    """Constrói mensagem Telegram compacta e informativa."""
    # Wave AGI-alerts (Bruno 12/08): relatório retrata o run INTEIRO — estratégias
    # geradas (Stage 4), cross-pair salvages, otimização de lucrativos, condição
    # de término real (convergiu/estagnou/deadline), métricas completas dos
    # applied e acumulador de todas as mudanças (não só a última chamada do S5).
    converged = ctx.get("converged", False)
    stagnated = ctx.get("stagnated", False)
    deadline_hit = ctx.get("deadline_hit", False)
    iters = ctx.get("current_iteration", 0)
    duration_s = ctx.get("duration_s", 0) or 0
    failing = ctx.get("failing_pairs", [])
    dry_run = ctx.get("dry_run", True)
    perf = ctx.get("performance", {})
    by_symbol = perf.get("by_symbol", {}) if isinstance(perf, dict) else {}

    # Wave AGI-soberano (01/08): decisões de entra/sai do AGI.
    reactivated = ctx.get("reactivated", [])
    deactivated = ctx.get("deactivated", [])

    # ── Condição de término (1c): distinguir convergiu/estagnou/deadline ──
    if stagnated:
        icon, term = "🔄", f"ESTAGNOU ({iters} it)"
    elif deadline_hit:
        icon, term = "⏰", f"DEADLINE ({iters} it)"
    elif converged:
        icon, term = "✅", "CONVERGIU"
    else:
        icon, term = "🔄", "ITERANDO"

    # Duração compacta: "12min" ou "1h05".
    dur_min = duration_s / 60.0
    if dur_min >= 60:
        dur_str = f"{int(dur_min // 60)}h{int(dur_min % 60):02d}min"
    else:
        dur_str = f"{dur_min:.0f}min"

    mode = "🔍 DRY-RUN" if dry_run else "⚡ APLICADO"
    ts = datetime.now().strftime("%H:%M:%S")

    lines = [
        f"{icon} *AGI v4 — {mode}*",
        f"• {term} | {dur_str} | {ts}",
    ]

    # ── Banner de dia inválido (2a) ──
    # /tmp/vt_invalid_day.flag → o AGI suprimiu todas as mudanças hoje.
    try:
        import os as _os
        if _os.path.exists("/tmp/vt_invalid_day.flag"):
            lines.append("• 🚫 Dia inválido — mudanças suprimidas")
    except Exception:
        pass

    # ── Estado de rolagem (Wave AGI-rollover 13/08): vencimentos à vista ──
    try:
        _rs = ctx.get("rollover_state") or {}
        _flags = []
        for _st in _rs.values():
            if not isinstance(_st, dict):
                continue
            if _st.get("freeze"):
                _exp = str(_st.get("expiry", "?"))
                _flags.append(f"🧊 {_st.get('symbol')} vence {_exp[-5:]} ({_st.get('days_util')}d úteis)")
            elif _st.get("grace"):
                _flags.append(f"⏳ {_st.get('symbol')} em grace pós-rolagem "
                              f"({_st.get('days_since_rollover')}d)")
        if _flags:
            lines.append("• 📅 Rolagem: " + " | ".join(_flags))
        _div = [f"{s.get('symbol')} Δ{s.get('diff_pts'):+.0f}pts"
                for s in (ctx.get("series_sanity") or {}).values()
                if isinstance(s, dict) and s.get("divergent")]
        if _div:
            lines.append("• ⚠️ Série perpétua DIVERGE do contrato live: "
                         + " | ".join(_div) + " — simulação não representa o live")
    except Exception:
        pass

    # ── Calibração de risco pelo AGI (Wave AGI-super 13/08) ──
    try:
        rc = ctx.get("risk_calibration") or {}
        _parts = []
        for _root, _r in (rc.get("daily_stops") or {}).items():
            if isinstance(_r, dict) and _r.get("status") == "calibrado":
                _mark = "→" + str(_r.get("best")) if _r.get("apply") else "=" + str(int(_r.get("current") or 0))
                _parts.append(f"{_root} {_mark}")
        if _parts:
            lines.append("• 🛑 Stop diário calibrado: " + " | ".join(_parts))
        _tg = rc.get("profit_target") or {}
        if _tg.get("status") == "calibrado":
            _tmark = ("→" + str(_tg.get("best")) if _tg.get("apply")
                      else "=" + str(int(_tg.get("current") or 0)))
            lines.append(f"• 🎯 Alvo diário calibrado: {_tmark} "
                         f"(ganho potencial R${_tg.get('gain', 0):+.0f})")
        _sl = [f"{_root}→{_r.get('best')}pts" for _root, _r in (rc.get("slippage") or {}).items()
               if isinstance(_r, dict) and _r.get("apply")]
        if _sl:
            lines.append("• 🚫 Slippage recalibrado: " + " | ".join(_sl))
    except Exception:
        pass

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

    # ── Estratégias geradas (Stage 4) + cross-pair salvages (2b) ──
    generated = ctx.get("generated_strategies", []) or []
    if generated:
        aprovadas = [g for g in generated if g.get("status") == "approved_pending"]
        aprov_passed = [g for g in aprovadas if g.get("backtest_gate") == "passed"]
        salvages = [g for g in aprov_passed if g.get("winning_pair")]
        # Wave AGI-param-tuning: estratégias com params próprios otimizados
        # (nasciam com params={} antes desta wave; agora vêm já tunadas).
        tunadas = [g for g in aprov_passed if g.get("tuned_params")]
        rejeitadas = [g for g in generated if g.get("status") == "rejected"]
        gates = {}
        for g in rejeitadas:
            gk = g.get("gate", "?")
            gates[gk] = gates.get(gk, 0) + 1
        parts = [f"{len(generated)} gerada(s)", f"{len(aprov_passed)} aprov."]
        if tunadas:
            parts.append(f"{len(tunadas)} otim.")
        if rejeitadas:
            gates_str = ", ".join(f"{g}:{n}" for g, n in sorted(gates.items()))
            parts.append(f"{len(rejeitadas)} rej ({gates_str})")
        lines.append("• 🧪 " + " | ".join(parts))
        for s in salvages[:3]:
            bt = s.get("backtest") or {}
            lines.append(f"   ↩️ {s.get('name','?')} → {s.get('winning_pair','?')} "
                         f"(R$ {bt.get('total_pnl',0):.0f})")
        for t in tunadas[:3]:
            tp = t.get("tuned_params") or {}
            tp_str = ", ".join(f"{k}={v}" for k, v in list(tp.items())[:3])
            lines.append(f"   🔧 {t.get('name','?')} otimizada ({tp_str})")

    # ── Otimização de lucrativos (2c) ──
    profit_opts = ctx.get("profit_optimizations", []) or []
    if profit_opts:
        lines.append(f"• ⬆️ {len(profit_opts)} otimização(ões) em pares lucrativos")

    # ── Sweep _pending/ (Wave AGI-sweep) ──
    # O AGI varre TODO o strategies/_pending/: testa em todos os pares ativos,
    # otimiza params das que têm edge e promove as melhores.
    sweep = ctx.get("sweep_promotions", []) or []
    if sweep:
        pairs_sweep = sorted({(p.get("change") or {}).get("pair", "")
                              for p in sweep if isinstance(p, dict)})
        pairs_str = ", ".join(p for p in pairs_sweep if p)[:60]
        lines.append(f"• 🧹 Sweep _pending/: {len(sweep)} promovida(s) ({pairs_str})")

    # ── Tune incumbents (Wave AGI-tune-incumbents) ──
    # Tuning fino dos params das AGI4 que JÁ OPERAM (não só as novas).
    inc_tunings = ctx.get("incumbent_tunings", []) or []
    if inc_tunings:
        pairs_inc = sorted({(p.get("change") or {}).get("pair", "")
                            for p in inc_tunings if isinstance(p, dict)})
        pairs_str = ", ".join(p for p in pairs_inc if p)[:60]
        lines.append(f"• 🔧 Incumbentes otimizados: {len(inc_tunings)} ({pairs_str})")

    # ── Mudanças aprovadas (2d): accumulator + métricas completas ──
    # all_applied_changes acumula o run inteiro (Stage 5 roda várias vezes).
    # Dedup por par (última vence = estado final do config) para não inflar.
    all_applied = ctx.get("all_applied_changes") or ctx.get("applied_changes") or []
    by_pair = {}
    for a in all_applied:
        pair = (a.get("change") or {}).get("pair", "")
        if pair:
            by_pair[pair] = a
    if by_pair:
        lines.append(f"• ✅ {len(by_pair)} par(es) com nova estratégia:")
        for pair, a in list(by_pair.items())[:5]:
            ch = a.get("change", {})
            bt = ch.get("backtest", {})
            strat = ch.get("strategy", "")
            pf = bt.get("pf", 0)
            wr = bt.get("wr", 0)
            nt = bt.get("n_trades", 0)
            max_dd = bt.get("max_dd", 0)
            base_pnl = ch.get("baseline_simulated_pnl", 0)
            lines.append(f"   → {pair} {strat} (PF {pf:.2f} WR {wr:.0f}% {nt}t "
                         f"maxDD R${max_dd:.0f}) vs base R${base_pnl:.0f}")

    # ── Rejeitadas por gates (accumulator — agora inclui guardrail_reject) ──
    all_rejected = ctx.get("all_rejected_changes") or ctx.get("rejected_changes") or []
    if all_rejected:
        gates = {}
        for r in all_rejected:
            g = r.get("gate", "?")
            gates[g] = gates.get(g, 0) + 1
        gates_str = ", ".join(f"{g}:{n}" for g, n in sorted(gates.items()))
        lines.append(f"• ❌ {len(all_rejected)} rejeitada(s) por gates ({gates_str})")

    if failing and not converged:
        pair_strs = [f["pair"] if isinstance(f, dict) else f for f in failing[:5]]
        lines.append(f"• 🔻 {len(failing)} par(es) perdedor(es): {', '.join(pair_strs)}")

    lines.append("• Audit: /tmp/vt_agi_v4_audit.json")

    return "\n".join(lines)
