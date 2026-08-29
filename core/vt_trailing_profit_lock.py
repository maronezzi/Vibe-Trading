"""Trailing Profit Lock (Wave 1110 — Bruno 2026-07-23).

Protege lucro intradiário com um ratchet progressivo:
  1. Ativa quando PnL atinge activation_pct do target (default 50%).
  2. Registra o pico de PnL (high-water mark).
  3. Calcula floor garantido: floor = peak × trail_factor(progress).
     trail_factor vai de trail_floor_pct (default 0.5) na ativação até
     1.0 quando PnL atinge o target. Interpolação linear.
  4. Se PnL cai abaixo do floor → fecha tudo (TRAILING_STOP_LOSS).
  5. Se PnL atinge target → delega ao profit lock full (comportamento atual).

Não conflita com vt_profit_lock: o trailing é uma camada ANTERIOR.
Se o trailing fecha tudo, o profit lock full nunca dispara (PnL já realizado).
Se o PnL sobe até o target sem cair abaixo do floor, o profit lock full
assume (fecha tudo + bloqueia novas entradas).

Wave 1111 (Bruno 2026-08-11): com o trailing ATIVO (PnL >= 50% do target),
o daemon NÃO abre novas entradas (gate em check_and_trade via is_active()) —
uma entrada nova é vetor de risco que pode derrubar o PnL abaixo do floor
(virando BREACH e fechando tudo, inclusive o lucro acumulado). Posições
abertas seguem gerenciadas; o bloqueio é só de ENTRADAS, mesma semântica do
profit lock full.

State persistente em /tmp/vt_trailing_profit_lock.json (sobrevive restart).
Day-rollover via campo "date".

API pública:
  - get_trailing_state() -> dict
  - update_trailing(pnl, target, config) -> TrailingAction
  - reset_trailing() -> None

TrailingAction:
  - HOLD: nada a fazer
  - TIGHTEN: atualizar floor (logar, não fecha)
  - BREACH: PnL caiu abaixo do floor → fechar tudo
  - TARGET: PnL atingiu target → delegar ao profit lock full
"""
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

_log = logging.getLogger(__name__)

# ─── Constantes ────────────────────────────────────────────────────────────
STATE_PATH = Path("/tmp/vt_trailing_profit_lock.json")

# Defaults (override via config)
DEFAULT_ACTIVATION_PCT = 0.50   # Ativa em 50% do target
DEFAULT_TRAIL_FLOOR_PCT = 0.50  # Na ativação, garante 50% do pico
# trail_factor sobe linearmente de TRAIL_FLOOR_PCT até 1.0 no target


class TrailingAction(Enum):
    HOLD = "hold"          # PnL abaixo da ativação ou acima do floor
    TIGHTEN = "tighten"    # Novo pico → floor sobe (logar)
    BREACH = "breach"      # PnL < floor → fechar tudo
    TARGET = "target"      # PnL >= target → profit lock full


@dataclass
class TrailingDecision:
    action: TrailingAction
    pnl: float = 0.0
    peak: float = 0.0
    floor: float = 0.0
    target: float = 0.0
    progress: float = 0.0    # 0.0 a 1.0 (pnl / target)
    trail_factor: float = 0.0


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ─── State persistence ─────────────────────────────────────────────────────
def _read_state() -> dict:
    try:
        if not STATE_PATH.exists():
            return {}
        raw = STATE_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        _log.warning("vt_trailing_profit_lock: state ilegível: %s", e)
        return {}


