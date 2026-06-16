#!/usr/bin/env python3
"""TDD — Validator inteligente v2: deve usar histórico + cache + contexto.

Casos cobertos:
  1. Cache: mesma symbol+tf+strategy+sl_band em <5min → não chama LLM (reusa)
  2. Histórico: setup com WR<30% nos últimos 30 dias → sugere NÃO abrir (early return)
  3. Contexto: PnL diário < -R$1000 → NÃO sugere aumentar SL
  4. Contexto: 3+ losses seguidas no symbol → NÃO sugere aumentar SL
  5. Performance: LLM chamada apenas uma vez por minuto por symbol
"""
import json
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))


class TestValidatorCache(unittest.TestCase):
    """Validator deve cachear respostas LLM por 5min."""

    def setUp(self):
        from vt_order_validator_v2 import _llm_cache
        _llm_cache.clear()

    def test_cache_hit_no_llm_call(self):
        """Segunda chamada dentro de 5min com mesmo symbol+tf+strategy → sem LLM."""
        from vt_order_validator_v2 import ValidatorV2, _llm_cache

        # Limpar cache
        _llm_cache.clear()

        with patch("vt_order_validator_v2._ask_llm") as mock_llm, \
             patch("vt_order_validator_v2.historical_setup_stats") as mock_hist, \
             patch("vt_order_validator_v2.get_daily_pnl") as mock_pnl, \
             patch("vt_order_validator_v2.get_consecutive_losses") as mock_streak:
            mock_llm.return_value = json.dumps({
                "sl_sugerido": 1000,
                "resumo": "test"
            })
            mock_hist.return_value = {"n_trades": 30, "win_rate": 60.0, "avg_pnl": 10.0,
                                      "total_pnl": 300.0, "avg_duration_min": 15}
            mock_pnl.return_value = 0.0
            mock_streak.return_value = 0

            v = ValidatorV2()
            order = {
                "symbol": "BITM26", "direction": "SELL", "tf": "M15",
                "strategy": "EMA_PULLBACK",
                "entry_price": 337920, "sl_pts": 50000, "atr": 1671,
            }

            # 1ª chamada → LLM
            r1 = v.validate(order, use_llm=True)
            self.assertEqual(mock_llm.call_count, 1)

            # 2ª chamada com mesmo setup → cache hit, sem LLM
            r2 = v.validate(order, use_llm=True)
            self.assertEqual(mock_llm.call_count, 1)  # não chamou de novo

    def test_cache_expires_after_5min(self):
        """Depois de 5min, cache expira e LLM é chamada de novo."""
        from vt_order_validator_v2 import ValidatorV2, _llm_cache

        _llm_cache.clear()

        with patch("vt_order_validator_v2._ask_llm") as mock_llm, \
             patch("vt_order_validator_v2.historical_setup_stats") as mock_hist, \
             patch("vt_order_validator_v2.get_daily_pnl") as mock_pnl, \
             patch("vt_order_validator_v2.get_consecutive_losses") as mock_streak:
            mock_llm.return_value = json.dumps({"sl_sugerido": 1000, "resumo": "x"})
            mock_hist.return_value = {"n_trades": 30, "win_rate": 60.0, "avg_pnl": 10.0,
                                      "total_pnl": 300.0, "avg_duration_min": 15}
            mock_pnl.return_value = 0.0
            mock_streak.return_value = 0
            v = ValidatorV2()
            order = {"symbol": "WINM26", "direction": "SELL", "tf": "M30",
                     "strategy": "BOLLINGER", "entry_price": 170500,
                     "sl_pts": 1000, "atr": 800}

            # 1ª chamada
            v.validate(order, use_llm=True)
            self.assertEqual(mock_llm.call_count, 1)

            # Forçar expiração do cache
            for k in list(_llm_cache.keys()):
                _llm_cache[k]["ts"] = datetime.now() - timedelta(minutes=10)

            # 2ª chamada → cache expirado, LLM chamada
            v.validate(order, use_llm=True)
            self.assertEqual(mock_llm.call_count, 2)


