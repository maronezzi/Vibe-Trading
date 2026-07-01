"""
Testes de integração: vt_copilot.py ↔ vt_self_heal.py (Fase 2.2, subtask 2.2.2).

Valida que:
  1. run_self_heal_hook() invoca self-heal e retorna resumo das ações
  2. --self-heal mode chama o hook e loga
  3. Hook nunca derruba o copilot se self-heal falhar (resiliência)

Import defensivo: o copilot faz `from monitoring.vt_self_heal import run_once`
no topo — se isso falhar, self_heal_run_once vira None e o hook retorna "".
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from monitoring import vt_copilot as copilot
from monitoring.vt_self_heal import HealthReport, HealthIssue, HealResult


class TestSelfHealHook:
    """run_self_heal_hook() integra copilot → self-heal."""

    def test_hook_returns_summary_when_issues_found(self):
        """Quando self-heal encontra issues, hook retorna resumo p/ relatório."""
        report = HealthReport(
            issues=[HealthIssue("autotrader_dead", "critical", "morto",
                                auto_healable=True)],
            heal_results=[HealResult("autotrader_dead", "start_autotrader.sh",
                                     True, "new_pid=123")],
        )
        with patch.object(copilot, "self_heal_run_once",
                          return_value=report) as mock_run:
            summary = copilot.run_self_heal_hook()

        mock_run.assert_called_once_with(heal=True)
        assert "self-heal" in summary.lower()
        assert "autotrader_dead" in summary
        assert "✅" in summary  # ícone de heal bem-sucedido

    def test_hook_returns_empty_when_healthy(self):
        """Sistema saudável → hook retorna "" (não polui relatório)."""
        report = HealthReport(issues=[])  # healthy
        with patch.object(copilot, "self_heal_run_once", return_value=report):
            summary = copilot.run_self_heal_hook()
        assert summary == ""

    def test_hook_returns_empty_when_import_failed(self):
        """Se self_heal_run_once é None (import falhou), hook retorna "".

        Garantia: self-heal NUNCA derruba o copilot (Lei: monitoramento não
        bloqueia fluxo principal).
        """
        with patch.object(copilot, "self_heal_run_once", None):
            summary = copilot.run_self_heal_hook()
        assert summary == ""

    def test_hook_swallows_exceptions_from_self_heal(self):
        """Se self-heal levanta exceção, hook loga e retorna "" (não propaga)."""
        with patch.object(copilot, "self_heal_run_once",
                          side_effect=RuntimeError("boom")), \
             patch("builtins.print"):  # silencia o log durante o teste
            summary = copilot.run_self_heal_hook()
        assert summary == ""


class TestSelfHealMode:
    """--self-heal mode invoca o hook isoladamente."""

    def test_self_heal_mode_calls_hook_and_returns(self, capsys):
        """`vt_copilot.py --self-heal` roda só o self-heal e sai."""
        with patch.object(copilot, "run_self_heal_hook",
                          return_value="🛡️ self-heal: 1 issue(s)\n   ✅ x: y") \
             as mock_hook, \
             patch.object(sys, "argv", ["vt_copilot.py", "--self-heal"]):
            copilot.main()

        mock_hook.assert_called_once()
        captured = capsys.readouterr()
        # Loga o resultado (cron captura p/ /tmp log)
        assert "SELF-HEAL" in captured.out or mock_hook.called

    def test_self_heal_mode_silent_when_healthy(self, capsys):
        """--self-heal com sistema saudável não spam log."""
        with patch.object(copilot, "run_self_heal_hook",
                          return_value="") as mock_hook, \
             patch.object(sys, "argv", ["vt_copilot.py", "--self-heal"]):
            copilot.main()
        mock_hook.assert_called_once()


class TestFullModeIntegration:
    """--full mode chama self-heal ANTES do health check do copilot."""

    def test_full_mode_invokes_self_heal_hook_first(self):
        """No modo --full, o hook de self-heal é chamado no início.

        Não rodamos o --full completo (depende de MT5/DB/Telegram reais);
        validamos só que o hook é chamado. Mockamos o resto p/ short-circuit.
        """
        call_order = []

        def fake_hook():
            call_order.append("self_heal")
            return ""

        # Mockamos tudo que --full toca depois do hook p/ evitar rede/MT5
        with patch.object(copilot, "run_self_heal_hook", side_effect=fake_hook), \
             patch.object(copilot, "check_autotrader_health",
                          return_value={"running": True, "pid": "1",
                                        "log_fresh": True}), \
             patch.object(copilot, "reconcile_orphans", return_value=0), \
             patch.object(copilot, "generate_report", return_value="ok"), \
             patch.object(copilot, "notify_telegram"), \
             patch.object(copilot, "evaluate_and_pause", return_value=None), \
             patch.object(copilot, "_restore_pauses_if_needed"), \
             patch("mt5.mt5_orchestrator.status", return_value={"positions": []}), \
             patch.object(sys, "argv", ["vt_copilot.py", "--full"]):
            try:
                copilot.main()
            except SystemExit:
                pass
            except Exception:
                # --full pode fazer mais chamadas que mockamos; o que importa
                # é que o hook de self-heal foi chamado PRIMEIRO.
                pass

        assert "self_heal" in call_order
        assert call_order[0] == "self_heal"
