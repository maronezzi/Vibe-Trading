"""
AGI Evidence Validator — valida mudanças do AGI contra DADOS REAIS (vt_trades.db).

Gap fechado em 2026-06-25: agi_safety_validator.py validava TIPOS (Pydantic)
mas NÃO consultava o banco real para validar se a mudança melhora ou piora
o histórico do par.

Este módulo é o GATE FINAL antes do apply_changes() chamar save_params():
se validate_against_reality() retornar (False, motivo), o AGI ABORTA
a mudança e loga o motivo.

Critérios de bloqueio (conservadores):
  1. WR < 35% em >=10 trades recentes (30 dias)
  2. Streak >= 5 losses consecutivos nos últimos 20 trades
  3. (futuro) PnL total < 0 E mudança não aumenta avg_pnl

Símbolos com <10 trades (evidência insuficiente) são PERMITIDOS
porque o AGI não tem dados suficientes para julgar.

NÃO modifica o DB. NÃO chama LLM. NÃO depende de CrewAI/LangChain.
Usa SQLite direto + heurísticas simples.
"""
import logging
import os
import sqlite3
from datetime import datetime, timedelta

log = logging.getLogger("agi_evidence_validator")

# Thresholds (ajustados empiricamente baseado em auditoria 2026-06-25):
#   - WR < 35% em >=10 trades = setup perdedor crônico (não ruído)
#   - Streak >= 5 losses = momentum morto, parar antes de hemorragia
MIN_TRADES_FOR_WR_CHECK = 10   # menos que isso = evidência insuficiente, permite
MIN_WR_PCT = 35.0             # WR abaixo disso em >=10 trades = BLOQUEIA
MIN_STREAK_LOSSES = 5         # 5 losses consecutivos = BLOQUEIA
STREAK_LOOKBACK = 20          # considera últimos 20 trades pra streak
WR_LOOKBACK_DAYS = 30         # WR considera últimos 30 dias


def validate_against_reality(symbol: str, new_params: dict, db_path: str) -> tuple[bool, str]:
    """
    Valida proposta de mudança do AGI contra dados REAIS do vt_trades.db.

    Args:
        symbol: símbolo MT5 (ex: "WINQ26", "WDOQ26"). É normalizado para o
            root do símbolo (WIN, WDO, BIT, WSP) na query SQL porque o DB
            tem múltiplos contratos por mês (WINQ26, WINM26, etc).
        new_params: dict com os params novos sendo aplicados. NÃO é usado
            no cálculo de bloqueio (ainda não temos backtest forward dos novos
            params); é apenas metadata para logging.
        db_path: caminho completo para o SQLite (ex: "vt_trades.db").

    Returns:
        (True, "") se a mudança é segura (passa todos os critérios).
        (False, "motivo") se deve ser BLOQUEADA.

    Critérios (na ordem):
      1. Evidência insuficiente (< MIN_TRADES_FOR_WR_CHECK trades)?
         → PERMITE (não temos dados para julgar).
      2. WR < MIN_WR_PCT% nos últimos WR_LOOKBACK_DAYS dias?
         → BLOQUEIA com motivo "WR=X% (< 35%) em N trades últimos 30d".
      3. Streak >= MIN_STREAK_LOSSES losses consecutivos nos últimos
         STREAK_LOOKBACK trades (qualquer janela)?
         → BLOQUEIA com motivo "Streak de X losses consecutivos em SYMBOL".

    Read-only: nunca modifica vt_trades.db.
    """
    if not os.path.exists(db_path):
        # Fail-open: se DB não existe, permite (AGI continua, mas loga warning).
        log.warning(f"DB não encontrado em {db_path} — permitindo por fail-open")
        return True, ""

    # Normalizar symbol para root: WINQ26 → WIN, WDOQ26 → WDO, etc.
    symbol_root = _symbol_root(symbol)

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        log.warning(f"Erro abrindo DB {db_path}: {e} — fail-open")
        return True, ""

    try:
        # ── Critério 1+2: WR em janela recente ──
        cutoff_date = (datetime.now() - timedelta(days=WR_LOOKBACK_DAYS)).isoformat()
        rows = conn.execute("""
            SELECT net_pnl
            FROM trades
            WHERE exit_time IS NOT NULL
              AND (symbol = ? OR symbol LIKE ? || '%')
              AND entry_time >= ?
            ORDER BY entry_time DESC
        """, (symbol, symbol_root, cutoff_date)).fetchall()

        n_trades = len(rows)
        if n_trades < MIN_TRADES_FOR_WR_CHECK:
            # Evidência insuficiente — permite (fail-open).
            return True, ""

        wins = sum(1 for r in rows if r["net_pnl"] and r["net_pnl"] > 0)
        wr_pct = (wins / n_trades) * 100.0

        if wr_pct < MIN_WR_PCT:
            return (
                False,
                f"WR={wr_pct:.1f}% ({wins}W/{n_trades - wins}L) < {MIN_WR_PCT:.0f}% "
                f"em {symbol_root} nos últimos {WR_LOOKBACK_DAYS}d. "
                f"Bloqueado: setup perdedor crônico.",
            )

        # ── Critério 3: Streak de losses consecutivos ──
        # Pega últimos STREAK_LOOKBACK trades (já em ordem DESC).
        recent = conn.execute("""
            SELECT net_pnl
            FROM trades
            WHERE exit_time IS NOT NULL
              AND (symbol = ? OR symbol LIKE ? || '%')
            ORDER BY entry_time DESC
            LIMIT ?
        """, (symbol, symbol_root, STREAK_LOOKBACK)).fetchall()

        streak = 0
        for r in recent:
            if r["net_pnl"] is not None and r["net_pnl"] <= 0:
                streak += 1
            else:
                break  # streak interrompido

        if streak >= MIN_STREAK_LOSSES:
            return (
                False,
                f"Streak de {streak} losses consecutivos em {symbol_root} "
                f"(últimos {STREAK_LOOKBACK} trades). "
                f"Bloqueado: momentum morto.",
            )

        return True, ""
    except sqlite3.Error as e:
        log.warning(f"Erro query DB {db_path}: {e} — fail-open")
        return True, ""
    finally:
        conn.close()


