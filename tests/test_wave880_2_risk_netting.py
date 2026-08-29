"""Tests Wave 880.II (26/08/2026) — governador de risco por root, split
netting e kill-switch live.

Cobre os três mecanismos do incidente 26/08 (WDOU26: 4 contratos SELL
empilhados por M15/M30/H1 numa única posição netting, SL last-writer-wins,
-R$285 num stop, perda inteira numa linha e 3 trades reais a zero):

1. core/vt_risk_governor.py — orçamento de risco por símbolo-root
   (pior caso em aberto + nova entrada ≤ stop diário, hedge liberado,
   fail-open, tightest-SL-wins).
2. core/vt_netting.py — repartição exata do PnL do deal OUT entre as
   sub-entradas (soma das linhas == broker truth).
3. optimization/agi_v4/live_kill_switch.py — regras live_bleed/live_churn
   sobre a tabela trades (DB sintético em tmp_path) + quarentena.

Hermético: nenhum MT5, nenhum config/DB real.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import vt_netting  # noqa: E402
from core import vt_risk_governor as gov  # noqa: E402
from optimization.agi_v4 import live_kill_switch as lks  # noqa: E402


# ─────────────────────────────────────────────────────────────────────
# 1. Governador de risco por símbolo-root
# ─────────────────────────────────────────────────────────────────────

CONFIG_RISCO = {
    "max_daily_loss_by_symbol": {"WIN": -150, "WDO": -250, "WSP": -200,
                                 "BIT": -150},
    "contract_specs": {
        "WIN$": {"mult": 0.2}, "WDO$": {"mult": 10.0},
        "BIT$": {"mult": 0.01}, "WSP$": {"mult": 0.01},
    },
    "execution_guards": {"risk_buffer": 0.0},
}


def _pos(symbol, direction, vol, entry, sl, magic=555501):
    return {"symbol": symbol, "type": 0 if direction == "BUY" else 1,
            "volume": vol, "price_open": entry, "sl": sl, "magic": magic,
            "comment": "VibeTrading", "ticket": 1}


class TestRiskGovernor:
    def test_primeira_entrada_livre(self):
        r = gov.check_entry_risk_budget(
            "WDOU26", "SELL", sl_pts=5000, volume=1.0,
            config=CONFIG_RISCO, open_positions=[])
        assert r["ok"] is True

    def test_incidente_26_08_quarto_contrato_bloqueado(self):
        """3 contratos SELL em aberto (SL 5157.5, entradas ~5149-5152):
        pior caso já passa do orçamento → 4ª entrada SELL bloqueada."""
        aberto = [
            _pos("WDOU26", "SELL", 1.0, 5149.0, 5157.5),
            _pos("WDOU26", "SELL", 1.0, 5149.5, 5157.5),
            _pos("WDOU26", "SELL", 1.0, 5150.5, 5157.5),
        ]
        r = gov.check_entry_risk_budget(
            "WDOU26", "SELL", sl_pts=5000, volume=1.0,
            config=CONFIG_RISCO, open_positions=aberto)
        assert r["ok"] is False and r["reason"] == "RISK_BUDGET"

    def test_segundo_contrato_dentro_do_orcamento(self):
        """Com o risco somado ainda ≤ 250, a 2ª entrada passa (o incidente
        não era 2 contratos — era 4)."""
        aberto = [_pos("WDOU26", "SELL", 1.0, 5149.0, 5154.0)]
        r = gov.check_entry_risk_budget(
            "WDOU26", "SELL", sl_pts=5000, volume=1.0,
            config=CONFIG_RISCO, open_positions=aberto)
        assert r["ok"] is True
        # 2 × R$50 = R$100 ≤ 250
        assert r["open_risk"] == pytest.approx(50.0)
        assert r["new_risk"] == pytest.approx(50.0)

    def test_hedge_direcao_oposta_liberado(self):
        aberto = [_pos("WDOU26", "SELL", 2.0, 5149.0, 5157.5)]
        r = gov.check_entry_risk_budget(
            "WDOU26", "BUY", sl_pts=5000, volume=1.0,
            config=CONFIG_RISCO, open_positions=aberto)
        assert r["ok"] is True  # reduz exposição líquida sob netting

    def test_posicao_sem_sl_consome_orcamento_inteiro(self):
        aberto = [_pos("WDOU26", "SELL", 1.0, 5149.0, 0.0)]
        r = gov.check_entry_risk_budget(
            "WDOU26", "SELL", sl_pts=5000, volume=1.0,
            config=CONFIG_RISCO, open_positions=aberto)
        assert r["ok"] is False  # sem SL: conservador, bloqueia

    def test_posicao_de_outro_bot_nao_conta(self):
        outra = _pos("WDOU26", "SELL", 5.0, 5149.0, 5200.0, magic=999999)
        r = gov.check_entry_risk_budget(
            "WDOU26", "SELL", sl_pts=5000, volume=1.0,
            config=CONFIG_RISCO, open_positions=[outra])
        assert r["ok"] is True

    def test_root_sem_limite_fica_livre(self):
        r = gov.check_entry_risk_budget(
            "WSPU26", "SELL", sl_pts=100000, volume=1.0,
            config=CONFIG_RISCO, open_positions=[])
        assert r["ok"] is True  # WSP tem limite; sem limite → guard off
        cfg = {"max_daily_loss_by_symbol": {}}
        r2 = gov.check_entry_risk_budget(
            "WSPU26", "SELL", sl_pts=100000, volume=1.0,
            config=cfg, open_positions=[])
        assert r2["ok"] is True

    def test_env_desativa(self, monkeypatch):
        monkeypatch.setenv("VT_RISK_GOVERNOR", "0")
        aberto = [
            _pos("WDOU26", "SELL", 1.0, 5149.0, 5157.5),
            _pos("WDOU26", "SELL", 1.0, 5149.5, 5157.5),
            _pos("WDOU26", "SELL", 1.0, 5150.5, 5157.5),
        ]
        r = gov.check_entry_risk_budget(
            "WDOU26", "SELL", sl_pts=5000, volume=1.0,
            config=CONFIG_RISCO, open_positions=aberto)
        assert r["ok"] is True

    def test_fail_open_com_entrada_invalida(self):
        r = gov.check_entry_risk_budget(
            "WDOU26", "SELL", sl_pts=None, volume="x",
            config=None, open_positions=None)
        # Nunca explode e nunca bloqueia (aqui: sem config → guard off)
        assert r["ok"] is True and r["detail"]
        # Com config real e lixo de entrada, o fail-open é explícito
        r2 = gov.check_entry_risk_budget(
            "WDOU26", "SELL", sl_pts=None, volume="x",
            config=CONFIG_RISCO, open_positions=None)
        assert r2["ok"] is True and "fail-open" in r2["detail"]

    def test_tightest_sl_wins(self):
        # Incidente: SL 5154 → 5154.5 → 5162.5 (H1 LARGOU o stop)
        assert gov.should_restore_prev_sl("SELL", 5154.0, 5162.5) is True
        assert gov.should_restore_prev_sl("SELL", 5162.5, 5157.5) is False
        assert gov.should_restore_prev_sl("BUY", 5170.0, 5160.0) is True
        assert gov.should_restore_prev_sl("BUY", 5160.0, 5170.0) is False
        assert gov.should_restore_prev_sl("SELL", 0.0, 5162.5) is False


# ─────────────────────────────────────────────────────────────────────
# 2. Split netting (atribuição do PnL da posição consolidada)
# ─────────────────────────────────────────────────────────────────────

INCIDENTE_MEMBERS = [
    # (ticket, trade_log_id, entry, vol) — SELLs de 26/08 09:22–09:31
    {"trade_log_id": 101, "ticket": "2513025096", "direction": "SELL",
     "entry_price": 5149.0, "volume": 1.0, "multiplier": 10.0,
     "symbol": "WDOU26", "fees": 0.0},
    {"trade_log_id": 102, "ticket": "2513025098", "direction": "SELL",
     "entry_price": 5149.5, "volume": 1.0, "multiplier": 10.0,
     "symbol": "WDOU26", "fees": 0.0},
    {"trade_log_id": 103, "ticket": "2513025100", "direction": "SELL",
     "entry_price": 5150.5, "volume": 1.0, "multiplier": 10.0,
     "symbol": "WDOU26", "fees": 0.0},
    {"trade_log_id": 104, "ticket": "2513025102", "direction": "SELL",
     "entry_price": 5152.5, "volume": 1.0, "multiplier": 10.0,
     "symbol": "WDOU26", "fees": 0.0},
]
INCIDENTE_OUT = {"price": 5157.5, "profit": -285.0, "commission": 0.0,
                 "swap": 0.0, "fee": 0.0,
                 "position_ticket": "2513025096", "time": "2026-08-26 09:40:15"}


class TestNettingSplit:
    def test_soma_das_linhas_iguala_broker(self):
        updates = vt_netting.settle_netting_group(
            INCIDENTE_MEMBERS, INCIDENTE_OUT)
        assert len(updates) == 4
        total = sum(u["net_pnl"] for u in updates)
        assert total == pytest.approx(-285.0)  # broker truth preservado

    def test_pnl_por_entrada_exato(self):
        updates = {u["trade_log_id"]: u for u in
                   vt_netting.settle_netting_group(INCIDENTE_MEMBERS,
                                                   INCIDENTE_OUT)}
        # Cada linha: (5157.5 − entrada) × 1 × R$10 (SELL perde na alta)
        assert updates[102]["net_pnl"] == pytest.approx(-80.0)
        assert updates[103]["net_pnl"] == pytest.approx(-70.0)
        assert updates[104]["net_pnl"] == pytest.approx(-50.0)
        # Pai recebe o resíduo: −285 − (−80 −70 −50) = −85
        assert updates[101]["net_pnl"] == pytest.approx(-85.0)
        assert updates[101]["is_parent"] is True

    def test_buy_direcao_sinal_correto(self):
        members = [{"trade_log_id": 1, "ticket": "T1", "direction": "BUY",
                    "entry_price": 5149.0, "volume": 1.0,
                    "multiplier": 10.0, "symbol": "WDOU26", "fees": 0.0}]
        out = {"price": 5160.0, "profit": 110.0, "commission": 0.0,
               "swap": 0.0, "fee": 0.0, "position_ticket": "T1",
               "time": "x"}
        updates = vt_netting.settle_netting_group(members, out)
        assert updates[0]["net_pnl"] == pytest.approx(110.0)  # pai = residual

    def test_mult_fallback_pelo_symbol(self):
        members = [{"trade_log_id": 1, "ticket": "T1", "direction": "SELL",
                    "entry_price": 100.0, "volume": 1.0, "multiplier": 0,
                    "symbol": "WINZ26", "fees": 0.0}]
        out = {"price": 105.0, "profit": -1.0, "commission": 0.0,
               "swap": 0.0, "fee": 0.0, "position_ticket": "T1", "time": "x"}
        updates = vt_netting.settle_netting_group(members, out)
        # WIN mult 0.2: (105−100)×0.2 = 1.0 de gross; pai residual = −1.0
        assert updates[0]["net_pnl"] == pytest.approx(-1.0)

    def test_membro_sem_linha_vai_no_pai(self):
        members = INCIDENTE_MEMBERS[:1] + [
            {"trade_log_id": None, "ticket": "9999", "direction": "SELL",
             "entry_price": 5150.0, "volume": 1.0, "multiplier": 10.0,
             "symbol": "WDOU26", "fees": 0.0}]
        updates = vt_netting.settle_netting_group(members, INCIDENTE_OUT)
        assert len(updates) == 2
        # Órfão: (5157.5−5150)×10 = −75; pai: −285 − (−75) = −210
        pai = next(u for u in updates if u["is_parent"])
        orfao = next(u for u in updates if not u["is_parent"])
        assert orfao["net_pnl"] == pytest.approx(-75.0)
        assert pai["net_pnl"] == pytest.approx(-210.0)

    def test_erro_sem_preco_ou_membros(self):
        with pytest.raises(ValueError):
            vt_netting.settle_netting_group([], INCIDENTE_OUT)
        with pytest.raises(ValueError):
            vt_netting.settle_netting_group(INCIDENTE_MEMBERS,
                                            {"price": 0, "profit": 0})


# ─────────────────────────────────────────────────────────────────────
# 3. Kill-switch live (AGI) — regras + quarentena
# ─────────────────────────────────────────────────────────────────────

def _seed_db(tmp_path, rows):
    db = tmp_path / "trades.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE trades (
        id INTEGER PRIMARY KEY, symbol TEXT, timeframe TEXT, net_pnl REAL,
        entry_time TEXT, exit_time TEXT, exit_reason TEXT)""")
    agora = datetime.now()
    for i, (sym, tf, pnl) in enumerate(rows):
        ts = (agora - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO trades (symbol, timeframe, net_pnl, entry_time, "
            "exit_time, exit_reason) VALUES (?,?,?,?,?,?)",
            (sym, tf, pnl, ts, ts, "SL_SERVIDOR"))
    conn.commit()
    conn.close()
    return db


class TestLiveKillSwitch:
    CFG = {"strategy_by_tf": {"WDO_M15": "ADX_TREND", "BIT_M15": "DIVERGENCE_RSI",
                              "WIN_M15": "AGI4_WIN_121815"},
           "disabled_timeframes": ["WIN_M5"]}

    def test_bleed_e_churn_detectados(self, tmp_path):
        rows = (
            [("WDOU26", "M15", -25.0)] * 12            # -300 → live_bleed
            + [("BITQ26", "M15", -1.0)] * 30            # -30 → live_churn
            + [("WINZ26", "M15", 10.0)] * 12            # +120 → nada
        )
        db = _seed_db(tmp_path, rows)
        dec = {d["pair"]: d for d in lks.evaluate(self.CFG, db_path=db)}
        assert dec["WDO_M15"]["rule"] == "live_bleed"
        assert dec["WDO_M15"]["pnl"] == pytest.approx(-300.0)
        assert dec["BIT_M15"]["rule"] == "live_churn"
        assert "WIN_M15" not in dec

    def test_poucos_trades_nao_mata(self, tmp_path):
        rows = [("WDOU26", "M15", -285.0)] * 2  # incidente real: n=2 < 10
        db = _seed_db(tmp_path, rows)
        assert lks.evaluate(self.CFG, db_path=db) == []

    def test_par_ja_desativado_ignorado(self, tmp_path):
        rows = [("WDOU26", "M15", -25.0)] * 12
        db = _seed_db(tmp_path, rows)
        cfg = dict(self.CFG, disabled_timeframes=["WIN_M5", "WDO_M15"])
        assert lks.evaluate(cfg, db_path=db) == []

    def test_env_desliga(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VT_AGI_LIVE_KILL", "0")
        rows = [("WDOU26", "M15", -25.0)] * 12
        db = _seed_db(tmp_path, rows)
        assert lks.evaluate(self.CFG, db_path=db) == []

    def test_quarentena(self):
        agora = datetime.now()
        journal = [{"kind": "live_kill", "pair": "WDO_M15",
                    "ts": (agora - timedelta(days=3)).isoformat()}]
        ok, motivo = lks.is_quarantined("WDO_M15", journal, now=agora)
        assert ok is True and "quarentena" in motivo

        journal_velho = [{"kind": "live_kill", "pair": "WDO_M15",
                          "ts": (agora - timedelta(days=11)).isoformat()}]
        ok2, _ = lks.is_quarantined("WDO_M15", journal_velho, now=agora)
        assert ok2 is False

        ok3, _ = lks.is_quarantined("WIN_M15", journal, now=agora)
        assert ok3 is False  # outro par não é afetado
