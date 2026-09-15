#!/usr/bin/env python3
"""Tests Wave 894C (15/09) — gatilhos de lucro: piso anti-pró-ciclo e banda
absoluta da ativação da trava.

Motivação (dados reais ago→set/2026): o piso estrutural da Wave 893
(1.5× perda média dos dias negativos) é PRÓ-CÍCLICO — dia ruim → piso sobe →
alvo inalcançável → trava nunca arma. O alvo escalou 100→400 e setembro teve
2 travas de lucro vs 20 em agosto (alvo ~150). Bruno: "lucro acima de R$60
satisfatório; prejuízo estava na casa dos 300 — precisa achar meio termo".

- Piso: min(1.5×perda média, 1.2×|soft_daily_loss|) — a perda de referência
  passa a ser a LIMITADA pelo soft stop.
- Ativação da trava: banda absoluta [0.30, 0.50] no valor FINAL (grid ganhou
  0.3 → R$60 num alvo de 200).
Hermético: nenhum DB/MT5 (calibrate_* são funções puras sobre listas).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from optimization.agi_v4 import risk_calibrator as rc  # noqa: E402


def _dias_perdedores(n_dias: int = 6, perda_dia: float = -300.0) -> list[dict]:
    """Dias com perda líquida `perda_dia` (3 trades de perda por dia)."""
    per_trade = perda_dia / 3.0
    return [{"root": "WIN", "tf": "M15", "pnl": per_trade, "day": f"d{i}"}
            for i in range(n_dias) for _ in range(3)]


class TestPisoAntiProciclico:
    def test_perda_grande_nao_mais_infla_piso(self):
        # Espiral do set/2026: perda média 300 → piso antigo 1.5×300=450
        # (grid 500). Com soft −150: min(450, 180)=180 → grid 200.
        cfg = {"profit_lock_min_target": 250.0, "soft_daily_loss": -150.0,
               "trailing_target_per_lot": 200.0, "trailing_activation_pct": 0.3}
        r = rc.calibrate_profit_target(cfg, _dias_perdedores(), None)
        assert r["status"] == "calibrado"
        assert r["floor_basis"]["avg_daily_loss"] == pytest.approx(300.0)
        assert r["floor_basis"]["soft_daily_loss_cap"] == pytest.approx(180.0)
        assert r["floor"] == 200

    def test_soft_zero_restaura_piso_antigo(self):
        # soft_daily_loss=0 (off) → piso volta a ser 1.5×perda média (450→500)
        cfg = {"profit_lock_min_target": 250.0, "soft_daily_loss": 0,
               "trailing_target_per_lot": 200.0, "trailing_activation_pct": 0.3}
        r = rc.calibrate_profit_target(cfg, _dias_perdedores(), None)
        assert r["floor_basis"]["soft_daily_loss_cap"] is None
        assert r["floor"] == 500

    def test_soft_default_150_quando_ausente(self):
        # Sem chave no config: cap de 180 vale mesmo assim (consistente com
        # o default −150 do daemon, Wave 894).
        cfg = {"profit_lock_min_target": 250.0,
               "trailing_target_per_lot": 200.0, "trailing_activation_pct": 0.3}
        r = rc.calibrate_profit_target(cfg, _dias_perdedores(), None)
        assert r["floor_basis"]["soft_daily_loss_cap"] == pytest.approx(180.0)
        assert r["floor"] == 200

    def test_piso_trailing_ainda_levanta(self):
        # Wave 893 intacta: piso nunca abaixo da ativação do trailing 1 lote
        # (1.2 × 0.3 × 200 = 72 → piso de perdas 180 domina, mas em janela
        # fraca o termo do trailing é quem sustenta — cenário com perdas míni).
        cfg = {"profit_lock_min_target": 100.0, "soft_daily_loss": -150.0,
               "trailing_target_per_lot": 250.0, "trailing_activation_pct": 0.5}
        r = rc.calibrate_profit_target(cfg, _dias_perdedores(perda_dia=-9.0), None)
        # floor perdas: min(1.5×9, 180)=13.5 → ABS_MIN 100 → trailing 150 → 150
        assert r["floor"] == 150

    def test_alvo_estavel_no_regime_novo(self):
        # Config 894C (200) com piso 200 → cur < floor é False e gain do
        # contrafactual em dias só-perda é 0 → nada a aplicar (sem churn).
        cfg = {"profit_lock_min_target": 200.0, "soft_daily_loss": -150.0,
               "trailing_target_per_lot": 200.0, "trailing_activation_pct": 0.3}
        r = rc.calibrate_profit_target(cfg, _dias_perdedores(), None)
        assert r["best"] == 200
        assert r["apply"] is False


class TestBandaAtivacao:
    def test_grid_tem_0_3(self):
        # R$60 num alvo de 200 (o número do Bruno) é representável no grid
        assert 0.3 in rc.ACTIVATION_GRID
        assert rc.ACTIVATION_ABS_MIN == 0.30
        assert rc.ACTIVATION_ABS_MAX == 0.50

    def test_banda_clampa_ativacao_tardia(self):
        # Dia de tendência: contrafactual quer ativação alta (≥0.5), mas a
        # histerese de passo (0.7×..1.3× de cur=0.3) limita o salto → 0.4 na
        # primeira sessão (0.5 só na seguinte); 0.4 já está na banda.
        seq = (20.0, 20.0, 20.0, 20.0, 20.0, 20.0)
        live = [{"root": "WIN", "tf": "M15", "pnl": p, "day": f"u{i}"}
                for i in range(6) for p in seq]
        cfg = {"trailing_target_per_lot": 200.0, "trailing_activation_pct": 0.3}
        r = rc.calibrate_lock_activation(cfg, live, None)
        assert r["best_raw"] >= 0.5
        assert r["best"] == 0.4
        assert r["band_clamped"] is False

    def test_ativacao_0_3_valida_sem_clamp(self):
        # Dias de devolução clássica: 0.3 está dentro da banda e a histerese
        # (0.3 é o próprio cur) não impede → sem clamp.
        seq = (30.0, 30.0, 30.0, -30.0, -30.0, -30.0)
        live = [{"root": "WIN", "tf": "M15", "pnl": p, "day": f"d{i}"}
                for i in range(6) for p in seq]
        cfg = {"trailing_target_per_lot": 200.0, "trailing_activation_pct": 0.3}
        r = rc.calibrate_lock_activation(cfg, live, None)
        assert r["best_raw"] == 0.4        # 90/dia (cruza 80 no pico 90)
        assert r["best"] == 0.4            # histerese 0.7×..1.3× de 0.3
        assert r["band_clamped"] is False
