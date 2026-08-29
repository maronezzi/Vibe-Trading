"""Tests dos gates de não-regressão do AGI v4 (Wave 880.I, 19/08/2026).

Cobre: walk-forward do candidato, fator mínimo de melhoria, proteção
live-winner, anti-churn com journal, anti-flip de soberania, e a
calibração VARIÁVEL do profit lock com dias censurados reconstruídos do
shadow (forward_sim_trades). Hermético: journal em tmp_path, sem config/DB
real — live_pnl injetado, snapshots isolados via monkeypatch do _PROJECT_ROOT.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from optimization.agi_v4 import non_regression as nr  # noqa: E402
from optimization.agi_v4 import risk_calibrator as rc  # noqa: E402


@pytest.fixture(autouse=True)
def _journal_isolado(tmp_path, monkeypatch):
    """Journal em tmp_path + snapshots fictícios — nunca o repo real."""
    monkeypatch.setattr(nr, "JOURNAL_PATH", tmp_path / "journal.json")
    monkeypatch.setattr(nr, "_PROJECT_ROOT", tmp_path)
    return tmp_path


def _cand(windows=((10, 150.0), (10, 80.0), (10, 120.0), (8, 60.0)),
          pnl=1000.0):
    """Candidato sintético: 4 janelas (n_trades, total_pnl)."""
    wf = [{"n_trades": n, "total_pnl": p} for n, p in windows]
    return {"strategy": "X", "params": {}, "walk_forward": wf,
            "full": {"total_pnl": pnl}}


LIVE_OK = {"WIN_M15": {"pnl": 0.0, "n": 9, "wins": 4}}          # não-winner
LIVE_WINNER = {"WIN_M15": {"pnl": 174.0, "n": 12, "wins": 7}}   # winner


class TestWfFromCandidate:
    def test_consistencia_calculada(self):
        wf = nr.wf_from_candidate(_cand())
        assert wf["ok"] and wf["n_judged"] == 4 and wf["n_positive"] == 4
        assert wf["consistency"] == 1.0

    def test_janela_com_poucos_trades_nao_e_julgada(self):
        wf = nr.wf_from_candidate(_cand(windows=((2, 999.0), (10, -50.0))))
        assert wf["n_judged"] == 1 and wf["n_positive"] == 0

    def test_sem_walk_forward_rejeita(self):
        assert nr.wf_from_candidate({"full": {}})["ok"] is False


class TestGateSwap:
    def test_happy_path_aprovado(self):
        ok, gate, _ = nr.gate_swap(
            "WSP_M15", _cand(), baseline_pnl=500.0, cand_score=1000.0,
            base_score=500.0, session="s1", live_pnl_by_pair=LIVE_OK)
        assert ok, gate

    def test_walk_forward_abaixo_da_regua(self):
        # 2 de 4 janelas positivas = 50% < 75%
        cand = _cand(windows=((10, 100.0), (10, -60.0), (10, 90.0), (8, -40.0)))
        ok, gate, _ = nr.gate_swap(
            "WSP_M15", cand, 500.0, 1000.0, 500.0, "s1", LIVE_OK)
        assert not ok and gate == "wf_below_bar"

    def test_melhoria_marginal_rejeitada(self):
        # cand 600 vs baseline 500 → 1.2x < 1.3x
        ok, gate, reason = nr.gate_swap(
            "WSP_M15", _cand(), 500.0, 600.0, 500.0, "s1", LIVE_OK)
        assert not ok and gate == "marginal_improvement"

    def test_live_winner_exige_2x(self):
        # 1.5x sobre baseline: passa régua comum (1.3x), mas WIN_M15 está
        # lucrando +174 live → exige 2.0x
        ok, gate, reason = nr.gate_swap(
            "WIN_M15", _cand(), 500.0, 750.0, 500.0, "s1", LIVE_WINNER)
        assert not ok and gate == "marginal_improvement"
        assert "live-winner" in reason

    def test_live_winner_exige_wf_perfeito(self):
        # 3 de 4 janelas positivas (75%): passa a régua comum, não a de winner
        cand = _cand(windows=((10, 100.0), (10, -20.0), (10, 90.0), (8, 40.0)))
        ok, gate, _ = nr.gate_swap(
            "WIN_M15", cand, 500.0, 1500.0, 500.0, "s1", LIVE_WINNER)
        assert not ok and gate == "live_winner_wf"

    def test_churn_exige_2x_a_evidencia_anterior(self):
        nr.append_journal({"kind": "swap", "pair": "WSP_M15",
                           "from": "A", "to": "B", "pnl_claimed": 500.0,
                           "session": "sessao_anterior"})
        # cand 700 < 2x500 → churn
        ok, gate, _ = nr.gate_swap(
            "WSP_M15", _cand(), 100.0, 700.0, 100.0, "s_atual", LIVE_OK)
        assert not ok and gate == "churn_cooldown"
        # cand 1200 >= 2x500 → aprova
        ok, gate, _ = nr.gate_swap(
            "WSP_M15", _cand(), 100.0, 1200.0, 100.0, "s_atual", LIVE_OK)
        assert ok, gate

    def test_churn_ignora_mesma_sessao(self):
        nr.append_journal({"kind": "swap", "pair": "WSP_M15",
                           "pnl_claimed": 900.0, "session": "mesma"})
        ok, _, _ = nr.gate_swap(
            "WSP_M15", _cand(), 100.0, 950.0, 100.0, "mesma", LIVE_OK)
        assert ok  # iteração interna do AGI não é churn

    def test_churn_janela_expirada(self):
        velho = (datetime.now() - timedelta(days=3)).isoformat(timespec="seconds")
        nr._save_journal([{"ts": velho, "kind": "swap", "pair": "WSP_M15",
                           "pnl_claimed": 900.0, "session": "outra"}])
        ok, _, _ = nr.gate_swap(
            "WSP_M15", _cand(), 100.0, 950.0, 100.0, "s_atual", LIVE_OK)
        assert ok  # >2 dias: fora da janela de churn


class TestAllowFlip:
    def test_u_turn_bloqueado(self):
        nr._save_journal([{"ts": datetime.now().isoformat(timespec="seconds"),
                           "kind": "enable", "pair": "BIT_H1",
                           "session": "outra"}])
        ok, reason = nr.allow_flip("BIT_H1", "disable", "s_atual")
        assert not ok and "U-turn" in reason

    def test_mesma_direcao_direcao_oposta_velha_liberada(self):
        velho = (datetime.now() - timedelta(days=6)).isoformat(timespec="seconds")
        nr._save_journal([{"ts": velho, "kind": "enable", "pair": "BIT_H1",
                           "session": "outra"}])
        ok, _ = nr.allow_flip("BIT_H1", "disable", "s_atual")
        assert ok  # fora da janela de 5d

    def test_mesma_sessao_ignorada(self):
        nr._save_journal([{"ts": datetime.now().isoformat(timespec="seconds"),
                           "kind": "disable", "pair": "BIT_H1",
                           "session": "mesma"}])
        ok, _ = nr.allow_flip("BIT_H1", "enable", "mesma")
        assert ok


class TestJournalSeed:
    def test_semea_dos_snapshots_e_nao_duplica(self, tmp_path):
        s1 = tmp_path / "vt_config.json.snapshot_pre_cron_20260818_120000"
        s2 = tmp_path / "vt_config.json.snapshot_pre_cron_20260818_171000"
        s1.write_text(json.dumps({
            "strategy_by_tf": {"BIT_M15": "A", "WDO_M5": "C"},
            "disabled_timeframes": ["BIT_H1"],
            "day_trade_intent": {"BIT_M15": True}}))
        s2.write_text(json.dumps({
            "strategy_by_tf": {"BIT_M15": "B", "WDO_M5": "C"},
            "disabled_timeframes": [],
            "day_trade_intent": {"BIT_M15": True, "BIT_H1": True}}))
        (tmp_path / "vt_config.json").write_text(s2.read_text())

        j = nr.load_journal()
        kinds = {(e["pair"], e["kind"]) for e in j}
        assert ("BIT_M15", "swap") in kinds      # A → B
        assert ("BIT_H1", "enable") in kinds     # saiu do disabled + intent on
        # segunda carga não duplica
        assert len(nr.load_journal()) == len(j)


class TestProfitTargetVariavel:
    @staticmethod
    def _cenario():
        """Dia 'a' censurado: live truncado pelo lock em ~105 (target 100);
        shadow mostra 12 trades que no live-scale teriam ido a ~300.
        Dias b1..b5 dão a razão de escala live/shadow = 0.5 (mediana)."""
        live = [{"root": "WIN", "tf": "M15", "pnl": 35.0, "day": "a"},
                {"root": "WIN", "tf": "M15", "pnl": 35.0, "day": "a"},
                {"root": "WIN", "tf": "M15", "pnl": 35.0, "day": "a"}]
        live += [{"root": "WIN", "tf": "M15", "pnl": p, "day": f"b{i}"}
                 for i in range(5) for p in (-5.0, 10.0, -8.0)]
        # shadow do dia 'a': própria razão 105/480=0.22 (truncado); b-days 0.5
        shadow = [{"root": "WIN", "tf": "M15", "pnl": 40.0, "day": "a"}
                  for _ in range(12)]
        shadow += [{"root": "WIN", "tf": "M15", "pnl": p, "day": f"b{i}"}
                   for i in range(5) for p in (-10.0, 20.0, -16.0)]
        return live, shadow

    def test_shadow_reconstroi_dia_censurado(self):
        # Sem shadow: 6 dias válidos, todos "planos" p/ o grid → empate → 100.
        # Com shadow: dia 'a' reconstruído chega a ~120+ → alvo maior vence.
        live, shadow = self._cenario()
        cfg = {"profit_lock_min_target": 100.0}
        so_live = rc.calibrate_profit_target(cfg, live, shadow=None)
        com_shadow = rc.calibrate_profit_target(cfg, live, shadow)
        assert so_live["status"] == "calibrado"
        assert so_live["best"] == 100          # vies p/ baixo sem shadow
        assert com_shadow["best"] == 200       # clamp 2x do atual (100→200)
        assert com_shadow["best_raw"] >= 200   # bruto quer mais
        assert com_shadow["shadow_meta"]["n_reconstructed_days"] == 1

    def test_clamp_de_histerese_limita_salto(self):
        # dias só de ganhos (8×~340/dia): bruto ótimo 400 (empate prefere
        # menor); atual 100 → clamp 2x = 200 — passo único, sem salto
        cfg = {"profit_lock_min_target": 100.0}
        live = [{"root": "WIN", "tf": "M15", "pnl": p, "day": f"d{i}"}
                for i in range(6)
                for p in (60.0, 55.0, 50.0, 45.0, 40.0, 35.0, 30.0, 25.0)]
        r = rc.calibrate_profit_target(cfg, live, shadow=None)
        assert r["best_raw"] == 400
        assert r["best"] == 200

    def test_dados_insuficientes_mantem(self):
        r = rc.calibrate_profit_target(
            {"profit_lock_min_target": 100.0},
            [{"root": "W", "tf": "M5", "pnl": 5.0, "day": "d1"}], None)
        assert r["status"] == "dados_insuficientes" and r["keep"] == 100.0

    def test_ratio_de_escala_aplicado(self):
        live, shadow = self._cenario()
        days, meta = rc._merge_with_shadow(live, shadow, cur_target=100.0)
        assert meta["ratio"] == pytest.approx(0.5, abs=0.01)
        assert meta["n_reconstructed_days"] == 1
        # dia 'a' censurado: shadow 480 × ratio 0.5 = 240 (> live 105)
        assert sum(days["a"]) == pytest.approx(240.0, abs=1.0)
        # dia só-shadow seria reescalado também (bug de escala coberto)
        days2, _ = rc._merge_with_shadow(
            live, shadow + [{"root": "W", "tf": "M15", "pnl": 100.0,
                             "day": "z"}], cur_target=100.0)
        assert sum(days2["z"]) == pytest.approx(50.0, abs=0.1)


class TestCalibrateLockActivation:
    """Wave 883.B3 (29/08): sintonia da trava de lucro (número sem chute)."""

    @staticmethod
    def _dias_devolucao(n_dias: int = 6) -> list[dict]:
        """Dias que motivaram a trava: lucro de manhã, devolução à tarde.
        Cada dia: +60 +60 +20 (sobe a ~140) então -40 -40 -40 (devolve)."""
        seq = (60.0, 60.0, 20.0, -40.0, -40.0, -40.0)
        return [{"root": "WIN", "tf": "M15", "pnl": p, "day": f"d{i}"}
                for i in range(n_dias) for p in seq]

    def test_dia_de_devolucao_prefere_travar_cedo(self):
        # per_lot=200, dia: cum 60→120→140 (pico) → devolve até +20.
        # Ativação 0.7 arma exatamente no pico (140) → 140/dia; 0.8+ nunca
        # arma → +20/dia; 0.4-0.6 travam em 120. Ótimo: 0.7.
        cfg = {"trailing_target_per_lot": 200.0, "trailing_activation_pct": 1.0}
        r = rc.calibrate_lock_activation(cfg, self._dias_devolucao(), None)
        assert r["status"] == "calibrado"
        assert r["best_raw"] == 0.7
        assert r["best"] == 0.7          # dentro do clamp [0.7, 1.3] de 1.0
        assert r["apply"] is True
        assert r["gain"] == pytest.approx(720.0)   # (140-20)×6 dias

    def test_histerese_limita_passo(self):
        # dia: cum 50→100→110 → devolve até -10. Níveis 80/100 truncam em
        # +100/dia (empate → menor=0.4); atual 1.0 → clamp 0.7× limita o
        # passo único a 0.7 (variável sem salto)
        seq = (50.0, 50.0, 10.0, -40.0, -40.0, -40.0)
        live = [{"root": "WIN", "tf": "M15", "pnl": p, "day": f"h{i}"}
                for i in range(6) for p in seq]
        cfg = {"trailing_target_per_lot": 200.0, "trailing_activation_pct": 1.0}
        r = rc.calibrate_lock_activation(cfg, live, None)
        assert r["best_raw"] == 0.4
        assert r["best"] == 0.7

    def test_dia_de_tendencia_nao_aptado_a_travar_cedo(self):
        # dia que só sobe: travar cedo CORTA ganho → ótimo = ativação alta
        seq = (20.0, 20.0, 20.0, 20.0, 20.0, 20.0)
        live = [{"root": "WIN", "tf": "M15", "pnl": p, "day": f"u{i}"}
                for i in range(6) for p in seq]
        cfg = {"trailing_target_per_lot": 100.0, "trailing_activation_pct": 0.5}
        r = rc.calibrate_lock_activation(cfg, live, None)
        # com per_lot 100: níveis 40..100; dia soma 120 → só trava em 100
        # (ou nunca); score de 0.4=80 < 1.0=100 → ótimo 1.0, mas clamp
        # 1.3× de 0.5 = 0.65 → melhor célula do grid dentro do clamp
        assert r["status"] == "calibrado"
        assert r["best"] >= 0.6

    def test_dados_insuficientes_mantem(self):
        r = rc.calibrate_lock_activation(
            {"trailing_target_per_lot": 200.0, "trailing_activation_pct": 0.5},
            [{"root": "W", "tf": "M5", "pnl": 5.0, "day": "d1"}], None)
        assert r["status"] == "dados_insuficientes" and r["keep"] == 0.5
