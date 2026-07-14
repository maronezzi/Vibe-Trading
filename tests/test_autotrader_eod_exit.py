"""
test_autotrader_eod_exit.py
===========================
Testa a lógica de saída pós-EOD do daemon ``core/vt_autotrader.py``.

O daemon deve fazer ``sys.exit(0)`` quando:
1. ``state.closed == True`` (EOD já rodou em close_all_and_report).
2. ``not is_trading_time()`` (passou das 16:45).
3. Já se passaram >= 10 minutos desde o close_time (janela de reconcile).

Antes dos 10min, deve continuar looping para permitir reconcile DB↔MT5.
Durante trading time normal, nunca sai (state.closed=False).
"""
import sys
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def _should_exit_after_eod(state_closed, is_trading_time, now_hour, now_minute,
                            close_hour=16, close_minute=45):
    """
    Replica a lógica de decisão do bloco EOD_EXIT em run_daemon().

    Retorna True se o daemon deveria fazer sys.exit(0) nesta iteração.
    """
    if not state_closed:
        return False
    if is_trading_time:
        return False
    # Calcula quantos minutos se passaram desde o close_time
    _eod_ref = close_hour * 60 + close_minute
    _now_min = now_hour * 60 + now_minute
    _eod_minutes = _now_min - _eod_ref
    return _eod_minutes >= 10


class TestAutotraderEodExitLogic(unittest.TestCase):
    """Testa a lógica de decisão do bloco EOD_EXIT (extraída isoladamente)."""

    def test_not_closed_no_exit(self):
        """state.closed=False (EOD ainda não rodou): não sai."""
        self.assertFalse(
            _should_exit_after_eod(state_closed=False, is_trading_time=False,
                                   now_hour=17, now_minute=0))

    def test_during_trading_time_no_exit(self):
        """is_trading_time=True: nunca sai (mesmo se closed por bug)."""
        self.assertFalse(
            _should_exit_after_eod(state_closed=True, is_trading_time=True,
                                   now_hour=15, now_minute=0))

    def test_within_reconcile_window_no_exit(self):
        """EOD fechou há < 10min: não sai (ainda em janela de reconcile)."""
        # close_time = 16:45, agora 16:50 → 5min < 10 → não sai
        self.assertFalse(
            _should_exit_after_eod(state_closed=True, is_trading_time=False,
                                   now_hour=16, now_minute=50))

    def test_after_reconcile_window_exits(self):
        """EOD fechou há >= 10min: sai (reconcile concluído)."""
        # close_time = 16:45, agora 16:55 → 10min >= 10 → sai
        self.assertTrue(
            _should_exit_after_eod(state_closed=True, is_trading_time=False,
                                   now_hour=16, now_minute=55))

    def test_well_after_eod_exits(self):
        """EOD fechou há 2h: sai."""
        # close_time = 16:45, agora 18:45 → 120min >= 10 → sai
        self.assertTrue(
            _should_exit_after_eod(state_closed=True, is_trading_time=False,
                                   now_hour=18, now_minute=45))

    def test_next_morning_would_exit_but_cron_restarts(self):
        """Cenário: daemon sobreviveu até a manhã (state.closed ainda True).
        Antes das 09:05, is_trading_time=False e _eod_minutes >> 10 → sairia.
        Na prática o cron faz pkill+restart às 09:00 antes disso importar."""
        # 08:00 da manhã seguinte, state.closed ainda True
        # close_time 16:45, agora 08:00 = 8*60=480, 16*60+45=1005
        # _eod_minutes = 480 - 1005 = -525 (negativo! passou meia-noite)
        # Neste caso o cálculo é incorreto (wraps), mas o _reset_daily_counter
        # reseta state.closed=False ao detectar mudança de dia ANTES deste bloco.
        # Testamos que com closed=False (resetado), não sai:
        self.assertFalse(
            _should_exit_after_eod(state_closed=False, is_trading_time=False,
                                   now_hour=8, now_minute=0))

    def test_exact_10_minute_boundary_exits(self):
        """Exatamente 10min pós-EOD: sai (>= é inclusivo)."""
        # close_time = 16:45, agora 16:55 → exatamente 10min → sai
        self.assertTrue(
            _should_exit_after_eod(state_closed=True, is_trading_time=False,
                                   now_hour=16, now_minute=55))

    def test_9_minutes_no_exit(self):
        """9min pós-EOD: ainda em janela de reconcile, não sai."""
        # close_time = 16:45, agora 16:54 → 9min < 10 → não sai
        self.assertFalse(
            _should_exit_after_eod(state_closed=True, is_trading_time=False,
                                   now_hour=16, now_minute=54))


class TestAutotraderEodExitConfig(unittest.TestCase):
    """Verifica que a config tem os campos que o bloco EOD_EXIT lê."""

    def test_config_has_close_hours(self):
        """vt_config.json deve ter close_hour e close_minute."""
        import json
        cfg_path = os.path.join(PROJECT_ROOT, "vt_config.json")
        with open(cfg_path) as f:
            cfg = json.load(f)
        self.assertIn("close_hour", cfg, "config deve ter close_hour")
        self.assertIn("close_minute", cfg, "config deve ter close_minute")
        self.assertIsInstance(cfg["close_hour"], int)
        self.assertIsInstance(cfg["close_minute"], int)


if __name__ == "__main__":
    unittest.main()
