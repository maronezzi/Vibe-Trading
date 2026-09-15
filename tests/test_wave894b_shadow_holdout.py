#!/usr/bin/env python3
"""Tests Wave 894B (15/09) — shadow gate e holdout gate do stage5.

shadow_gate: promoção shadow-first (sombra negativa rejeita; sombra positiva
passa) + blindagem de incumbência live (vencedor comprovado não sai por
candidato só-sim — o caso HTF_BIAS→AGI4_WIN de 11/08).

holdout_gate: revalidação out-of-sample (PnL<0 nos últimos N pregões rejeita)
+ fração de overlap netting (entrada dentro de trade oposto de outro TF do
mesmo root na sombra).

Hermético: DBs sintéticos em tmp_path, envs via monkeypatch (o conftest
desliga os gates por default — esta suíte os reativa por teste).
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from optimization.agi_v4 import holdout_gate as hg  # noqa: E402
from optimization.agi_v4 import shadow_gate as sg  # noqa: E402

AGORA = datetime.now()
ONTEM = (AGORA - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")


def _seed_db(tmp_path):
    """DB sintético com trades + forward_sim_trades (espelha schemas reais).

    Nome distinto do vt_trades.db do conftest (fixture autouse cria tabela
    `trades` mínima no mesmo tmp_path — schema sem `strategy`/sombra)."""
    db = tmp_path / "gate_trades.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE trades (
        id INTEGER PRIMARY KEY, symbol TEXT, timeframe TEXT, strategy TEXT,
        net_pnl REAL, entry_time TEXT, exit_time TEXT, exit_reason TEXT)""")
    conn.execute("""CREATE TABLE forward_sim_trades (
        id INTEGER PRIMARY KEY, symbol TEXT, timeframe TEXT, strategy TEXT,
        direction TEXT, net_pnl_brl REAL, entry_time TEXT, exit_time TEXT)""")
    conn.commit()
    conn.close()
    return db


def _add_live(conn_db, sym, tf, strat, pnl, n=1):
    conn = sqlite3.connect(str(conn_db))
    for _ in range(n):
        conn.execute(
            "INSERT INTO trades (symbol, timeframe, strategy, net_pnl, "
            "entry_time, exit_time, exit_reason) VALUES (?,?,?,?,?,?,?)",
            (sym, tf, strat, pnl, ONTEM, ONTEM, "SL_SERVIDOR"))
    conn.commit()
    conn.close()


def _add_shadow(conn_db, sym, tf, strat, direction, pnl, n=1,
                entry=None, exit_=None):
    conn = sqlite3.connect(str(conn_db))
    for _ in range(n):
        e = entry or ONTEM
        x = exit_ or ONTEM
        conn.execute(
            "INSERT INTO forward_sim_trades (symbol, timeframe, strategy, "
            "direction, net_pnl_brl, entry_time, exit_time) "
            "VALUES (?,?,?,?,?,?,?)",
            (sym, tf, strat, direction, pnl, e, x))
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────────
# 1. shadow_gate
# ─────────────────────────────────────────────────────────────────────

CFG_SWAP = {"strategy_by_tf": {"WIN_M15": "AGI4_BIT_121102",
                               "WDO_H1": "TRIPLE_EMA"}}


