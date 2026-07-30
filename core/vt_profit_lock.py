"""Profit Lock Adaptativo (Wave 880.H — Bruno 2026-07-20).

Quando o PnL diário (realizado + flutuante) atinge um target, fecha tudo
a mercado, realiza o lucro e bloqueia novas entradas até o dia seguinte.
Defesa contra "o mercado comer o lucro do dia", como aconteceu em 20/07
(de +R$XXX para −R$434).

Target é adaptativo: média do PnL dos dias POSITIVOS recentes, com mínimo
garantido. Se não houver histórico (DB vazio / < 2 dias positivos), usa o
mínimo. Funciona desde o dia 1.

Semântica (decisões Bruno):
  - PnL contado: realizado + flutuante (mark-to-market via saldo MT5).
  - Ao atingir: fecha tudo + bloqueia novas.
  - Liberação: automática no dia seguinte (date field no state file).

State persistente em /tmp/vt_profit_lock.json (sobrevive a restart).
Day-rollover via campo "date" (mesmo padrão de vt_starting_balance e
/tmp/vt_block_counter.json).

API pública:
  - get_target(config) -> float
  - get_intraday_pnl_total() -> float
  - is_locked() -> (locked: bool, state: dict)
  - arm_lock(target, armed_pnl, closed_n) -> None
  - release_lock() -> None
"""
import json
import logging
import os
import sqlite3
import tempfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Tuple

# ─── Constantes ────────────────────────────────────────────────────────────
LOCK_STATE_PATH = Path("/tmp/vt_profit_lock.json")
DB_PATH = Path("/home/bruno/Projects/Vibe-Trading/vt_trades.db")

# Defaults (override via config: profit_lock_min_target, profit_lock_target_mult,
# profit_lock_lookback_days). Mínimo R$ 200-300 conforme Bruno.
DEFAULT_MIN_TARGET = 250.0
DEFAULT_TARGET_MULT = 1.0
DEFAULT_LOOKBACK_DAYS = 7
# Mínimo de dias positivos no lookback pra usar a média (abaixo disso, fallback).
MIN_POSITIVE_DAYS_FOR_AVG = 2

_log = logging.getLogger(__name__)


def _today_str() -> str:
    """Data local no formato YYYY-MM-DD. Mesmo formato de daily_summary.date."""
    return datetime.now().strftime("%Y-%m-%d")


# ─── Cálculo do target adaptativo ──────────────────────────────────────────
def _query_recent_positive_pnls(db_path: Path, lookback_days: int) -> list:
    """PnL líquido por dia dos últimos N dias (apenas dias com net_pnl > 0).

    Usa daily_summary (UNIQUE(date, symbol)) somando por data. Filtra só dias
    positivos. Retorna lista de floats (já em R$).
    """
    try:
        con = sqlite3.connect(str(db_path), timeout=3.0)
        try:
            # Soma net_pnl por date, últimos lookback_days, só positivos.
            rows = con.execute(
                """
                SELECT date, SUM(net_pnl) as day_pnl
                FROM daily_summary
                WHERE date >= date('now', ?)
                  AND date != ?      -- exclui hoje (não conta o dia correndo)
                GROUP BY date
                HAVING day_pnl > 0
                ORDER BY date DESC
                """,
                (f"-{lookback_days} days", _today_str()),
            ).fetchall()
        finally:
            con.close()
        return [float(r[1]) for r in rows if r[1] is not None]
    except sqlite3.Error as e:
        _log.warning("vt_profit_lock: falha lendo daily_summary: %s", e)
        return []


def get_target(config: dict) -> float:
    """Calcula target adaptativo (R$). Sempre ≥ min_target.

    Fórmula: max(min_target, média_pnls_positivos_recentes × mult).
    Fallback: min_target se < MIN_POSITIVE_DAYS_FOR_AVG dias positivos.
    """
    min_target = float(config.get("profit_lock_min_target", DEFAULT_MIN_TARGET))
    mult = float(config.get("profit_lock_target_mult", DEFAULT_TARGET_MULT))
    lookback = int(config.get("profit_lock_lookback_days", DEFAULT_LOOKBACK_DAYS))

    positive_pnls = _query_recent_positive_pnls(DB_PATH, lookback)
    if len(positive_pnls) < MIN_POSITIVE_DAYS_FOR_AVG:
        # Histórico insuficiente — fallback ao mínimo. Garante dia 1.
        return min_target

    avg = sum(positive_pnls) / len(positive_pnls)
    target = max(min_target, avg * mult)
    return round(target, 2)


