"""
core/vt_decision.py
===================
Decision Logger (Fase 4.2) — audit trail de decisões autônomas.

Problema atual: o autotrader decide sozinho em vários lugares (restart, close,
pause, skip, kill_switch, reconcile), mas sem log estruturado — difícil auditar
"por que o bot decidiu X?".

Solução: cada decisão autônoma é registrada com:
  - decision_id (uuid8 único)
  - ts (timestamp)
  - type (categoria: restart_autotrader, close_position, pause_symbol, ...)
  - context (estado que motivou a decisão)
  - alternatives (opções consideradas)
  - chosen (a escolhida)
  - justification (por quê)
  - auto_action (o que foi feito de fato)

Persistência: /tmp/vt_decisions.jsonl (append-only JSON Lines — 1 linha por
decisão, fácil de grep/auditar). Stdlib only (json + uuid + time + pathlib).

Lei 1: sem dependências externas. NUNCA bloqueia o autotrader: se persistência
falha, só loga no stderr (decisão já foi tomada, log é pós-fato).

8 categorias pré-definidas (handoff):
  restart_autotrader, restart_mt5, close_position, pause_symbol, skip_entry,
  kill_switch_activated, reconcile_state, reconcile_db

Uso:
    from core.vt_decision import DecisionLogger
    dl = DecisionLogger()
    did = dl.log("restart_autotrader",
                 context={"old_pid": 654321, "killed_at": 1722547790},
                 alternatives=["restart", "alert", "abort"],
                 chosen="restart",
                 justification="autotrader morto há 30s",
                 auto_action="start_autotrader.sh executado, novo PID=789012")
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

DECISIONS_LOG = Path("/tmp/vt_decisions.jsonl")

# 8 categorias pré-definidas (handoff Fase 4.2)
DECISION_TYPES = frozenset({
    "restart_autotrader",
    "restart_mt5",
    "close_position",
    "pause_symbol",
    "skip_entry",
    "kill_switch_activated",
    "reconcile_state",
    "reconcile_db",
})


class DecisionLogger:
    """Append-only JSON Lines decision log. Thread-safe via append mode."""

    def __init__(self, log_path: Path = DECISIONS_LOG):
        self.path = Path(log_path)

    def log(self, decision_type: str, context: Dict[str, Any],
            alternatives: List[str], chosen: str, justification: str,
            auto_action: Optional[str] = None) -> str:
        """Registra uma decisão autônoma. Retorna decision_id (uuid8).

        Args:
            decision_type: categoria (ver DECISION_TYPES). Strings fora do set
                são aceitas (com warning) p/ flexibilidade, mas recomenda-se usar
                as categorias padronizadas para query consistente.
            context: estado que motivou a decisão (dict serializável).
            alternatives: opções consideradas (lista).
            chosen: a opção escolhida (deve estar em alternatives idealmente).
            justification: string explicando o porquê.
            auto_action: o que foi feito de fato (None se só decisão, sem ação).

        Returns:
            decision_id (8 chars hex). Vazio "" se persistência falhou (mas a
            decisão NÃO é desfeita — log é pós-fato, nunca bloqueia).
        """
        decision_id = uuid.uuid4().hex[:8]
        record = {
            "decision_id": decision_id,
            "ts": time.time(),
            "type": decision_type,
            "context": context or {},
            "alternatives": alternatives or [],
            "chosen": chosen,
            "justification": justification or "",
            "auto_action": auto_action,
        }
        # Aviso se tipo não-padronizado (não bloqueia)
        if decision_type not in DECISION_TYPES:
            record["_warning"] = f"tipo '{decision_type}' fora do set padronizado"
        try:
            # append-only: 'a' mode, 1 linha JSON por decisão
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError as e:
            # Log é pós-fato: decisão já foi tomada. Só stderr, não bloqueia.
            print(f"[VT-DECISION] WARN: falha ao persistir decisão {decision_id}: "
                  f"{e}", file=sys.stderr)
        return decision_id

    def query(self, decision_type: Optional[str] = None,
              since_ts: Optional[float] = None,
              limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Consulta decisões (para auditoria). Lê o JSONL.

        Args:
            decision_type: filtra por tipo (None = todos).
            since_ts: só decisões após este timestamp (None = todas).
            limit: máx de resultados (mais recentes primeiro).

        Returns:
            Lista de records (dicts), ordenados por ts (crescente por padrão;
            se limit, os `limit` mais recentes).
        """
        if not self.path.exists():
            return []
        results = []
        try:
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # linha corrompida — pula
                    if decision_type and rec.get("type") != decision_type:
                        continue
                    if since_ts and rec.get("ts", 0) < since_ts:
                        continue
                    results.append(rec)
        except OSError:
            return []
        if limit and len(results) > limit:
            # mais recentes primeiro quando há limit
            results = results[-limit:]
        return results

    def count_today(self, decision_type: Optional[str] = None) -> int:
        """Conta decisões de hoje (para dashboard)."""
        import datetime
        today_start = datetime.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        return len(self.query(decision_type=decision_type,
                              since_ts=today_start))


# Instância global (lazy)
_global_dl: Optional[DecisionLogger] = None


def get_decision_logger() -> DecisionLogger:
    global _global_dl
    if _global_dl is None:
        _global_dl = DecisionLogger()
    return _global_dl


if __name__ == "__main__":  # pragma: no cover
    dl = DecisionLogger()
    did = dl.log("restart_autotrader",
                 context={"old_pid": 654321},
                 alternatives=["restart", "alert"],
                 chosen="restart",
                 justification="morto há 30s",
                 auto_action="start_autotrader.sh")
    print(f"logged: {did}")
    print(f"today: {dl.count_today()}")
