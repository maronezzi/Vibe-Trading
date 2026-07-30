"""
Testes para o score blended 'hoje conta extra' (Wave hoje-conta-mais) no Stage 5.

Cobre:
- _compute_metrics adiciona today_pnl/today_n_trades (dia da última trade).
- _apply_one usa score blended (total_pnl + today_weight*today_pnl) só no
  comparativo cand-vs-baseline; o gate de lucratividade (cand_pnl>0) segue
  em total_pnl cru.

Os testes mockam evaluate_baseline e usam dry_run=True (sem escrita em disco,
sem promover estratégias geradas).
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from optimization.agi_v4 import stage5_apply, backtest_evaluator  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────

def _cand(pair, total_pnl, today_pnl=0.0, today_n_trades=0, strategy="STRAT_A"):
    return {
        "pair": pair,
        "strategy": strategy,
        "params": {"sl_atr_mult": 1.5},
        "full": backtest_evaluator._compute_metrics([]) if total_pnl is None else {
            **backtest_evaluator._empty_metrics(),
            "n_trades": 20,
            "total_pnl": total_pnl,
            "today_pnl": today_pnl,
            "today_n_trades": today_n_trades,
        },
    }


def _make_baseline_eb(baseline_total, baseline_today=0.0, baseline_today_n=0):
    """evaluate_baseline fake que devolve métricas fixas."""
    def _fake(sym, tf, config):  # noqa: ARG001
        return {
            **backtest_evaluator._empty_metrics(),
            "n_trades": 20,
            "total_pnl": baseline_total,
            "today_pnl": baseline_today,
            "today_n_trades": baseline_today_n,
        }
    return _fake


# Thresholds base (replica defaults de gates.py).
_THRESH = {
    "min_profit_factor": 1.15, "min_win_rate": 0.35, "min_trades": 20,
    "max_drawdown_pct": -25.0, "min_walk_forward_consistency": 0.65,
    "min_30d_projection_improvement": 0.0,
    "today_weight": 0.3, "today_min_trades": 3,
}
_CONFIG = {"strategy_by_tf": {}, "strategy": {}, "params_by_tf": {}}
_CTX = {"dry_run": True}


# ── _compute_metrics ─────────────────────────────────────────────────────────

class TestComputeMetricsToday:
    def test_today_pnl_soma_apenas_dia_da_ultima_trade(self):
        trades = [
            {"pnl": 100.0, "entry_dt": datetime(2026, 7, 29, 10, 0)},
            {"pnl": -50.0, "entry_dt": datetime(2026, 7, 30, 11, 0)},
            {"pnl": 80.0, "entry_dt": datetime(2026, 7, 30, 12, 0)},
        ]
        m = backtest_evaluator._compute_metrics(trades)
        assert m["total_pnl"] == 130.0
        assert m["today_pnl"] == 30.0   # -50 + 80
        assert m["today_n_trades"] == 2

    def test_empty_metrics_tem_campos_today(self):
        m = backtest_evaluator._compute_metrics([])
        assert m["today_pnl"] == 0.0
        assert m["today_n_trades"] == 0

    def test_entry_dt_none_nao_quebra(self):
        trades = [{"pnl": 50.0, "entry_dt": None}]
        m = backtest_evaluator._compute_metrics(trades)
        assert m["today_pnl"] == 0.0
        assert m["today_n_trades"] == 0


# ── _apply_one score blended ─────────────────────────────────────────────────

class TestApplyOneBlendedScore:
    def test_cand_melhor_hoje_vence_apesar_de_pior_em_30d(self, monkeypatch):
        # cand: 30d R$100, hoje R$200 (3 trades). baseline: 30d R$150, hoje R$0.
        # total_pnl cru: cand(100) < base(150) → rejeita (comportamento antigo).
        # blended (w=0.3): cand=100+0.3*200=160 > base=150+0.3*0=150 → APLICA.
        cand = _cand("WIN_M5", 100.0, today_pnl=200.0, today_n_trades=3)
        monkeypatch.setattr(
            "optimization.agi_v4.backtest_evaluator.evaluate_baseline",
            _make_baseline_eb(150.0, baseline_today=0.0, baseline_today_n=0),
        )
        res = stage5_apply._apply_one(cand, _CONFIG, _THRESH, dry_run=True, ctx=_CTX)
        assert res["applied"] is True, res

    def test_peso_zero_reduz_ao_comportamento_original(self, monkeypatch):
        # Mesmo cenário, mas today_weight=0 → compara total_pnl puro → rejeita.
        cand = _cand("WIN_M5", 100.0, today_pnl=200.0, today_n_trades=3)
        th = {**_THRESH, "today_weight": 0.0}
        monkeypatch.setattr(
            "optimization.agi_v4.backtest_evaluator.evaluate_baseline",
            _make_baseline_eb(150.0, baseline_today=0.0, baseline_today_n=0),
        )
        res = stage5_apply._apply_one(cand, _CONFIG, th, dry_run=True, ctx=_CTX)
        assert res["applied"] is False
        assert res["gate"] == "better_baseline_exists"

    def test_poucas_trades_hoje_nao_ativa_bonus(self, monkeypatch):
        # cand hoje tem só 2 trades (< today_min_trades=3) → bônus não conta.
        # total_pnl cru: cand(100) < base(150) → rejeita (ignora o today_pnl).
        cand = _cand("WIN_M5", 100.0, today_pnl=200.0, today_n_trades=2)
        monkeypatch.setattr(
            "optimization.agi_v4.backtest_evaluator.evaluate_baseline",
            _make_baseline_eb(150.0, baseline_today=0.0, baseline_today_n=0),
        )
        res = stage5_apply._apply_one(cand, _CONFIG, _THRESH, dry_run=True, ctx=_CTX)
        assert res["applied"] is False
        assert res["gate"] == "better_baseline_exists"

    def test_gate_lucratividade_continua_em_total_pnl_cru(self, monkeypatch):
        # cand negativo em 30d → rejeitado por must_be_profitable, mesmo com
        # hoje excelente (o blended NÃO relaxa o gate de lucratividade).
        cand = _cand("WIN_M5", -50.0, today_pnl=500.0, today_n_trades=5)
        monkeypatch.setattr(
            "optimization.agi_v4.backtest_evaluator.evaluate_baseline",
            _make_baseline_eb(150.0),
        )
        res = stage5_apply._apply_one(cand, _CONFIG, _THRESH, dry_run=True, ctx=_CTX)
        assert res["applied"] is False
        assert res["gate"] == "must_be_profitable"

    def test_baseline_negativo_cand_positivo_aplica(self, monkeypatch):
        # baseline <= 0: qualquer cand positivo passa (pré-condição base>0 falsa).
        cand = _cand("WIN_M5", 30.0, today_pnl=-100.0, today_n_trades=3)
        monkeypatch.setattr(
            "optimization.agi_v4.backtest_evaluator.evaluate_baseline",
            _make_baseline_eb(-20.0),
        )
        res = stage5_apply._apply_one(cand, _CONFIG, _THRESH, dry_run=True, ctx=_CTX)
        assert res["applied"] is True, res


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