class TestShadowGate:
    @pytest.fixture(autouse=True)
    def _gate_on(self, monkeypatch):
        monkeypatch.setenv("VT_AGI_SHADOW_GATE", "1")
        monkeypatch.delenv("VT_AGI_SHADOW_GATE_DAYS", raising=False)
        monkeypatch.delenv("VT_AGI_SHADOW_GATE_MIN_TRADES", raising=False)
        monkeypatch.delenv("VT_AGI_SHADOW_GATE_MIN_PF", raising=False)
        monkeypatch.delenv("VT_AGI_LIVE_PROTECT_MIN_TRADES", raising=False)
        monkeypatch.delenv("VT_AGI_LIVE_PROTECT_DAYS", raising=False)

    def test_sombra_negativa_rejeita(self, tmp_path):
        # DIVERGENCE_RSI/BIT_M15 na vida real: sombra −R$5.739/80t
        db = _seed_db(tmp_path)
        _add_shadow(db, "BITU26", "M15", "DIVERGENCE_RSI", "SELL", -72.0, n=25)
        ok, gate, _ = sg.gate_strategy_swap(
            {"strategy_by_tf": {"BIT_M15": "AGI4_BIT_201305"}}, db,
            "BIT_M15", "DIVERGENCE_RSI")
        assert ok is False and gate == "shadow_negative"

    def test_sombra_positiva_passa(self, tmp_path):
        db = _seed_db(tmp_path)
        # PF alto: 15 vitórias +6, 10 perdas −2 → +70, PF 4.5
        _add_shadow(db, "WDOV26", "M5", "EMA_SLOPE_MOMENTUM", "BUY", 6.0, n=15)
        _add_shadow(db, "WDOV26", "M5", "EMA_SLOPE_MOMENTUM", "SELL", -2.0, n=10)
        ok, _, why = sg.gate_strategy_swap(
            {"strategy_by_tf": {"WDO_M5": "AGI4_BIT_121102"}}, db,
            "WDO_M5", "EMA_SLOPE_MOMENTUM")
        assert ok is True and "sombra positiva" in why

    def test_incumbente_protegido_bloqueia(self, tmp_path):
        # O caso HTF_BIAS→AGI4_WIN (11/08): incumbente +R$665/83t live,
        # candidato sem sombra — não se tira vencedor por candidato só-sim
        db = _seed_db(tmp_path)
        _add_live(db, "WINZ26", "M15", "HTF_BIAS_LTF_ENTRY", 55.0, n=12)
        cfg = {"strategy_by_tf": {"WIN_M15": "HTF_BIAS_LTF_ENTRY"}}
        ok, gate, why = sg.gate_strategy_swap(cfg, db,
                                              "WIN_M15", "AGI4_WIN_121815")
        assert ok is False and gate == "incumbent_protected"
        assert "HTF_BIAS_LTF_ENTRY" in why

    def test_incumbente_perdendo_nao_e_protegido(self, tmp_path):
        # Incumbente sangrando → sem blindagem; candidato sem contradição passa
        db = _seed_db(tmp_path)
        _add_live(db, "WINZ26", "M15", "AGI4_BIT_121102", -30.0, n=12)
        ok, gate, _ = sg.gate_strategy_swap(CFG_SWAP, db,
                                            "WIN_M15", "AGI4_WIN_121815")
        assert ok is True and gate == ""

    def test_params_only_passa_direto(self, tmp_path):
        db = _seed_db(tmp_path)
        ok, _, why = sg.gate_strategy_swap(CFG_SWAP, db,
                                           "WIN_M15", "AGI4_BIT_121102")
        assert ok is True and "params-only" in why

    def test_env_desliga(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VT_AGI_SHADOW_GATE", "0")
        db = _seed_db(tmp_path)
        _add_shadow(db, "BITU26", "M15", "DIVERGENCE_RSI", "SELL", -72.0, n=25)
        ok, _, _ = sg.gate_strategy_swap(
            {"strategy_by_tf": {"BIT_M15": "X"}}, db,
            "BIT_M15", "DIVERGENCE_RSI")
        assert ok is True

    def test_sombra_curta_nao_rejeita(self, tmp_path):
        # n=7 < 20 (mín): evidência insuficiente não mata (fall-open parcial)
        db = _seed_db(tmp_path)
        _add_shadow(db, "BITU26", "M15", "NOVA_X", "SELL", -10.0, n=7)
        ok, gate, _ = sg.gate_strategy_swap(
            {"strategy_by_tf": {"BIT_M15": "AGI4_BIT_201305"}}, db,
            "BIT_M15", "NOVA_X")
        assert ok is True and gate == ""

    def test_sem_db_segue_soberania(self, tmp_path):
        ok, gate, _ = sg.gate_strategy_swap(
            CFG_SWAP, tmp_path / "nada.db", "WIN_M15", "AGI4_WIN_121815")
        assert ok is True and gate == ""


# ─────────────────────────────────────────────────────────────────────
# 2. holdout_gate — decisão pura
# ─────────────────────────────────────────────────────────────────────

class TestHoldoutDecisao:
    @pytest.fixture(autouse=True)
    def _defaults(self, monkeypatch):
        monkeypatch.setenv("VT_AGI_HOLDOUT_DAYS", "10")
        monkeypatch.delenv("VT_AGI_NETTING_OVERLAP_MAX", raising=False)

    def test_holdout_negativo_rejeita(self):
        ok, why = hg.evaluate_holdout_decision(-50.0, 10, 0.0)
        assert ok is False and "NEGATIVO" in why

    def test_overlap_excessivo_rejeita(self):
        ok, why = hg.evaluate_holdout_decision(50.0, 10, 0.5)
        assert ok is False and "conflitam" in why

    def test_aprovado(self):
        ok, why = hg.evaluate_holdout_decision(50.0, 10, 0.1)
        assert ok is True and why == ""

    def test_sem_trades_neutro(self):
        ok, why = hg.evaluate_holdout_decision(0.0, 0, 0.0)
        assert ok is True and "neutro" in why

    def test_desligado_por_env(self, monkeypatch):
        monkeypatch.setenv("VT_AGI_HOLDOUT_DAYS", "0")
        ok, _ = hg.evaluate_holdout_decision(-50.0, 10, 0.9)
        assert ok is True


# ─────────────────────────────────────────────────────────────────────
# 3. holdout_gate — overlap netting + wrapper validate
# ─────────────────────────────────────────────────────────────────────

class TestHoldoutOverlap:
    @pytest.fixture(autouse=True)
    def _defaults(self, monkeypatch):
        monkeypatch.setenv("VT_AGI_HOLDOUT_DAYS", "10")

    def test_entrada_dentro_de_trade_oposto_conflita(self, tmp_path):
        db = _seed_db(tmp_path)
        e = AGORA - timedelta(hours=2)
        x = AGORA - timedelta(hours=1)
        _add_shadow(db, "WSPU26", "M15", "AGI4_WSP_134734", "SELL", -5.0,
                    entry=e.strftime("%Y-%m-%d %H:%M:%S"),
                    exit_=x.strftime("%Y-%m-%d %H:%M:%S"))
        cands = [{"entry_dt": AGORA - timedelta(minutes=90),
                  "direction": "BUY"},   # dentro do intervalo SELL → conflito
                 {"entry_dt": AGORA - timedelta(minutes=10),
                  "direction": "BUY"}]   # depois do intervalo → livre
        frac, n = hg.netting_overlap_fraction(db, "WSP", "M5", cands)
        assert n == 2 and frac == pytest.approx(0.5)

    def test_mesmo_tf_nao_conflita(self, tmp_path):
        # Sombra do PRÓPRIO TF não conta (o gate vive da briga ENTRE TFs)
        db = _seed_db(tmp_path)
        e = AGORA - timedelta(hours=2)
        x = AGORA - timedelta(hours=1)
        _add_shadow(db, "WSPU26", "M5", "AGI4_BIT_171647", "SELL", -5.0,
                    entry=e.strftime("%Y-%m-%d %H:%M:%S"),
                    exit_=x.strftime("%Y-%m-%d %H:%M:%S"))
        cands = [{"entry_dt": AGORA - timedelta(minutes=90), "direction": "BUY"}]
        frac, n = hg.netting_overlap_fraction(db, "WSP", "M5", cands)
        assert frac == 0.0 and n == 1

    def test_validate_rejeita_holdout_negativo(self, tmp_path, monkeypatch):
        import optimization.agi_v4.backtest_evaluator as be
        monkeypatch.setattr(
            be, "evaluate_holdout",
            lambda *a, **k: {"total_pnl": -100.0, "n_trades": 8,
                             "trades": [], "error": ""})
        ok, why = hg.validate({}, "WIN_M15", "CAND_X", {}, db_path=tmp_path / "n.db")
        assert ok is False and "NEGATIVO" in why

    def test_validate_fail_open_em_erro(self, tmp_path, monkeypatch):
        import optimization.agi_v4.backtest_evaluator as be
        def _boom(*a, **k):
            raise RuntimeError("mt5 fora")
        monkeypatch.setattr(be, "evaluate_holdout", _boom)
        ok, why = hg.validate({}, "WIN_M15", "CAND_X", {},
                              db_path=tmp_path / "n.db")
        assert ok is True and "fail-open" in why

    def test_validate_desligado_por_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VT_AGI_HOLDOUT_DAYS", "0")
        ok, _ = hg.validate({}, "WIN_M15", "CAND_X", {}, db_path=None)
        assert ok is True