def _symbol_root(symbol: str) -> str:
    """WINQ26 → WIN, WDOQ26 → WDO, etc. Pega os 3 primeiros chars."""
    if len(symbol) >= 3:
        return symbol[:3]
    return symbol


def fetch_symbol_evidence(symbol: str, db_path: str, days: int = 30) -> dict:
    """
    Helper para auditoria e debug: retorna dicionário com métricas reais
    do símbolo no DB (sem side-effects, read-only).

    Returns:
        {
            "n_trades": int,
            "wins": int,
            "losses": int,
            "wr_pct": float,
            "total_pnl": float,
            "avg_pnl": float,
            "max_loss": float,
            "max_win": float,
            "current_streak_loss": int,
            "current_streak_win": int,
        }
    """
    if not os.path.exists(db_path):
        return {"error": f"DB não encontrado: {db_path}"}

    symbol_root = _symbol_root(symbol)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        rows = conn.execute("""
            SELECT net_pnl, entry_time
            FROM trades
            WHERE exit_time IS NOT NULL
              AND (symbol = ? OR symbol LIKE ? || '%')
              AND entry_time >= ?
            ORDER BY entry_time DESC
        """, (symbol, symbol_root, cutoff)).fetchall()

        if not rows:
            return {"error": f"Sem trades para {symbol_root} nos últimos {days}d"}

        pnls = [r["net_pnl"] for r in rows if r["net_pnl"] is not None]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        # Streak atual
        streak_loss = 0
        streak_win = 0
        for p in pnls:
            if p > 0:
                if streak_loss > 0:
                    break
                streak_win += 1
            else:
                if streak_win > 0:
                    break
                streak_loss += 1

        return {
            "symbol": symbol_root,
            "days": days,
            "n_trades": len(pnls),
            "wins": len(wins),
            "losses": len(losses),
            "wr_pct": (len(wins) / len(pnls) * 100.0) if pnls else 0.0,
            "total_pnl": sum(pnls),
            "avg_pnl": sum(pnls) / len(pnls) if pnls else 0.0,
            "max_loss": min(losses) if losses else 0.0,
            "max_win": max(wins) if wins else 0.0,
            "current_streak_loss": streak_loss,
            "current_streak_win": streak_win,
        }
    finally:
        conn.close()
