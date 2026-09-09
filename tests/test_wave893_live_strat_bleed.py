"""Tests Wave 893 (08/09) — exceção de realidade live no better_baseline.

Caso que motivou: AGI4_WIN_121815 live -R$440/29t em WIN_M15 (30d) com sim
positiva; o gate better_baseline_exists segurava qualquer troca "porque a
sim do incumbente é melhor", e as vitórias do HTF_BIAS no mesmo par
escondiam o kill-switch por PAR (granularidade errada — bleed é por
ESTRATÉGIA). O _incumbent_live_bleeding detecta o bleed e libera a troca
(gates de WF/churn/rolagem seguem valendo).

Hermético: DB sqlite sintético em tmp_path, sem MT5/config real.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from optimization.agi_v4 import stage5_apply  # noqa: E402


def _make_db(tmp_path: Path) -> Path:
    # conftest.py já cria tmp_path/vt_trades.db com o schema real
    db = tmp_path / "vt_trades.db"
    con = sqlite3.connect(str(db))
    _cols = ("symbol, direction, volume, timeframe, strategy, entry_time, "
             "entry_price, exit_time, exit_reason, net_pnl")

    def _ins(sym, tf, strat, day, pnl, reason="SL_SERVIDOR"):
        con.execute(
            f"INSERT INTO trades ({_cols}) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sym, "SELL", 1.0, tf, strat, f"{day} 10:00:00", 100.0,
             f"{day} 11:00:00", reason, pnl))

    # Incumbente sangrando: 12 trades, -R$300 no mês
    for i in range(12):
        _ins("WINZ26", "M15", "AGI4_WIN_121815", f"2026-08-{20+i%10:02d}", -25.0)
    # Concorrente saudável no MESMO par (não deve afetar a checagem)
    _ins("WINZ26", "M15", "HTF_BIAS_LTF_ENTRY", "2026-08-26", 346.6, "TRAIL")
    con.commit()
    con.close()
    return db


class TestIncumbentLiveBleeding:
    def test_incumbente_sangrando_detectado(self, tmp_path, monkeypatch):
        db = _make_db(tmp_path)
        monkeypatch.setattr(
            "optimization.agi_v4.stage1_collect._resolve_db_path",
            lambda cfg: str(db))
        cfg = {"strategy_by_tf": {"WIN_M15": "AGI4_WIN_121815"}}
        bleed, why = stage5_apply._incumbent_live_bleeding("WIN_M15", cfg)
        assert bleed is True
        assert "AGI4_WIN_121815" in why and "-300" in why

    def test_par_sem_incumbente_nao_bleeda(self, tmp_path, monkeypatch):
        db = _make_db(tmp_path)
        monkeypatch.setattr(
            "optimization.agi_v4.stage1_collect._resolve_db_path",
            lambda cfg: str(db))
        cfg = {"strategy_by_tf": {}}
        bleed, _ = stage5_apply._incumbent_live_bleeding("WIN_M15", cfg)
        assert bleed is False

    def test_incumbente_saudavel_nao_bleeda(self, tmp_path, monkeypatch):
        db = _make_db(tmp_path)
        monkeypatch.setattr(
            "optimization.agi_v4.stage1_collect._resolve_db_path",
            lambda cfg: str(db))
        cfg = {"strategy_by_tf": {"WIN_M15": "HTF_BIAS_LTF_ENTRY"}}
        bleed, _ = stage5_apply._incumbent_live_bleeding("WIN_M15", cfg)
        assert bleed is False

    def test_db_ausente_fail_safe(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "optimization.agi_v4.stage1_collect._resolve_db_path",
            lambda cfg: str(tmp_path / "inexistente.db"))
        cfg = {"strategy_by_tf": {"WIN_M15": "AGI4_WIN_121815"}}
        bleed, _ = stage5_apply._incumbent_live_bleeding("WIN_M15", cfg)
        assert bleed is False
