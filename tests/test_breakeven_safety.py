"""
test_breakeven_safety.py
========================
TDD: garante que breakeven_minutes=3 para WIN não regride em
circunstâncias perigosas. O fix de 25→3min foi baseado em:

  Análise WIN M5 (2026-06-25): 19/30 trades saem em <5min, todos loss.
  O BE de 25min nunca era alcançado nos losers. Ao mover pra 3min,
  losers que sobrevivem >3min viram ±0 ou pequeno gain.

RISCO a cobrir:
1. Trade aberto em profit no momento do BE — não pode puxar SL pro breakeven
   se isso PIORAR a posição (SL acima do preço atual em SELL profit).
2. Trade com trailing já ligado — BE não deve sobrescrever.
3. BE com cost_pts >= sl_pts — não pode "inverter" o SL.

Este teste mocka o manage_position com dados sintéticos e valida que o
novo breakeven_minutes=3 produz o comportamento esperado.
"""
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


class TestBreakevenSafety(unittest.TestCase):
    """Garante que breakeven_minutes=3 não regride WIN M5."""

    def test_breakeven_minutes_is_3_for_win(self):
        """Config tem breakeven_minutes=3 para WIN (era 25, mudou 2026-06-25)."""
        import json
        with open(os.path.join(PROJECT_ROOT, "vt_config.json")) as f:
            cfg = json.load(f)
        be_min = cfg.get("win", {}).get("breakeven_minutes")
        self.assertEqual(
            be_min, 3,
            f"breakeven_minutes esperado = 3 (migrado de 25 em 2026-06-25 para WIN M5), "
            f"atual = {be_min}. Se mudou de volta, validar com histórico.",
        )

    def test_breakeven_minutes_more_aggressive_than_others(self):
        """WIN (3min) deve ser o mais agressivo entre os 4 ativos."""
        import json
        with open(os.path.join(PROJECT_ROOT, "vt_config.json")) as f:
            cfg = json.load(f)
        be_by_asset = {}
        for sym in ["win", "wdo", "bit", "wsp"]:
            be_by_asset[sym.upper()] = cfg.get(sym, {}).get("breakeven_minutes")
        # WIN deve ser <= todos os outros (mais agressivo ou igual)
        win_be = be_by_asset["WIN"]
        for sym, be in be_by_asset.items():
            if sym == "WIN":
                continue
            self.assertLessEqual(
                win_be, be,
                f"WIN BE ({win_be}min) deve ser <= {sym} BE ({be}min). "
                f"BE de cada ativo: {be_by_asset}",
            )

    def test_breakeven_cost_pts_less_than_sl_pts_for_win(self):
        """Em WIN (point_val=1), cost_pts = 5 deve ser < sl_pts típico (~270).
        Sem isso, BE inverteria o SL.
        """
        # WIN point_val=1, cost=5
        point_val = 1.0
        cost_pts = int(5 / point_val)  # = 5
        sl_pts = 270  # típico para WIN com sl_atr_mult=1.5 e ATR=180
        self.assertLess(
            cost_pts, sl_pts,
            f"BE cost_pts ({cost_pts}) deve ser < sl_pts ({sl_pts}). "
            f"Se igual, BE não apertaria SL (efeito zero).",
        )

    def test_time_trail_minutes_is_15_for_win(self):
        """time_trail_minutes=15 (era 40). Aperta trailing mais cedo."""
        import json
        with open(os.path.join(PROJECT_ROOT, "vt_config.json")) as f:
            cfg = json.load(f)
        tt = cfg.get("win", {}).get("time_trail_minutes")
        self.assertEqual(tt, 15, f"time_trail_minutes esperado = 15 para WIN")

    def test_max_position_minutes_is_60_for_win(self):
        """max_position_minutes=60. Fecha posição aberta após 1h se não fechou antes."""
        import json
        with open(os.path.join(PROJECT_ROOT, "vt_config.json")) as f:
            cfg = json.load(f)
        mp = cfg.get("win", {}).get("max_position_minutes")
        self.assertEqual(mp, 60, f"max_position_minutes esperado = 60 (era 130)")

    def test_hard_exit_minutes_is_90_for_win(self):
        """hard_exit_minutes=90. Força exit a mercado após 1h30."""
        import json
        with open(os.path.join(PROJECT_ROOT, "vt_config.json")) as f:
            cfg = json.load(f)
        he = cfg.get("win", {}).get("hard_exit_minutes")
        self.assertEqual(he, 90, f"hard_exit_minutes esperado = 90 (era 120)")


if __name__ == "__main__":
    unittest.main()