# ─── PnL intradiário (realizado + flutuante) ───────────────────────────────
def get_intraday_pnl_total() -> float:
    """PnL total do dia: saldo MT5 atual − saldo inicial do dia.

    Inclui realizado (deals fechados) + flutuante (posições abertas), porque
    o saldo do MT5 já reflete equity realizado e o equity captura o flutuante.
    Mesma fonte do fallback de vt_truth.get_daily_pnl() (linha ~425), mas sem
    depender de history_deals_get (que está quebrado — ver Wave 880.G/C).

    Fail-safe: retorna 0.0 se MT5 ou snapshot indisponível (não levanta).
    Um PnL 0 nunca dispara o target — melhor não travar por engano.
    """
    try:
        from core import vt_starting_balance
        starting = vt_starting_balance.get_today_starting_balance()
        if starting is None:
            _log.debug("vt_profit_lock: sem starting_balance pra hoje")
            return 0.0
        starting = float(starting)

        # Saldo realizado + flutuante = equity do MT5.
        # equity reflete o que a conta vale AGORA (realizado + MTM das abertas).
        from mt5 import mt5_orchestrator as _mt5o
        st = _mt5o.status()
        # Preferência: equity (inclui flutuante). Fallback: balance.
        acc = st.get("account", {}) if isinstance(st, dict) else {}
        current = float(acc.get("equity") or acc.get("balance") or 0)
        if current == 0:
            return 0.0

        return round(current - starting, 2)
    except Exception as e:
        _log.warning("vt_profit_lock: get_intraday_pnl_total falhou: %s", e)
        return 0.0


# ─── Estado persistente (lock armado/desarmado) ────────────────────────────
def _read_state() -> dict:
    """Lê state file. Retorna {} se ausente/malformado (fail-safe: desarmado)."""
    try:
        if not LOCK_STATE_PATH.exists():
            return {}
        raw = LOCK_STATE_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}
        return data
    except (OSError, json.JSONDecodeError) as e:
        _log.warning("vt_profit_lock: state ilegível (%s): %s",
                     type(e).__name__, e)
        return {}


def _atomic_write_state(data: dict) -> None:
    """Escrita atômica (tempfile + os.replace). Mesmo padrão de vt_starting_balance."""
    LOCK_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(LOCK_STATE_PATH.parent),
        prefix=".vt_profit_lock.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(LOCK_STATE_PATH))
    except OSError as e:
        _log.warning("vt_profit_lock: falha gravando state: %s", e)
        try:
            Path(tmp).unlink(missing_ok=True)
        except OSError:
            pass


def is_locked() -> Tuple[bool, dict]:
    """(locked, state). Auto-release se state.date != hoje.

    Retorna (True, state) só se armado E date == hoje. Qualquer outro caso
    (sem arquivo, data antiga, malformado) retorna (False, {}).
    """
    state = _read_state()
    if not state.get("armed"):
        return (False, state)
    if state.get("date") != _today_str():
        # Lock de dia anterior — expirou. Limpa o arquivo.
        # (Não chamamos release_lock() pra evitar log spam a cada tick; o
        # arquivo velho é inerte porque date != today.)
        return (False, state)
    return (True, state)


def arm_lock(target: float, armed_pnl: float, closed_n: int) -> None:
    """Marca o lock no state file. Não fecha posições (isso é com o daemon).

    Idempotente no mesmo dia: se já armado hoje, atualiza armed_pnl/closed_n
    mas não muda o target (preserva o primeiro critério que disparou).
    """
    state = _read_state()
    today = _today_str()
    if state.get("date") == today and state.get("armed"):
        # Já armado hoje — só atualiza contadores, mantém target original.
        state["armed_pnl"] = armed_pnl
        state["closed_n"] = state.get("closed_n", 0) + closed_n
        state["updated_at"] = datetime.now().isoformat()
    else:
        state = {
            "date": today,
            "armed": True,
            "target": float(target),
            "armed_at": datetime.now().isoformat(),
            "armed_pnl": float(armed_pnl),
            "closed_n": int(closed_n),
        }
    _atomic_write_state(state)


def release_lock() -> None:
    """Desarma explicitamente (remove o state file).

    Normalmente desnecessário: o day-rollover em is_locked() cuida da
    liberação automática. Útil para desarmar manualmente no mesmo dia
    (ex: Bruno decide retomar antes do EOD).
    """
    try:
        LOCK_STATE_PATH.unlink(missing_ok=True)
    except OSError as e:
        _log.warning("vt_profit_lock: falha removendo state: %s", e)


# ─── Helpers de parsing (defensivos) ───────────────────────────────────────
def _to_float(v, default: float = 0.0) -> float:
    """Converte robustamente p/ float. Decimal-safe."""
    try:
        return float(Decimal(str(v)))
    except (InvalidOperation, TypeError, ValueError):
        return default
