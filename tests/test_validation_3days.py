"""
test_validation_3days.py
=========================
Suite de testes para o script scripts/vt_validate_3days.py.

ESTES TESTES (Wave 12 validation, 2026-07-01):
- test_script_exists_and_runs             — script existe e roda sem erro
- test_validation_3days_mock_mode_passes  — modo mock retorna exit 0
- test_validation_3days_generates_report  — relatorio .md eh criado
- test_validation_3days_report_schema     — relatorio contem tabela esperada
- test_validation_3days_drift_detection   — drift artificial gera exit 1

Por que este teste importa (regressao historica):
- O script foi implementado em Wave 12 (deleg_7b946ce5) mas nunca ganhou
  tests nem commit. Alem disso, o mock tinha bug: truth layer consultava
  MT5 real via Wine (porque vt_truth importa de mt5.mt5_orchestrator no
  import-time), causando PnL MT5 R$ 0.00 e drift falso-positivo.
- O fix deste teste valida que os bindings mock do truth estao instalados
  E que o cleanup end-of-day evita orphans/ghosts acumulados.

NAO MEXE EM:
- core/vt_truth.py (autoritativo)
- core/vt_autotrader.py
- monitoring/vt_trade_watchdog.py
- mt5_orchestrator.py
- core/vt_config_loader.py
- AGI / optimization/*
"""
import os
import re
import subprocess
import sys
import unittest
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
DATA_DIR = PROJECT_ROOT / "data"

# Garante que scripts/ esta no path para `from scripts.vt_validate_3days ...`
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Limita accel e trades para a suite nao demorar >5s por teste.
# (default 1.0s/dia * 3 dias ~ 3s, mais overhead de pytest ~ 5s total)
_QUICK_TRADES = (2, 4)  # min,max por dia
_QUICK_ACCEL = 0.2      # 0.2s/dia * 3 dias ~ 0.6s


def _run_validation(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """Roda scripts/vt_validate_3days.py com args. Retorna CompletedProcess."""
    cmd = [
        sys.executable,
        str(SCRIPTS_DIR / "vt_validate_3days.py"),
        "--mode=mock",
        f"--trades-min={_QUICK_TRADES[0]}",
        f"--trades-max={_QUICK_TRADES[1]}",
        f"--accel-sec-per-day={_QUICK_ACCEL}",
        *args,
    ]
    return subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
    )


