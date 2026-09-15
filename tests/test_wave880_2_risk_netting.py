"""Tests Wave 880.II (26/08/2026) — governador de risco por root, split
netting e kill-switch live.

Cobre os três mecanismos do incidente 26/08 (WDOU26: 4 contratos SELL
empilhados por M15/M30/H1 numa única posição netting, SL last-writer-wins,
-R$285 num stop, perda inteira numa linha e 3 trades reais a zero):

1. core/vt_risk_governor.py — orçamento de risco por símbolo-root
   (pior caso em aberto + nova entrada ≤ stop diário, coerência direcional
   sob netting [Wave 894], fail-open, tightest-SL-wins).
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


class TestGovernadorRealizado:
    """Wave 893 (08/09): perda REALIZADA do root consome orçamento.

    Incidente 04/09 (WIN): -R$90 realizado, nova entrada pior caso -R$120,
    stop diário -150 — governador via "0 aberto + 120 ≤ 150" e liberava;
    o dia fechou -210 (40% além do stop)."""

    def test_incidente_04_09_perda_realizada_bloqueia(self):
        # WIN: SL 600pts × mult 0.2 × 1 lote = R$120 de pior caso.
        # Sem realizado: 120 ≤ 150 → ok. Com -90 realizado: 120 > 60 → bloqueia.
        r_sem = gov.check_entry_risk_budget(
            "WINZ26", "SELL", 600, 1.0, CONFIG_RISCO, [])
        assert r_sem["ok"] is True
        r_com = gov.check_entry_risk_budget(
            "WINZ26", "SELL", 600, 1.0, CONFIG_RISCO, [], realized_pnl=-90.0)
        assert r_com["ok"] is False
        assert r_com["reason"] == "RISK_BUDGET"

    def test_lucro_realizado_nao_expande_orcamento(self):
        # +R$200 realizado NÃO libera risco além do stop diário.
        r = gov.check_entry_risk_budget(
            "WINZ26", "SELL", 1200, 1.0, CONFIG_RISCO, [], realized_pnl=200.0)
        # 1200pts × 0.2 = R$240 > 150 mesmo com lucro — bloqueia
        assert r["ok"] is False

    def test_realizado_zero_mantem_comportamento_antigo(self):
        # default/kwarg 0 = comportamento pré-Wave 893 (retrocompatível)
        r = gov.check_entry_risk_budget(
            "WINZ26", "SELL", 600, 1.0, CONFIG_RISCO, [], realized_pnl=0.0)
        assert r["ok"] is True


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

    def test_hedge_direcao_oposta_bloqueado_w894(self):
        """Wave 894 (15/09): entrada contra a exposição líquida do root é
        BLOQUEADA — sob netting não é hedge, é churn (set/2026: ~-R$357 em
        entradas opostas simultâneas, exposição zero, custos pagos)."""
        aberto = [_pos("WDOU26", "SELL", 2.0, 5149.0, 5157.5)]
        r = gov.check_entry_risk_budget(
            "WDOU26", "BUY", sl_pts=5000, volume=1.0,
            config=CONFIG_RISCO, open_positions=aberto)
        assert r["ok"] is False
        assert r["reason"] == "NETTING_COHERENCE"

    def test_coerencia_opt_out_libera_hedge(self):
        # netting_coherence_enabled=false volta ao comportamento pré-Wave 894
        cfg = dict(CONFIG_RISCO, execution_guards={
            "risk_buffer": 0.0, "netting_coherence_enabled": False})
        aberto = [_pos("WDOU26", "SELL", 2.0, 5149.0, 5157.5)]
        r = gov.check_entry_risk_budget(
            "WDOU26", "BUY", sl_pts=5000, volume=1.0,
            config=cfg, open_positions=aberto)
        assert r["ok"] is True and "liberada" in r["detail"]

    def test_coerencia_sem_posicao_aberta_na_bloqueia(self):
        # Sem sub-entradas do bot no root → guard de coerência não se aplica
        r = gov.check_entry_risk_budget(
            "WDOU26", "SELL", sl_pts=5000, volume=1.0,
            config=CONFIG_RISCO, open_positions=[])
        assert r["ok"] is True

    def test_coerencia_mesma_direcao_segue_orcamento(self):
        # Entrada na MESMA direção da exposição não é churn — segue o fluxo
        # normal de orçamento (2 × R$50 = R$100 ≤ 250 → ok)
        aberto = [_pos("WDOU26", "SELL", 1.0, 5149.0, 5154.0)]
        r = gov.check_entry_risk_budget(
            "WDOU26", "SELL", sl_pts=5000, volume=1.0,
            config=CONFIG_RISCO, open_positions=aberto)
        assert r["ok"] is True
        assert r["reason"] == ""

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
        id INTEGER PRIMARY KEY, symbol TEXT, timeframe TEXT, strategy TEXT,
        net_pnl REAL, entry_time TEXT, exit_time TEXT, exit_reason TEXT)""")
    agora = datetime.now()
    for i, row in enumerate(rows):
        sym, tf, pnl = row[0], row[1], row[2]
        strat = row[3] if len(row) > 3 else f"S_{tf}_{i % 97}"
        ts = (agora - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO trades (symbol, timeframe, strategy, net_pnl, "
            "entry_time, exit_time, exit_reason) VALUES (?,?,?,?,?,?,?)",
            (sym, tf, strat, pnl, ts, ts, "SL_SERVIDOR"))
    conn.commit()
    conn.close()
    return db


class TestLiveKillSwitch:
    CFG = {"strategy_by_tf": {"WDO_M15": "ADX_TREND", "BIT_M15": "DIVERGENCE_RSI",
                              "WIN_M15": "AGI4_WIN_121815"},
           "disabled_timeframes": ["WIN_M5"]}

    def test_bleed_e_churn_detectados(self, tmp_path):
        rows = (
            [("WDOU26", "M15", -25.0, "ADX_TREND")] * 12      # -300 → live_bleed
            + [("BITQ26", "M15", -1.0, "DIVERGENCE_RSI")] * 30  # -30 → live_churn
            + [("WINZ26", "M15", 10.0, "AGI4_WIN_121815")] * 12  # +120 → nada
        )
        db = _seed_db(tmp_path, rows)
        dec = {d["pair"]: d for d in lks.evaluate(self.CFG, db_path=db)
               if d.get("pair")}
        assert dec["WDO_M15"]["rule"] == "live_bleed"
        assert dec["WDO_M15"]["pnl"] == pytest.approx(-300.0)
        assert dec["BIT_M15"]["rule"] == "live_churn"
        assert "WIN_M15" not in dec

    def test_poucos_trades_nao_mata(self, tmp_path):
        rows = [("WDOU26", "M15", -285.0, "ADX_TREND")] * 2  # incidente real: n=2 < min
        db = _seed_db(tmp_path, rows)
        assert lks.evaluate(self.CFG, db_path=db) == []

    def test_par_ja_desativado_ignorado(self, tmp_path):
        rows = [("WDOU26", "M15", -25.0, "ADX_TREND")] * 12
        db = _seed_db(tmp_path, rows)
        cfg = dict(self.CFG, disabled_timeframes=["WIN_M5", "WDO_M15"])
        assert lks.evaluate(cfg, db_path=db) == []

    def test_env_desliga(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VT_AGI_LIVE_KILL", "0")
        rows = [("WDOU26", "M15", -25.0, "ADX_TREND")] * 12
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


# ─────────────────────────────────────────────────────────────────────
# 4. Wave 894 (15/09) — kill mais rápido + kill por ESTRATÉGIA + face
#    intradia (blocked_for_entry). Motivação: setembro -R$802 (7/9 pregões
#    negativos); WDO_M5 sangrou 6 pregões antes do gate n≥10/-R$200 pegar;
#    AGI4_BIT_121102 migrou de par com "ficha limpa".
# ─────────────────────────────────────────────────────────────────────

class TestLiveKillWave894:
    CFG_WDO = {"strategy_by_tf": {"WDO_M5": "AGI4_BIT_121102",
                                  "WIN_M15": "AGI4_BIT_121102",
                                  "BIT_M30": "AGI4_BIT_121102",
                                  "BIT_M15": "DIVERGENCE_RSI"},
               "disabled_timeframes": ["WDO_M5"]}

    def test_defaults_mais_rapidos_n4_m120(self, tmp_path):
        # Sequência real do WDO_M5 em 09-10/09: 4 trades, -R$167 — o gate
        # antigo (n≥10, -R$200) não pegava; o novo (n≥4, -R$120) pega.
        rows = [("WDOV26", "M5", -42.0, "AGI4_BIT_121102")] * 4
        db = _seed_db(tmp_path, rows)
        cfg = dict(self.CFG_WDO, disabled_timeframes=[])
        dec = [d for d in lks.evaluate(cfg, db_path=db) if d.get("pair")]
        assert dec and dec[0]["pair"] == "WDO_M5"
        assert dec[0]["rule"] == "live_bleed"

    def test_sem_falso_positivo_churn_pequeno(self, tmp_path):
        # DIVERGENCE_RSI set/2026: 23 trades, -R$110 — acima de -R$120, vive
        rows = [("BITQ26", "M15", -4.8, "DIVERGENCE_RSI")] * 23
        db = _seed_db(tmp_path, rows)
        assert lks.evaluate(self.CFG_WDO, db_path=db) == []

    def test_strategy_bleed_apanha_migracao_de_par(self, tmp_path):
        # AGI4_BIT_121102 sangrou -R$160/6t (n≥5, ≤-R$150) — WDO_M5 já está
        # disabled, mas a estratégia RODA em WIN_M15/BIT_M30 → os dois pares
        # ativos que a usam entram na decisão (a estratégia não recomeça
        # "limpa" em outro par).
        rows = ([("WDOV26", "M5", -30.0, "AGI4_BIT_121102")] * 5
                + [("BITQ26", "M30", -10.0, "AGI4_BIT_121102")])
        db = _seed_db(tmp_path, rows)
        dec = [d for d in lks.evaluate(self.CFG_WDO, db_path=db)
               if d.get("strategy")]
        assert len(dec) == 1
        assert dec[0]["rule"] == "live_strategy_bleed"
        assert dec[0]["strategy"] == "AGI4_BIT_121102"
        assert set(dec[0]["pairs"]) == {"WIN_M15", "BIT_M30"}
        assert dec[0]["pnl"] == pytest.approx(-160.0)

    def test_strategy_saudavel_nao_morre(self, tmp_path):
        rows = ([("BITQ26", "M15", 5.0, "DIVERGENCE_RSI")] * 10
                + [("BITQ26", "M15", -5.0, "DIVERGENCE_RSI")] * 10)  # 0.0
        db = _seed_db(tmp_path, rows)
        assert lks.evaluate(self.CFG_WDO, db_path=db) == []

    def test_blocked_for_entry_face_do_daemon(self, tmp_path):
        rows = ([("WDOV26", "M5", -42.0, "AGI4_BIT_121102")] * 4
                + [("BITQ26", "M15", -4.8, "DIVERGENCE_RSI")] * 23)
        db = _seed_db(tmp_path, rows)
        cfg = dict(self.CFG_WDO,
                   strategy_by_tf={"WDO_M5": "AGI4_BIT_121102",
                                   "BIT_M15": "DIVERGENCE_RSI"},
                   disabled_timeframes=[])
        blk = lks.blocked_for_entry(cfg, db_path=db)
        # WDO_M5: live_bleed (par) — BIT_M15: -110 > -120 no gate de par,
        # mas o churn n≥30/-R$20 não bate (n=23) → só o par sangrado.
        assert blk["pairs"] == {"WDO_M5"}
        # AGI4_BIT_121102: n=4 < 5 → estratégia ainda não morre
        assert "AGI4_BIT_121102" not in blk["strategies"]
        # Fail-open com banco inexistente
        assert lks.blocked_for_entry(cfg, db_path=tmp_path / "nada.db") == {
            "pairs": set(), "strategies": set()}