def _atomic_write_state(data: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(STATE_PATH.parent),
        prefix=".vt_trailing_pl.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(STATE_PATH))
    except OSError as e:
        _log.warning("vt_trailing_profit_lock: falha gravando state: %s", e)
        try:
            Path(tmp).unlink(missing_ok=True)
        except OSError:
            pass


def get_trailing_state() -> dict:
    """Retorna state atual. Auto-expira se date != hoje."""
    state = _read_state()
    if state.get("date") != _today_str():
        return {}
    return state


def is_active() -> bool:
    """True se o trailing profit lock está engajado HOJE (activated).

    Wave 1111 (Bruno 2026-08-11): o daemon usa isso como gate de novas
    entradas — com o trailing ativo (PnL >= 50% do target), não abre
    posição nova porque uma entrada é vetor de risco que pode derrubar o
    PnL abaixo do floor (BREACH fecha tudo). Day-rollover via "date".
    """
    st = get_trailing_state()
    return bool(st.get("activated"))


def reset_trailing() -> None:
    """Remove state file (reset manual ou day-rollover)."""
    try:
        STATE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


# ─── Core logic ────────────────────────────────────────────────────────────
def _compute_trail_factor(progress: float, floor_pct: float) -> float:
    """Fator de trailing: floor_pct na ativação → 1.0 no target.

    progress = pnl / target (0.0 a 1.0+).
    Na ativação (progress = activation_pct), factor = floor_pct.
    No target (progress = 1.0), factor = 1.0.
    Interpolação linear entre os dois.
    Acima de 1.0: factor = 1.0 (cap).
    """
    activation = DEFAULT_ACTIVATION_PCT  # 0.5
    if progress <= activation:
        return floor_pct
    if progress >= 1.0:
        return 1.0
    # Linear: activation → floor_pct, 1.0 → 1.0
    span = 1.0 - activation  # 0.5
    t = (progress - activation) / span  # 0.0 a 1.0
    return floor_pct + (1.0 - floor_pct) * t


def update_trailing(pnl: float, target: float, config: dict) -> TrailingDecision:
    """Atualiza o trailing profit lock com o PnL atual.

    Chamado a cada tick do daemon (junto com o profit lock full).
    Retorna TrailingDecision com a ação recomendada.

    NÃO fecha posições — o daemon decide o que fazer com a ação.
    """
    activation_pct = float(config.get("trailing_activation_pct", DEFAULT_ACTIVATION_PCT))
    floor_pct = float(config.get("trailing_floor_pct", DEFAULT_TRAIL_FLOOR_PCT))

    if target <= 0:
        return TrailingDecision(action=TrailingAction.HOLD, pnl=pnl, target=target)

    progress = pnl / target
    state = get_trailing_state()

    # ── Caso 1: PnL atingiu o target → delega ao profit lock full
    if pnl >= target and target > 0:
        return TrailingDecision(
            action=TrailingAction.TARGET,
            pnl=pnl, target=target, progress=progress,
        )

    # ── Caso 2: PnL abaixo da ativação → nada a fazer
    if pnl < target * activation_pct:
        # Se estava ativado e PnL caiu abaixo da ativação (mas acima do floor),
        # mantém o trailing ativo (ratchet não desarma).
        if state.get("activated") and state.get("floor", 0) > 0:
            floor = state["floor"]
            if pnl < floor:
                return TrailingDecision(
                    action=TrailingAction.BREACH,
                    pnl=pnl, peak=state.get("peak", 0),
                    floor=floor, target=target, progress=progress,
                    trail_factor=state.get("trail_factor", 0.0),
                )
        return TrailingDecision(
            action=TrailingAction.HOLD, pnl=pnl, target=target, progress=progress,
        )

    # ── Caso 3: PnL >= ativação → trailing ativo
    peak = max(pnl, state.get("peak", 0.0))
    # trail_factor baseado no PICO (não no PnL atual) — ratchet só sobe.
    peak_progress = peak / target
    trail_factor = _compute_trail_factor(peak_progress, floor_pct)
    new_floor = round(peak * trail_factor, 2)
    # Floor só pode SUBIR (ratchet). Nunca desce mesmo se PnL cair.
    floor = max(new_floor, state.get("floor", 0.0))

    # Persiste state
    new_state = {
        "date": _today_str(),
        "activated": True,
        "activated_at": state.get("activated_at", datetime.now().isoformat()),
        "peak": peak,
        "floor": floor,
        "trail_factor": round(trail_factor, 4),
        "target": target,
        "last_pnl": pnl,
        "updated_at": datetime.now().isoformat(),
    }
    _atomic_write_state(new_state)

    # ── Caso 3a: PnL caiu abaixo do floor → BREACH
    if pnl < floor:
        return TrailingDecision(
            action=TrailingAction.BREACH,
            pnl=pnl, peak=peak, floor=floor,
            target=target, progress=progress,
            trail_factor=trail_factor,
        )

    # ── Caso 3b: Novo pico ou floor subiu → TIGHTEN
    old_floor = state.get("floor", 0.0)
    if floor > old_floor or peak > state.get("peak", 0.0):
        return TrailingDecision(
            action=TrailingAction.TIGHTEN,
            pnl=pnl, peak=peak, floor=floor,
            target=target, progress=progress,
            trail_factor=trail_factor,
        )

    # ── Caso 3c: Tudo igual → HOLD
    return TrailingDecision(
        action=TrailingAction.HOLD,
        pnl=pnl, peak=peak, floor=floor,
        target=target, progress=progress,
        trail_factor=trail_factor,
    )
