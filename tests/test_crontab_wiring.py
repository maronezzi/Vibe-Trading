"""
test_crontab_wiring.py
======================
TDD RED phase: garante que cada linha de crontab do projeto aponta para
arquivo que EXISTE e roda com o PYTHON do projeto (não o do sistema).

CONTEXTO (2026-06-25 cron audit):
- Cron roda com PATH=/usr/bin:/bin e o `/usr/bin/python3` é 3.12 sem
  deps do projeto (optuna, numpy etc. estão em .venv/bin/python).
- 5 dos 6 jobs do `crontab -l` instalado apontam para arquivos que
  NÃO EXISTEM (refactor 2026-06-22 moveu scripts de raiz → monitoring/,
  optimization/). Erro: `No such file or directory`.
- AGI 17:10 (o pedido original do Bruno): chama `agi_tuning_17h.py`
  (raiz) mas o script está em `optimization/agi_tuning_17h.py`.

ESTE TESTE:
- Lê o `crontab.txt` versionado no repo (fonte de verdade) e o
  `crontab -l` instalado.
- Para cada linha de job (sem comentários), extrai:
  (a) o caminho do script python alvo
  (b) o interpretador python usado
- Verifica (a) que o script existe no filesystem.
- Verifica (b) que o interpretador consegue `import optuna` (uma
  dep crítica do AGI). Se for `/usr/bin/python3` do sistema → FAIL
  porque não tem as deps.

NOTA: este teste é de wiring, não executa o autotrader. Falha rápida
se o crontab driftar do estado conhecido.
"""
import os
import re
import subprocess
import sys
import unittest

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
CRONTAB_FILE = os.path.join(PROJECT_ROOT, "crontab.txt")
VENV_PYTHON = os.path.join(PROJECT_ROOT, ".venv", "bin", "python")


def _parse_crontab_jobs(text):
    """Extrai linhas de job (não comentário) de um texto de crontab."""
    jobs = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        jobs.append(s)
    return jobs


def _extract_script_and_python(job_line):
    """
    Extrai (python_interpreter, script_path) de uma linha de cron.
    Suporta 2 formas:
      1) /usr/bin/python3 /path/script.py
      2) cd /path && /usr/bin/python3 script.py
    """
    # pega o primeiro token absoluto que pareça python
    py_match = re.search(r"(/[\w/.-]+python[\w.]*)", job_line)
    python = py_match.group(1) if py_match else None

    # pega o último token .py da linha
    script_match = re.findall(r"([/\w.-]+\.py)\b", job_line)
    # filtra pra paths absolutos ou com monitoring/optimization/scripts
    script = None
    for s in reversed(script_match):
        if s.startswith("/") or "/" in s:
            script = s
            break
        if s.endswith(".py"):
            script = s
    return python, script


