"""
Testes do Self-Healing Monitor (Fase 2.2).

Cobertura (8 grupos):
  1. health_check saudável quando tudo OK
  2. detecta autotrader morto (pgrep vazio)
  3. detecta MT5 unreachable (status com error_code)
  4. detecta DB locked
  5. detecta state stale (mas LOW — projection-only)
  6. detecta config lock órfão
  7. auto_heal restarta autotrader morto
  8. run_once reporta critical não-curado ao Telegram
  + helpers: HealthReport.healthy/critical_count

Todos os subprocess/sqlite/mt5_status são mockados — produção intocada.
"""
from __future__ import annotations

import sqlite3
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from monitoring import vt_self_heal as sh
from monitoring.vt_self_heal import (
    HealthIssue,
    HealthReport,
    HealResult,
    auto_heal,
    health_check,
    run_once,
)


# ── Helper: mock subprocess.run com mapa de args→CompletedProcess ───────────
def _mock_run(responses, default=None):
    """Cria side_effect p/ subprocess.run baseado no argv[0] ou argv[1]."""
    default = default or subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr="")

    def _side(args, *a, **kw):
        key = args[0]
        if key == "pgrep":
            return responses.get("pgrep", default)
        if key == "crontab":
            return responses.get("crontab", default)
        return default

    return _side


def _cp(stdout="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr="")


# ── 1. health_check tudo OK ─────────────────────────────────────────────────
class TestHealthCheckHealthy:
    def test_all_green_when_everything_up(self, tmp_path, monkeypatch):
        """Sistema saudável → report.healthy=True, 0 issues."""
        # autotrader vivo (pgrep retorna PID), log fresco
        monkeypatch.setattr(sh, "LOG_PATH", tmp_path / "fresh.log")
        (tmp_path / "fresh.log").write_text("x")
        # state fresco
        monkeypatch.setattr(sh, "STATE_PATH", tmp_path / "state.json")
        (tmp_path / "state.json").write_text("{}")
        # sem lock
        monkeypatch.setattr(sh, "CONFIG_LOCK_PATH", tmp_path / "nope.lock")
        # crontab.txt com jobs e crontab -l idêntico
        cron_file = tmp_path / "crontab.txt"
        cron_file.write_text("0 9 * * 1-5 /p/start_autotrader.sh\n")
        monkeypatch.setattr(sh, "CRONTAB_FILE", cron_file)
        monkeypatch.setattr(sh, "DB_PATH", tmp_path / "trades.db")
        # DB válido
        conn = sqlite3.connect(str(tmp_path / "trades.db"))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
        conn.close()

        with patch("subprocess.run", side_effect=_mock_run({
            "pgrep": _cp(stdout="12345\n"),
            "crontab": _cp(stdout="0 9 * * 1-5 /p/start_autotrader.sh\n"),
        })), patch("monitoring.vt_self_heal._check_mt5_reachable",
                   return_value=None):
            report = health_check()

        assert report.healthy is True
        assert report.critical_count == 0
        assert report.issues == []


# ── 2. autotrader morto ─────────────────────────────────────────────────────
class TestAutotraderDead:
    def test_pgrep_empty_reports_critical(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sh, "LOG_PATH", tmp_path / "log.log")
        monkeypatch.setattr(sh, "STATE_PATH", tmp_path / "no_state.json")
        monkeypatch.setattr(sh, "CONFIG_LOCK_PATH", tmp_path / "no.lock")
        monkeypatch.setattr(sh, "CRONTAB_FILE", tmp_path / "crontab.txt")
        (tmp_path / "crontab.txt").write_text("# vazio\n")
        monkeypatch.setattr(sh, "DB_PATH", tmp_path / "t.db")
        conn = sqlite3.connect(str(tmp_path / "t.db"))
        conn.execute("CREATE TABLE t(x INTEGER)")
        conn.commit()
        conn.close()

        with patch("subprocess.run", side_effect=_mock_run({
            "pgrep": _cp(stdout=""),       # autotrader MORTO
            "crontab": _cp(stdout=""),
        })), patch("monitoring.vt_self_heal._check_mt5_reachable",
                   return_value=None):
            report = health_check()

        issue = next(i for i in report.issues if i.type == "autotrader_dead")
        assert issue.severity == sh.SEV_CRITICAL
        assert issue.auto_healable is True


# ── 3. MT5 unreachable ──────────────────────────────────────────────────────
class TestMT5Unreachable:
    def test_status_with_error_code_reports_critical(self):
        with patch("monitoring.vt_self_heal._check_mt5_reachable",
                   return_value=HealthIssue(
                       "mt5_unreachable", sh.SEV_CRITICAL, "NO_ACCOUNT",
                       auto_healable=True)):
            report = health_check()
        # _check_mt5_reachable mockado → outros checks também rodam, mas o
        # MT5 issue está garantido presente
        mt5 = [i for i in report.issues if i.type == "mt5_unreachable"]
        assert len(mt5) == 1
        assert mt5[0].severity == sh.SEV_CRITICAL


# ── 4. DB locked ────────────────────────────────────────────────────────────
class TestDBLocked:
    def test_db_operational_error_reports_high(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sh, "DB_PATH", tmp_path / "trades.db")
        # cria DB mas força erro na leitura
        conn = sqlite3.connect(str(tmp_path / "trades.db"))
        conn.close()

        def boom(*a, **kw):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: _raise_conn(boom))

        issue = sh._check_db_accessible()
        assert issue is not None
        assert issue.type in ("db_locked", "db_error")
        assert issue.severity == sh.SEV_HIGH