class TestValidatorHistoricalContext(unittest.TestCase):
    """Validator deve consultar histórico do setup no DB."""

    def test_losing_setup_skips_llm(self):
        """Setup com WR<30% nos últimos 30 dias → sugere NÃO abrir, sem LLM."""
        from vt_order_validator_v2 import ValidatorV2, _llm_cache, historical_setup_stats

        _llm_cache.clear()
        # Popular DB com histórico ruim pra esse setup
        self._insert_test_trades(symbol="BITM26", tf="M15", strategy="EMA_PULLBACK",
                                  direction="SELL", n_trades=12, win_rate=20.0)

        with patch("vt_order_validator_v2._ask_llm") as mock_llm:
            v = ValidatorV2()
            order = {"symbol": "BITM26", "direction": "SELL", "tf": "M15",
                     "strategy": "EMA_PULLBACK", "entry_price": 337920,
                     "sl_pts": 50000, "atr": 1671}

            result = v.validate(order, use_llm=False)
            self.assertFalse(mock_llm.called,
                             "LLM não deve ser chamada para setup com WR<30%")
            self.assertFalse(result["valid"])
            self.assertIn("HISTORICAL_LOSING", str([a["type"] for a in result["alerts"]]))

    def test_winning_setup_proceeds(self):
        """Setup com WR>50% → LLM é consultada."""
        from vt_order_validator_v2 import ValidatorV2, _llm_cache

        _llm_cache.clear()
        self._insert_test_trades(symbol="DOLN26", tf="H1", strategy="EMA_PULLBACK",
                                  direction="BUY", n_trades=20, win_rate=60.0)

        with patch("vt_order_validator_v2._ask_llm") as mock_llm, \
             patch("vt_order_validator_v2.get_daily_pnl") as mock_pnl, \
             patch("vt_order_validator_v2.get_consecutive_losses") as mock_streak:
            mock_pnl.return_value = 0.0
            mock_streak.return_value = 0
            mock_llm.return_value = json.dumps({"sl_sugerido": 25000, "resumo": "ok"})
            v = ValidatorV2()
            order = {"symbol": "DOLN26", "direction": "BUY", "tf": "H1",
                     "strategy": "EMA_PULLBACK", "entry_price": 5073,
                     "sl_pts": 15000, "atr": 18.5}

            v.validate(order, use_llm=True)
            self.assertTrue(mock_llm.called)

    @staticmethod
    def _insert_test_trades(symbol, tf, strategy, direction, n_trades, win_rate):
        """Helper: popula vt_trades.db com trades sintéticos para o setup."""
        import random
        random.seed(42)  # determinístico
        conn = sqlite3.connect(str(PROJECT_DIR / "vt_trades.db"), timeout=30)
        cur = conn.cursor()
        # Limpar trades sintéticos anteriores do mesmo setup
        cur.execute("""
            DELETE FROM trades
            WHERE symbol LIKE ?
              AND timeframe = ?
              AND strategy = ?
              AND direction = ?
              AND entry_time >= '2026-01-01'
        """, (f"{symbol}%", tf, strategy, direction))
        conn.commit()

        # Gerar datas recentes (últimos 28 dias) pra cair dentro do cutoff de 30d
        base_ts = datetime.now() - timedelta(days=28)
        # Distribuição FIXA (não random) pra WR ser determinístico
        # 20% wins, 80% losses → WR garantido < 30% com n_trades >= 10
        n_wins = max(1, int(n_trades * (win_rate / 100)))
        n_losses = n_trades - n_wins
        outcomes = [True] * n_wins + [False] * n_losses
        for i in range(n_trades):
            is_win = outcomes[i] if i < len(outcomes) else False
            entry = 100 + i
            sl = entry - 50
            # Para SELL: lucro quando exit < entry. Para BUY: lucro quando exit > entry.
            if direction == "BUY":
                exit_p = entry + 30 if is_win else entry - 50
                net = (exit_p - entry) * 0.5
            else:  # SELL
                exit_p = entry - 30 if is_win else entry + 50
                net = (entry - exit_p) * 0.5
            entry_time = base_ts + timedelta(days=i)
            exit_time = entry_time + timedelta(minutes=10)
            cur.execute("""
                INSERT INTO trades
                (symbol, direction, timeframe, strategy, entry_time, exit_time,
                 entry_price, entry_sl, exit_price, gross_pnl, fees, net_pnl,
                 exit_reason, volume, signal_detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                f"{symbol}N99", direction, tf, strategy,
                entry_time.isoformat(), exit_time.isoformat(),
                entry, sl, exit_p,
                net, 0.5, net - 0.5,
                "SL_SERVIDOR" if not is_win else "TRAIL",
                1,  # volume
                None,  # signal_detail
            ))
        conn.commit()
        conn.close()


class TestValidatorDailyContext(unittest.TestCase):
    """Validator deve respeitar contexto do dia (PnL, streak, posição)."""

    def test_daily_loss_above_threshold_blocks_sl_increase(self):
        """PnL diário < -R$1000 → não sugere aumentar SL."""
        from vt_order_validator_v2 import ValidatorV2

        with patch("vt_order_validator_v2._ask_llm") as mock_llm, \
             patch("vt_order_validator_v2.get_daily_pnl") as mock_pnl, \
             patch("vt_order_validator_v2.get_consecutive_losses") as mock_streak, \
             patch("vt_order_validator_v2.historical_setup_stats") as mock_hist:
            mock_pnl.return_value = -1500.0  # drawdown sério
            mock_streak.return_value = 0
            mock_hist.return_value = {"n_trades": 30, "win_rate": 60.0, "avg_pnl": 10.0,
                                      "total_pnl": 300.0, "avg_duration_min": 15}
            mock_llm.return_value = json.dumps({"sl_sugerido": 100000, "resumo": "aumenta"})

            v = ValidatorV2()
            order = {"symbol": "BITM26", "direction": "SELL", "tf": "M15",
                     "strategy": "EMA_PULLBACK", "entry_price": 337920,
                     "sl_pts": 50000, "atr": 1671}

            result = v.validate(order, use_llm=True)
            # Se LLM aumentar SL > 1.3×, validator deve rejeitar
            if result.get("suggested_action"):
                new_sl = result["suggested_action"]["suggested_sl"]
                self.assertLessEqual(new_sl, order["sl_pts"] * 1.3)

    def test_consecutive_losses_3plus_blocks_sl_increase(self):
        """3+ losses seguidas → não aumenta exposição."""
        from vt_order_validator_v2 import ValidatorV2

        with patch("vt_order_validator_v2._ask_llm") as mock_llm, \
             patch("vt_order_validator_v2.get_daily_pnl") as mock_pnl, \
             patch("vt_order_validator_v2.get_consecutive_losses") as mock_streak, \
             patch("vt_order_validator_v2.historical_setup_stats") as mock_hist:
            mock_pnl.return_value = -200.0
            mock_streak.return_value = 4  # 4 losses seguidas
            mock_hist.return_value = {"n_trades": 30, "win_rate": 60.0, "avg_pnl": 10.0,
                                      "total_pnl": 300.0, "avg_duration_min": 15}
            mock_llm.return_value = json.dumps({"sl_sugerido": 100000, "resumo": "aumenta"})

            v = ValidatorV2()
            order = {"symbol": "BITM26", "direction": "SELL", "tf": "M15",
                     "strategy": "EMA_PULLBACK", "entry_price": 337920,
                     "sl_pts": 50000, "atr": 1671}

            result = v.validate(order, use_llm=True)
            if result.get("suggested_action"):
                new_sl = result["suggested_action"]["suggested_sl"]
                self.assertLessEqual(new_sl, order["sl_pts"] * 1.3)


if __name__ == "__main__":
    unittest.main()