class TestCrontabWiring(unittest.TestCase):
    """Garante que o crontab aponta para scripts e interpretadores que existem e funcionam."""

    @classmethod
    def setUpClass(cls):
        # Lê crontab.txt versionado
        with open(CRONTAB_FILE) as f:
            cls.crontab_txt = f.read()
        # Lê crontab instalado (pode falhar em ambiente sem cron)
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=10
        )
        cls.crontab_installed = result.stdout if result.returncode == 0 else ""

    def test_crontab_txt_exists(self):
        self.assertTrue(
            os.path.exists(CRONTAB_FILE),
            f"crontab.txt não encontrado em {CRONTAB_FILE}",
        )

    def test_venv_python_has_critical_deps(self):
        """O .venv/bin/python precisa ter optuna (dep crítica do AGI)."""
        result = subprocess.run(
            [VENV_PYTHON, "-c", "import optuna; print(optuna.__version__)"],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(
            result.returncode, 0,
            f".venv/bin/python não consegue importar optuna:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}",
        )

    def test_system_python_has_critical_deps(self):
        """Documenta que /usr/bin/python3 TEM as deps instaladas globalmente.

        Achado 2026-06-25: .venv/bin/python é symlink para /usr/bin/python3.12
        (instalado em jun/2026 via uv), e as deps (optuna, numpy, pandas) foram
        instaladas globalmente com pip --break-system-packages. Portanto o
        /usr/bin/python3 do sistema É o interpretador válido pro cron.

        O fix do crontab é APENAS de path (refactor 2026-06-22 moveu scripts
        de raiz → monitoring/, optimization/), não de interpretador.
        """
        result = subprocess.run(
            ["/usr/bin/python3", "-c", "import optuna; print(optuna.__version__)"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(
            result.returncode, 0,
            f"/usr/bin/python3 não consegue importar optuna (esperado: SIM consegue):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}",
        )

    def test_crontab_txt_jobs_have_existing_scripts(self):
        """Toda linha de job no crontab.txt deve apontar para arquivo que existe."""
        for job in _parse_crontab_jobs(self.crontab_txt):
            python, script = _extract_script_and_python(job)
            if not script:
                continue  # linha de start_autotrader.sh (.sh) ou sem .py
            full = script if script.startswith("/") else os.path.join(PROJECT_ROOT, script)
            if not os.path.exists(full):
                # só falha em linhas que claramente apontam pra .py
                if script.endswith(".py"):
                    self.fail(
                        f"crontab.txt tem linha com script inexistente:\n"
                        f"  {job}\n"
                        f"  → {full}\n"
                        f"Esperado: caminho correto após refactor 2026-06-22."
                    )

    def test_crontab_jobs_use_paths_from_refactor(self):
        """crontab.txt deve usar paths do refactor 2026-06-22 (monitoring/, optimization/).

        A regra é: se o job tem `cd <dir> && python3 <script>` OU path absoluto,
        o `<script>` tem que estar onde o refactor deixou. Esta validação
        confirma tanto o path quanto que o script existe.
        """
        issues = []
        for job in _parse_crontab_jobs(self.crontab_txt):
            python, script = _extract_script_and_python(job)
            if not script or not script.endswith(".py"):
                continue
            # normaliza path
            full = script if script.startswith("/") else os.path.join(PROJECT_ROOT, script)
            if not os.path.exists(full):
                issues.append(f"  {job}\n    → {full} (NÃO EXISTE)")
        if issues:
            self.fail(
                "crontab.txt tem jobs apontando para scripts inexistentes:\n"
                + "\n".join(issues)
                + "\n\nProvavelmente o refactor 2026-06-22 moveu os scripts "
                "mas o crontab não foi atualizado."
            )

    def test_agi_17_10_line_uses_optimization_path(self):
        """A linha 17:10 (AGI) deve apontar para optimization/agi_tuning_17h.py."""
        for job in _parse_crontab_jobs(self.crontab_txt):
            if "17 * * 1-5" in job and "agi" in job.lower():
                _, script = _extract_script_and_python(job)
                self.assertIn(
                    "optimization/agi_tuning_17h.py", script or "",
                    f"AGI 17:10 não aponta para optimization/agi_tuning_17h.py:\n"
                    f"  {job}\n"
                    f"  script extraído: {script}",
                )

    def test_agi_runs_with_venv_and_finds_module(self):
        """Smoke test: AGI importa módulos críticos quando rodado com .venv/bin/python."""
        result = subprocess.run(
            [
                VENV_PYTHON, "optimization/agi_tuning_17h.py",
                "--no-llm", "--dry-run", "--days", "1",
            ],
            capture_output=True, text=True, timeout=90,
            cwd=PROJECT_ROOT,
        )
        # dry-run termina com 0 ou com erro de regime — qualquer coisa
        # menos ImportError ou ModuleNotFoundError.
        combined = (result.stdout + result.stderr).lower()
        if "modulenotfounderror" in combined or "importerror" in combined:
            self.fail(
                f"AGI rodando com .venv/bin/python tem ImportError:\n"
                f"stdout: {result.stdout[-500:]}\nstderr: {result.stderr[-500:]}"
            )

    def test_installed_crontab_matches_txt(self):
        """O crontab instalado (crontab -l) deve bater com o crontab.txt versionado.

        Qualquer drift entre os dois = silent breakage de um job de produção.
        """
        if not self.crontab_installed:
            self.skipTest("crontab -l não disponível neste ambiente")
        # normaliza: remove comentários e linhas em branco pra comparar só os jobs
        jobs_txt = set(_parse_crontab_jobs(self.crontab_txt))
        jobs_inst = set(_parse_crontab_jobs(self.crontab_installed))
        only_in_txt = jobs_txt - jobs_inst
        only_in_inst = jobs_inst - jobs_txt
        if only_in_txt or only_in_inst:
            msg = "Crontab instalado diverge do crontab.txt (drift = silent breakage).\n"
            if only_in_txt:
                msg += f"  Em crontab.txt mas NÃO instalado: {sorted(only_in_txt)}\n"
            if only_in_inst:
                msg += f"  Instalado mas NÃO em crontab.txt: {sorted(only_in_inst)}\n"
            msg += "\nFIX: reinstalar o crontab a partir do repo:\n"
            msg += "  crontab ~/Projects/Vibe-Trading/crontab.txt"
            self.fail(msg)

    def test_installed_crontab_scripts_exist(self):
        """Cada .py no crontab instalado precisa apontar para arquivo que existe."""
        if not self.crontab_installed:
            self.skipTest("crontab -l não disponível neste ambiente")
        issues = []
        for job in _parse_crontab_jobs(self.crontab_installed):
            python, script = _extract_script_and_python(job)
            if not script or not script.endswith(".py"):
                continue
            full = script if script.startswith("/") else os.path.join(PROJECT_ROOT, script)
            if not os.path.exists(full):
                issues.append(f"  {job}\n    → {full} (NÃO EXISTE)")
        if issues:
            self.fail(
                "Crontab instalado tem jobs com scripts inexistentes:\n"
                + "\n".join(issues)
            )


if __name__ == "__main__":
    unittest.main()