class _FakeConn:
    def execute(self, *a, **kw):
        raise sqlite3.OperationalError("database is locked")
    def close(self): pass


def _raise_conn(exc_fn):
    return _FakeConn()


# ── 5. state stale ──────────────────────────────────────────────────────────
class TestStateStale:
    def test_old_state_file_is_low_not_critical(self, tmp_path, monkeypatch):
        """State é projection-only — staleness é LOW, não CRITICAL."""
        state = tmp_path / "state.json"
        state.write_text("{}")
        # backdate mtime p/ 60min atrás
        old = time.time() - 60 * 60
        import os
        os.utime(state, (old, old))
        monkeypatch.setattr(sh, "STATE_PATH", state)

        issue = sh._check_state_fresh()
        assert issue is not None
        assert issue.type == "state_stale"
        assert issue.severity == sh.SEV_LOW   # NÃO é high/critical
        assert "projection-only" in issue.detail.lower()

    def test_missing_state_is_ok(self, tmp_path, monkeypatch):
        """Projection-only: ausência do state não é issue."""
        monkeypatch.setattr(sh, "STATE_PATH", tmp_path / "inexistente.json")
        assert sh._check_state_fresh() is None


# ── 6. config lock órfão ────────────────────────────────────────────────────
class TestConfigLockStale:
    def test_old_lock_is_auto_healable(self, tmp_path, monkeypatch):
        lock = tmp_path / "vt_config.json.lock"
        lock.write_text("{}")
        old = time.time() - 600  # 10min
        import os
        os.utime(lock, (old, old))
        monkeypatch.setattr(sh, "CONFIG_LOCK_PATH", lock)

        issue = sh._check_config_lock_stale()
        assert issue is not None
        assert issue.type == "config_lock_stale"
        assert issue.auto_healable is True

    def test_fresh_lock_is_ok(self, tmp_path, monkeypatch):
        lock = tmp_path / "vt_config.json.lock"
        lock.write_text("{}")  # mtime = agora
        monkeypatch.setattr(sh, "CONFIG_LOCK_PATH", lock)
        assert sh._check_config_lock_stale() is None

    def test_no_lock_is_ok(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sh, "CONFIG_LOCK_PATH", tmp_path / "nope.lock")
        assert sh._check_config_lock_stale() is None