class TestValidation3Days(unittest.TestCase):
    """Suite para scripts/vt_validate_3days.py."""

    # ----------------------------------------------------------
    # 1. Script existe e roda
    # ----------------------------------------------------------
    def test_script_exists_and_runs(self):
        """O script deve existir em scripts/vt_validate_3days.py e ser executavel."""
        script_path = SCRIPTS_DIR / "vt_validate_3days.py"
        self.assertTrue(
            script_path.exists(),
            f"Script nao encontrado em {script_path}",
        )
        # Conteudo minimo esperado (header + funcao main)
        content = script_path.read_text(encoding="utf-8")
        self.assertIn("def run_validation", content)
        self.assertIn("def main", content)
        self.assertIn("DRIFT_THRESHOLD_REAIS", content)
        # --help nao exige MT5 nem DB
        result = subprocess.run(
            [sys.executable, str(script_path), "--help"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
        )
        self.assertEqual(result.returncode, 0, f"--help falhou: {result.stderr}")
        self.assertIn("validacao 3 dias", result.stdout.lower())

    # ----------------------------------------------------------
    # 2. Modo mock termina com exit 0 (sem drift injetado)
    # ----------------------------------------------------------
    def test_validation_3days_mock_mode_passes(self):
        """Em modo mock sem injecao, exit code deve ser 0 (PASS)."""
        result = _run_validation()
        self.assertEqual(
            result.returncode, 0,
            f"Esperado exit 0, recebeu {result.returncode}.\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
        # OVERALL: PASS deve aparecer no stdout
        self.assertIn("OVERALL: PASS", result.stdout)

    # ----------------------------------------------------------
    # 3. Gera relatorio .md em data/
    # ----------------------------------------------------------
    def test_validation_3days_generates_report_file(self):
        """O script deve criar data/validation_3days_YYYYMMDD_HHMMSS.md."""
        before = set(DATA_DIR.glob("validation_3days_*.md"))
        result = _run_validation()
        self.assertEqual(result.returncode, 0, f"Falhou: {result.stderr}")
        after = set(DATA_DIR.glob("validation_3days_*.md"))
        new_files = after - before
        self.assertTrue(
            len(new_files) >= 1,
            f"Nenhum relatorio novo em {DATA_DIR}. "
            f"Antes: {len(before)} arquivos, depois: {len(after)}.",
        )
        # O relatorio deve ter conteudo nao-trivial
        newest = max(new_files, key=lambda p: p.stat().st_mtime)
        self.assertGreater(newest.stat().st_size, 500, "Relatorio vazio demais")

    # ----------------------------------------------------------
    # 4. Schema do relatorio contem campos esperados
    # ----------------------------------------------------------
    def test_validation_3days_report_has_required_fields(self):
        """O relatorio .md deve conter tabela com colunas obrigatorias."""
        result = _run_validation()
        self.assertEqual(result.returncode, 0)
        # Acha o relatorio mais recente
        reports = sorted(
            DATA_DIR.glob("validation_3days_*.md"),
            key=lambda p: p.stat().st_mtime,
        )
        self.assertTrue(reports, "Sem relatorios em data/")
        report_text = reports[-1].read_text(encoding="utf-8")

        # Campos obrigatorios na tabela
        required_columns = [
            "Dia", "Data", "Trades", "Wins", "Losses",
            "PnL MT5", "PnL DB", "Drift", "Threshold",
            "Orphans", "Ghosts", "GHOST(PnL=0)", "Consistente", "Status",
        ]
        for col in required_columns:
            self.assertIn(
                col, report_text,
                f"Coluna '{col}' ausente no relatorio. "
                f"Verifique o header da tabela em write_markdown_report().",
            )

        # Deve ter 3 linhas de dados (1 por dia) — verifica via '| 1 |', '| 2 |', '| 3 |'
        self.assertRegex(report_text, r"\|\s*1\s*\|")
        self.assertRegex(report_text, r"\|\s*2\s*\|")
        self.assertRegex(report_text, r"\|\s*3\s*\|")

        # Deve terminar com PASS ou FAIL
        self.assertRegex(report_text, r"\*\*(PASS|FAIL)\*\*")

        # Total de trades e drift alerts no header
        self.assertIn("Total trades", report_text)
        self.assertIn("Drift alerts", report_text)

    # ----------------------------------------------------------
    # 5. Drift detection funciona (--inject-drift -> exit 1)
    # ----------------------------------------------------------
    def test_validation_3days_handles_drift_correctly(self):
        """Com --inject-drift, exit code deve ser 1 (FAIL) e drift > R$ 5."""
        result = _run_validation("--inject-drift")
        self.assertEqual(
            result.returncode, 1,
            f"Esperado exit 1 (FAIL), recebeu {result.returncode}.\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
        self.assertIn("OVERALL: FAIL", result.stdout)

        # Relatorio gerado deve mostrar drift > R$ 5.00 em algum dia.
        reports = sorted(
            DATA_DIR.glob("validation_3days_*.md"),
            key=lambda p: p.stat().st_mtime,
        )
        self.assertTrue(reports, "Sem relatorio gerado em --inject-drift")
        report_text = reports[-1].read_text(encoding="utf-8")

        # Algum dia deve ter drift > R$ 5 (formato "R$ +XX.XX" ou "R$ -XX.XX")
        # Extrai valores de "Drift | R$ 5.00 |" — aqui procuramos outra ocorrencia
        # com valor numerico grande. Match generico: "R$ [+-]\\d+\\.\\d{2}".
        drift_values = re.findall(
            r"\|\s*R\$\s*([+-]?\d+\.\d{2})\s*\|\s*R\$\s*5\.00\s*\|",
            report_text,
        )
        # Em modo --inject-drift, espera-se pelo menos 1 valor > 5.00 em absolute.
        high_drifts = [float(v) for v in drift_values if abs(float(v)) > 5.0]
        self.assertTrue(
            len(high_drifts) >= 1,
            f"Esperava drift > R$ 5 em --inject-drift; encontrei {drift_values}. "
            f"Relatorio:\n{report_text[:1500]}",
        )

    # ----------------------------------------------------------
    # Bonus: smoke test do run_validation() direto (sem subprocess)
    # ----------------------------------------------------------
    def test_run_validation_function_returns_session(self):
        """run_validation() deve retornar (ValidationSession, int)."""
        # Import lazy — so carrega o modulo quando o teste roda
        sys.path.insert(0, str(SCRIPTS_DIR))
        try:
            from vt_validate_3days import run_validation  # type: ignore
        except ImportError as e:
            self.skipTest(f"Nao foi possivel importar run_validation: {e}")

        session, exit_code = run_validation(
            mode="mock",
            accel_sec_per_day=_QUICK_ACCEL,
            trades_per_day=_QUICK_TRADES,
            verbose=False,
        )

        self.assertEqual(exit_code, 0, "Mock run deveria ser PASS")
        self.assertEqual(len(session.days), 3, "Devem existir 3 daily reports")
        self.assertTrue(session.passed)
        # Cada dia deve ter drift zero (mock sincronizado)
        for d in session.days:
            self.assertEqual(
                d.drift, type(d.drift)(0),
                f"Drift no dia {d.day_index} deveria ser 0, foi {d.drift}",
            )
            self.assertEqual(d.n_orphans, 0)
            self.assertEqual(d.n_ghosts, 0)


if __name__ == "__main__":
    unittest.main()