"""
core/vt_order_tracker.py
========================
Order Tracker (Fase 3.1 — Lei 4): rastreamento ininterrupto de cada ordem aberta.

Para cada ordem enviada ao MT5, mantemos um registro autoritativo no lado Python:
  - ticket (MT5 broker — fonte de verdade)
  - symbol, direction, volume
  - entry_price, sl_pts, tp_pts
  - entry_time, entry_reason, strategy
  - status: 'open' | 'closed' | 'orphan' | 'ghost'
  - last_heartbeat (atualizado a cada tick)

Garantia (Lei 4): se MT5 fecha uma posição (server-side SL/TP, manual, emergency),
o tracker detecta via reconcile() e marca como closed/ghost ANTES do próximo tick,
evitando double-send de ordens.

Diferente do state.json (projection-only, Fase 3 do refactor), o tracker é
PERSISTIDO em /tmp/vt_order_tracker.json com atomic write — sobrevive a restarts
e dá audit trail. Mas a verdade final continua sendo o MT5: reconcile() usa
core.vt_truth.get_open_positions() para saber o que está realmente aberto.

Integração:
  - vt_autotrader._execute_entry() → tracker.register_order() após MT5 confirmar
  - vt_autotrader.reconcile_positions_with_mt5() → tracker.reconcile() e age
    conforme o report (orphans para ingest, ghosts para cleanup)

 Lei 2: nunca desabilita símbolo/TF. Lei 3: toda ordem registrada tem sl_pts>0.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("vt_order_tracker")
if not log.handlers:
    logging.basicConfig(level=logging.INFO,
                        format="[%(asctime)s] [TRACKER] %(message)s",
                        datefmt="%H:%M:%S")

# Persistência (atomic write via tmp + rename)
TRACKER_PATH = Path("/tmp/vt_order_tracker.json")
_HEARTBEAT_STALE_SEC = 300  # 5min sem update = stale (info, não fatal)


# ── Dataclasses ─────────────────────────────────────────────────────────────
@dataclass
class OrderRecord:
    """Registro de uma ordem rastreada."""
    ticket: int
    symbol: str
    direction: str            # 'BUY' | 'SELL'
    volume: float
    entry_price: float
    sl_pts: int
    tp_pts: Optional[int] = None
    entry_time: float = field(default_factory=time.time)
    entry_reason: str = ""
    strategy: str = ""
    status: str = "open"      # open | closed | orphan | ghost
    last_heartbeat: float = field(default_factory=time.time)
    state_rebuild_count: int = 0
    close_time: Optional[float] = None
    close_price: Optional[float] = None
    close_reason: Optional[str] = None


@dataclass
class ReconcileReport:
    """Resultado de uma reconciliação tracker vs MT5."""
    confirmed: List[int] = field(default_factory=list)      # abertos nos 2 lados
    orphans: List[int] = field(default_factory=list)        # no MT5, não no tracker
    ghosts: List[int] = field(default_factory=list)         # no tracker, não no MT5
    closed_by_broker: List[int] = field(default_factory=list)  # tracker marcou closed

    @property
    def has_drift(self) -> bool:
        return bool(self.orphans or self.ghosts)

    def to_dict(self) -> dict:
        return {
            "confirmed": self.confirmed,
            "orphans": self.orphans,
            "ghosts": self.ghosts,
            "closed_by_broker": self.closed_by_broker,
            "has_drift": self.has_drift,
        }


# ── Atomic write helper (espelha vt_config_loader._atomic_write) ────────────
def _atomic_write_json(path: Path, data: dict) -> None:
    """Escreve JSON atomicamente (tmp + rename). Não corrompe se crash mid-write."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(str(tmp), str(path))


