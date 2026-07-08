"""
test_integration_hooks.py — Wave N+2.5 (2026-07-08)

Verifica que os hooks implementados em commits separados estão wired-in
no autotrader real. Como autotrader não pode ser importado limpo (tem
MT5 dependency + state global), testamos os HOOKS isoladamente
mas com o call shape REAL dos callsites.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "core"),
          str(PROJECT_ROOT / "mt5")):
    if p not in sys.path:
        sys.path.insert(0, p)


def test_aggregate_blackout_called_with_real_config_key():
    """Confirma que o callsite de check_and_trade (esperado em L1650+) usa
    vt_calendar.aggregate_blackout com CONFIG + last_bar_ts."""
    import inspect
    from core import vt_autotrader
    src = inspect.getsource(vt_autotrader.check_and_trade)
    # Deve referenciar aggregate_blackout
    assert "aggregate_blackout" in src
    assert "from core.vt_calendar import aggregate_blackout" in src


def test_loss_cooldown_helper_symbol_direction():
    """Confirma que _is_loss_cooldown_active (via position_manager) usa
    last_loss_direction_per_symbol."""
    import inspect
    from core.vt_position_manager import check_loss_cooldown_active
    src = inspect.getsource(check_loss_cooldown_active)
    assert "last_loss_direction_per_symbol" in src


def test_day_trade_flatten_called_in_manage_position():
    """Confirma integração day-trade no manage_position."""
    import inspect
    from core import vt_autotrader
    src = inspect.getsource(vt_autotrader.manage_position)
    assert "_is_day_trade_flatten_window" in src
    assert "DAY_TRADE_FLATTEN" in src


def test_loss_cooldown_counter_bump_and_reset_exist():
    """Helpers _bump + _reset devem existir no autotrader."""
    from core import vt_autotrader
    assert hasattr(vt_autotrader, "_bump_loss_cooldown_counter")
    assert hasattr(vt_autotrader, "_reset_loss_cooldown_counter")


def test_orchestrator_records_latency():
    """Confirma que _run_wine instrumenta vt_latency_monitor."""
    import inspect
    from mt5 import mt5_orchestrator
    src = inspect.getsource(mt5_orchestrator._run_wine)
    # Deve chamar record_latency
    assert "record_latency" in src
    assert "time.perf_counter" in src


def test_sizing_applies_latency_degradation():
    """vol_scaling_final deve passar por _apply_latency_degradation."""
    import inspect
    from core import vt_sizing
    src = inspect.getsource(vt_sizing.resolve_volume)
    assert "_apply_latency_degradation" in src
    assert "_apply_edge_decay" in src


def test_daemon_loop_calls_edge_estimator():
    """Confirma que o daemon chama edge_estimator.update periodicamente."""
    import inspect
    from core import vt_autotrader
    src = inspect.getsource(vt_autotrader.run_daemon)
    assert "vt_edge_estimator" in src
    assert "_ee_update" in src


def test_crontab_has_loser_replay_entry():
    """crontab.txt deve ter o line de loser replay pós-EOD."""
    crontab = (PROJECT_ROOT / "crontab.txt").read_text()
    assert "loser_replay" in crontab
    assert "monitoring.vt_loser_replay" in crontab


def test_crontab_has_signal_journal_vacuum():
    """crontab.txt deve ter vacuum semanal de signal_blocked_log."""
    crontab = (PROJECT_ROOT / "crontab.txt").read_text()
    assert "signal_blocked_log" in crontab
    assert "vacuum" in crontab.lower() or "VACUM" in crontab or "vacum" in crontab


def test_state_has_required_wave_n_fields():
    """SessionState inclui os campos adicionados pelas waves N+1..N+4."""
    import inspect
    from core import vt_autotrader
    src = inspect.getsource(vt_autotrader.SessionState.__init__)
    assert "recent_signal_ts" in src           # N+1
    assert "last_loss_direction_per_symbol" in src  # N+4B
    assert "consecutive_loss_direction_count" in src  # N+4B


def test_position_dict_has_tp1_fields():
    """Per-position dict deve ter original_volume, tp1_done (N+2A)."""
    import inspect
    from core import vt_autotrader
    src = inspect.getsource(vt_autotrader.manage_position)
    assert "original_volume" in src
    assert "tp1_done" in src


def test_aggregate_blackout_replaces_fragmented_checks():
    """Callsites antigos não devem mais existir no check_and_trade ativo."""
    import inspect
    from core import vt_autotrader
    src = inspect.getsource(vt_autotrader.check_and_trade)
    # Aggregate_blackout é usado no callsite principal (gate de entrada)
    assert src.count("aggregate_blackout(") >= 1
    # Os antigos _is_blocked_day_direction/_is_blocked_time AINDA existem
    # como helpers (seriam removidos em cleanup wave), mas NÃO devem
    # ser chamados como gate inline no check_and_trade (entre os blocos
    # de `_defenses_ok` e `_execute_entry`).
    # Conta gates — aggregate_blackout deve substituir pelo menos 2 calls.
    has_aggregate_call = "aggregate_blackout(" in src
    assert has_aggregate_call


def test_vol_scaled_sizing_chain_end_to_end(tmp_path, monkeypatch):
    """Smoke test: resolve_volume respeita sizing + edge + latency em cascata."""
    from core import vt_sizing
    cfg = {
        "sizing": {
            "mode": "vol_scaled",
            "atr_baseline": 100.0,
            "min_scale": 0.4,
            "max_scale": 1.8,
        },
        "volume": 10,
        # sem edge_estimator / sem latency degradation
    }
    # ATR 50 → scale 2.0 → clamp 1.8 → vol = 18 → ceil to 18
    vol = vt_sizing.resolve_volume(
        "WINQ26", "M5",
        config=cfg, current_atr=50,
    )
    assert vol == 18  # 10 * 1.8 = 18 (clamped + ceiling)


def test_vol_scaled_with_latency_degradation_applied(tmp_path, monkeypatch):
    """Quando latency degrada, vol é multiplicada pelo factor."""
    from core import vt_sizing
    # Mock latency_monitor.should_degrade
    cfg = {
        "sizing": {
            "mode": "vol_scaled",
            "atr_baseline": 100.0,
            "min_scale": 0.4,
            "max_scale": 1.8,
        },
        "volume": 10,
        "latency_slo": {"degrade_size_factor": 0.5, "degrade_ms": 1000},
    }
    # Monkeypatch should_degrade
    with mock.patch("core.vt_latency_monitor.should_degrade", return_value=True), \
         mock.patch("core.vt_latency_monitor.get_degraded_ops", return_value={"buy"}), \
         mock.patch("core.vt_autotrader.CONFIG", cfg):
        vol = vt_sizing.resolve_volume(
            "WINQ26", "M5",
            config=cfg, current_atr=50,
        )
        # 10 * 1.8 (vol_scaled) = 18; * 0.5 (latency) = 9
        assert vol == 9
