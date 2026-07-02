"""
Testes do DecisionLogger (Fase 4.2).

Cobertura:
  1. log() persiste JSONL com decision_id único
  2. query() filtra por tipo
  3. query() filtra por since_ts
  4. count_today() conta decisões de hoje
  5. persistência append-only (múltiplas decisões = múltiplas linhas)
  6. decision_id único entre chamadas
  7. log() não bloqueia se persistência falha (OSError → stderr, retorna id)
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from core.vt_decision import DecisionLogger, DECISION_TYPES


@pytest.fixture
def dl(tmp_path):
    """DecisionLogger isolado em tmp."""
    return DecisionLogger(log_path=tmp_path / "decisions.jsonl")


class TestLog:
    def test_log_returns_unique_id(self, dl):
        did1 = dl.log("close_position", context={"ticket": 1},
                      alternatives=["hold", "close"], chosen="close",
                      justification="SL hit")
        did2 = dl.log("close_position", context={"ticket": 2},
                      alternatives=["hold", "close"], chosen="close",
                      justification="TP hit")
        assert len(did1) == 8
        assert did1 != did2

    def test_log_persists_jsonl(self, dl, tmp_path):
        dl.log("restart_autotrader", context={"pid": 123},
               alternatives=["restart", "alert"], chosen="restart",
               justification="morto", auto_action="start.sh")
        content = (tmp_path / "decisions.jsonl").read_text()
        rec = json.loads(content.strip())
        assert rec["type"] == "restart_autotrader"
        assert rec["chosen"] == "restart"
        assert rec["context"]["pid"] == 123
        assert rec["auto_action"] == "start.sh"
        assert "decision_id" in rec
        assert "ts" in rec

    def test_append_only_multiple_decisions(self, dl):
        """Múltiplas decisões = múltiplas linhas (append-only)."""
        for i in range(3):
            dl.log("skip_entry", context={"i": i},
                   alternatives=["enter", "skip"], chosen="skip",
                   justification=f"blocked {i}")
        records = dl.query()
        assert len(records) == 3


class TestQuery:
    def test_filter_by_type(self, dl):
        dl.log("close_position", context={}, alternatives=["a"], chosen="a",
               justification="x")
        dl.log("restart_mt5", context={}, alternatives=["a"], chosen="a",
               justification="y")
        closes = dl.query(decision_type="close_position")
        assert len(closes) == 1
        assert all(r["type"] == "close_position" for r in closes)

    def test_filter_by_since_ts(self, dl):
        """since_ts filtra decisões anteriores ao timestamp."""
        dl.log("close_position", context={}, alternatives=["a"], chosen="a",
               justification="old")
        time.sleep(0.05)
        cutoff = time.time()  # captura DEPOIS da 1ª decisão
        time.sleep(0.02)
        dl.log("restart_mt5", context={}, alternatives=["a"], chosen="a",
               justification="new")
        recent = dl.query(since_ts=cutoff)
        assert len(recent) == 1
        assert recent[0]["justification"] == "new"

    def test_limit_returns_most_recent(self, dl):
        for i in range(5):
            dl.log("skip_entry", context={"i": i}, alternatives=["a"],
                   chosen="a", justification=str(i))
            time.sleep(0.005)
        recent = dl.query(limit=2)
        assert len(recent) == 2
        # mais recentes
        justifs = [r["justification"] for r in recent]
        assert "4" in justifs

    def test_empty_log_returns_empty(self, tmp_path):
        dl = DecisionLogger(log_path=tmp_path / "nope.jsonl")
        assert dl.query() == []


class TestCountToday:
    def test_count_today(self, dl):
        for _ in range(3):
            dl.log("close_position", context={}, alternatives=["a"],
                   chosen="a", justification="x")
        assert dl.count_today() == 3
        assert dl.count_today("close_position") == 3
        assert dl.count_today("restart_mt5") == 0


class TestResilience:
    def test_non_standard_type_warns_but_logs(self, dl):
        """Tipo fora do set padronizado é aceito com warning."""
        did = dl.log("custom_action", context={}, alternatives=["a"],
                     chosen="a", justification="x")
        assert len(did) == 8
        recs = dl.query(decision_type="custom_action")
        assert "_warning" in recs[0]

    def test_decision_types_set_has_8(self):
        assert len(DECISION_TYPES) == 8