# ── OrderTracker ────────────────────────────────────────────────────────────
class OrderTracker:
    """Rastreia ordens abertas. Persiste em /tmp/vt_order_tracker.json.

    Lei 3: register_order recusa sl_pts <= 0 (SL obrigatório).
    Lei 4: reconcile usa MT5 como verdade; ghosts viram closed.
    """

    def __init__(self, path: Path = TRACKER_PATH,
                 truth_layer=None, autoload: bool = True):
        self.path = Path(path)
        # truth_layer = módulo core.vt_truth (injetável p/ testes)
        self.truth = truth_layer
        self._active: Dict[int, OrderRecord] = {}
        if autoload:
            self.load()

    # ── Persistência ──
    def load(self) -> None:
        """Carrega tracker do disco (se existir). Nunca levanta."""
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for ticket_str, rec in data.items():
                try:
                    ticket = int(ticket_str)
                    self._active[ticket] = OrderRecord(**rec)
                except (ValueError, TypeError) as e:
                    log.warning("load: registro inválido ticket=%s: %s",
                                ticket_str, e)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("load: falha ao ler %s: %s (começando vazio)",
                        self.path, e)
            self._active = {}

    def save(self) -> None:
        """Persiste tracker atomicamente. Só salva ordens não-closed."""
        data = {
            str(t): asdict(r) for t, r in self._active.items()
            if r.status != "closed"
        }
        try:
            _atomic_write_json(self.path, data)
        except OSError as e:  # pragma: no cover
            log.error("save: falha ao persistir %s: %s", self.path, e)

    # ── API pública ──
    def register_order(self, ticket: int, symbol: str, direction: str,
                       volume: float, entry_price: float, sl_pts: int,
                       tp_pts: Optional[int] = None, reason: str = "",
                       strategy: str = "") -> bool:
        """Registra nova ordem aberta. Retorna False se sl inválido (Lei 3)
        ou ticket inválido (Lei 4).

        Lei 3: sl_pts deve ser > 0.
        Lei 4: ticket deve ser > 0 (MT5 confirmou).
        """
        if not ticket or ticket <= 0:
            log.warning("register_order: ticket inválido %s (Lei 4). Recusado.",
                        ticket)
            return False
        if sl_pts is None or sl_pts <= 0:
            log.warning("register_order: %s ticket=%s sl_pts=%s inválido (Lei 3). "
                        "Recusado.", symbol, ticket, sl_pts)
            return False
        rec = OrderRecord(
            ticket=int(ticket), symbol=symbol,
            direction=direction.upper(), volume=float(volume),
            entry_price=float(entry_price), sl_pts=int(sl_pts),
            tp_pts=tp_pts, entry_reason=reason, strategy=strategy,
            status="open",
        )
        self._active[int(ticket)] = rec
        self.save()
        log.info("register: %s %s ticket=%d vol=%s sl=%d (%s)",
                 symbol, direction, ticket, volume, sl_pts,
                 reason or strategy or "-")
        return True

    def update_heartbeat(self, ticket: int) -> None:
        """Atualiza timestamp do último heartbeat de uma ordem."""
        rec = self._active.get(int(ticket))
        if rec is not None and rec.status == "open":
            rec.last_heartbeat = time.time()

    def mark_closed(self, ticket: int, close_price: float = 0.0,
                    close_reason: str = "") -> bool:
        """Marca ordem como fechada (broker closed, emergency, manual)."""
        rec = self._active.get(int(ticket))
        if rec is None:
            return False
        rec.status = "closed"
        rec.close_time = time.time()
        rec.close_price = close_price
        rec.close_reason = close_reason
        self.save()
        log.info("closed: ticket=%d %s reason=%s", ticket, rec.symbol,
                 close_reason or "-")
        return True

    def get_active_orders(self, symbol: Optional[str] = None) -> List[OrderRecord]:
        """Retorna ordens ativas (status='open'), filtrado por symbol opcional."""
        orders = [r for r in self._active.values() if r.status == "open"]
        if symbol:
            orders = [r for r in orders if symbol.upper() in r.symbol.upper()]
        return orders

    def check_orphans(self) -> List[int]:
        """Tickets no tracker mas NÃO no MT5 (server-side close)."""
        mt5_tickets = self._mt5_open_tickets()
        return [t for t, r in self._active.items()
                if r.status == "open" and t not in mt5_tickets]

    def check_ghosts(self) -> List[int]:
        """Alias semântico para check_orphans (posições que 'somem')."""
        return self.check_orphans()

    def reconcile(self) -> ReconcileReport:
        """Reconcilia tracker vs MT5. Retorna report com ações sugeridas.

        Usa core.vt_truth.get_open_positions() como verdade (não DB).
        Marca ghosts como 'closed' (foram fechadas server-side).
        NÃO ingere orphans diretamente — o caller (autotrader) decide.
        """
        report = ReconcileReport()
        try:
            mt5_positions = self._get_mt5_positions()
        except Exception as e:
            log.warning("reconcile: MT5 indisponível (%s) — pulando", e)
            return report
        mt5_tickets = {p.get("ticket") if isinstance(p, dict) else p.ticket
                       for p in mt5_positions}
        tracker_open = {t for t, r in self._active.items()
                        if r.status == "open"}

        report.confirmed = sorted(tracker_open & mt5_tickets)
        report.orphans = sorted(mt5_tickets - tracker_open)
        report.ghosts = sorted(tracker_open - mt5_tickets)

        # Marca ghosts como closed (broker fechou server-side)
        for ticket in report.ghosts:
            rec = self._active.get(ticket)
            if rec:
                self.mark_closed(ticket, close_reason="ghost_reconcile")

        if report.has_drift:
            log.info("reconcile: %d confirmed, %d orphans, %d ghosts",
                     len(report.confirmed), len(report.orphans),
                     len(report.ghosts))
        self.save()
        return report

    # ── Helpers internos (MT5 truth) ──
    def _get_mt5_positions(self) -> list:
        """Lista posições abertas via truth layer. Injeta p/ testes."""
        if self.truth is not None:
            return self.truth.get_open_positions()
        # import tardio p/ evitar ciclo em produção
        try:
            from core import vt_truth
            self.truth = vt_truth
            return vt_truth.get_open_positions()
        except Exception as e:
            log.warning("MT5 indisponível: %s", e)
            return []

    def _mt5_open_tickets(self) -> set:
        return {p.get("ticket") if isinstance(p, dict) else p.ticket
                for p in self._get_mt5_positions()}


def _self_test() -> None:  # pragma: no cover
    """Smoke test manual (não via pytest)."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        t = OrderTracker(path=Path(d) / "tracker.json")
        ok = t.register_order(12345, "WINQ26", "BUY", 1.0, 175000.0, 200,
                              reason="test")
        print(f"register: {ok}")
        print(f"active: {[r.ticket for r in t.get_active_orders()]}")
        rep = t.reconcile()
        print(f"reconcile: {rep.to_dict()}")


if __name__ == "__main__":  # pragma: no cover
    _self_test()