# ── 7. auto_heal restart autotrader ─────────────────────────────────────────
class TestAutoHeal:
    def test_heal_autotrader_dead_calls_restart(self, tmp_path, monkeypatch):
        """auto_heal(autotrader_dead) tenta restart via start_autotrader.sh."""
        monkeypatch.setattr(sh, "START_AUTOTRADER_SH", tmp_path / "start.sh")
        (tmp_path / "start.sh").write_text("#!/bin/bash\necho ok\n")
        monkeypatch.setattr(sh, "AUTOTRADER_SCRIPT", tmp_path / "at.py")
        monkeypatch.setattr(sh, "LOG_PATH", tmp_path / "log.log")
        monkeypatch.setattr(sh, "_PROJECT", tmp_path)

        issue = HealthIssue("autotrader_dead", sh.SEV_CRITICAL, "morto",
                            auto_healable=True)
        with patch("subprocess.run", side_effect=_mock_run({
            "pgrep": _cp(stdout="99999\n"),   # depois do restart, vivo
        })), patch("subprocess.Popen") as popen, patch("time.sleep"):
            result = auto_heal(issue)

        assert result.success is True
        assert "start_autotrader.sh" in result.action or "pkill" in result.action

    def test_heal_unknown_issue_returns_alert_only(self):
        """Issue sem healer mapeado → alert_only (não auto-cura)."""
        issue = HealthIssue("db_locked", sh.SEV_HIGH, "locked",
                            auto_healable=False)
        result = auto_heal(issue)
        assert result.action == "alert_only"
        assert result.success is False

    def test_heal_config_lock_removes_file(self, tmp_path, monkeypatch):
        lock = tmp_path / "vt_config.json.lock"
        lock.write_text("{}")
        monkeypatch.setattr(sh, "CONFIG_LOCK_PATH", lock)
        issue = HealthIssue("config_lock_stale", sh.SEV_LOW, "órfão",
                            auto_healable=True)
        result = auto_heal(issue)
        assert result.success is True
        assert not lock.exists()  # lock removido


# ── 8. run_once notifica Telegram ───────────────────────────────────────────
class TestRunOnceTelegram:
    def test_unhealed_critical_triggers_telegram(self, monkeypatch):
        """CRITICAL sem auto-cura dispara Telegram."""
        issue = HealthIssue("imaginary_critical", sh.SEV_CRITICAL,
                            "sem healer", auto_healable=False)
        with patch("monitoring.vt_self_heal.health_check",
                   return_value=HealthReport(issues=[issue])), \
             patch("monitoring.vt_self_heal._notify_telegram") as notify:
            report = run_once(heal=True)
            notify.assert_called_once()
            msg = notify.call_args[0][0]
            assert "CRITICAL" in msg
            assert "imaginary_critical" in msg
        assert len(report.heal_results) == 0  # não curou (auto_healable=False)

    def test_health_only_mode_does_not_heal(self):
        """--health-check-only não aplica auto-cura."""
        issue = HealthIssue("autotrader_dead", sh.SEV_CRITICAL, "morto",
                            auto_healable=True)
        with patch("monitoring.vt_self_heal.health_check",
                   return_value=HealthReport(issues=[issue])), \
             patch("monitoring.vt_self_heal.auto_heal") as heal:
            report = run_once(heal=False)
            heal.assert_not_called()
        assert report.heal_results == []


# ── 9. HealthReport helpers ─────────────────────────────────────────────────
class TestReportHelpers:
    def test_healthy_true_when_no_high_critical(self):
        r = HealthReport(issues=[
            HealthIssue("low1", sh.SEV_LOW, "x"),
            HealthIssue("info1", sh.SEV_INFO, "y"),
        ])
        assert r.healthy is True
        assert r.critical_count == 0

    def test_healthy_false_when_critical(self):
        r = HealthReport(issues=[
            HealthIssue("crit", sh.SEV_CRITICAL, "boom"),
        ])
        assert r.healthy is False
        assert r.critical_count == 1

    def test_healthy_false_when_high(self):
        r = HealthReport(issues=[
            HealthIssue("h", sh.SEV_HIGH, "x"),
        ])
        assert r.healthy is False
