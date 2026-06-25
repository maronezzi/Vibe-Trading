"""
test_gitignore.py
=================
TDD: garante que paths runtime/efêmeros do projeto estão no
.gitignore e que `git status` NÃO os reporta como untracked.

Achado 2026-06-25: `.hermes/` aparece como untracked file — sujaria
o próximo commit com planos locais do Hermes Agent. Tem que estar
no .gitignore.

Estado documentado (2026-06-25):
- vt_config.json e vt_trades.db estão RASTREADOS no git (decisão
  consciente: histórico de evolução do AGI). NÃO são cobertos
  aqui. Para "limpar" do status, usar `git rm --cached` em
  branch separado.
- .hermes/ NÃO está no .gitignore — precisa entrar.
"""
import os
import subprocess
import unittest

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"


def _is_ignored(path: str) -> bool:
    """Retorna True se `git check-ignore` considera o path ignorado."""
    result = subprocess.run(
        ["git", "check-ignore", path],
        capture_output=True, text=True, timeout=10,
        cwd=PROJECT_ROOT,
    )
    # exit 0 = ignored, exit 1 = not ignored, exit 128 = error
    return result.returncode == 0


class TestGitignore(unittest.TestCase):
    """Garante que paths efêmeros não aparecem como untracked no git status."""

    def test_hermes_dir_is_ignored(self):
        """.hermes/ (working dir do Hermes Agent) deve estar no .gitignore."""
        self.assertTrue(
            _is_ignored(".hermes"),
            f".hermes/ NÃO está no .gitignore. Vai aparecer como untracked "
            f"no próximo `git add -A` e poluir o commit. Adicione '.hermes/' "
            f"a .gitignore.",
        )

    def test_hermes_dir_specifically_named(self):
        """.gitignore deve ter a entrada '.hermes/' explicitamente (não só '.*')."""
        with open(os.path.join(PROJECT_ROOT, ".gitignore")) as f:
            content = f.read()
        self.assertIn(
            ".hermes", content,
            f".gitignore não contém '.hermes'. Provavelmente foi ignorado "
            f"por outro pattern (ex: '.*') mas queremos explícito para "
            f"documentação. Adicione uma linha '# Hermes Agent working dir\\n.hermes/'.",
        )

    def test_ruff_cache_ignored(self):
        """.ruff_cache/ é cache local do linter — não vai pro repo."""
        self.assertTrue(
            _is_ignored(".ruff_cache"),
            f".ruff_cache/ deveria estar no .gitignore",
        )

    def test_pytest_cache_ignored(self):
        """.pytest_cache/ é cache local do test runner — não vai pro repo."""
        self.assertTrue(
            _is_ignored(".pytest_cache"),
            f".pytest_cache/ deveria estar no .gitignore",
        )

    def test_no_untracked_local_artifacts(self):
        """Smoke: 'git status --porcelain' não deve listar paths efêmeros conhecidos."""
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
            cwd=PROJECT_ROOT,
        )
        lines = [l for l in result.stdout.splitlines() if l.startswith("??")]
        bad = [l for l in lines if any(
            l.endswith(p) or p in l
            for p in [".hermes/", ".ruff_cache/", ".pytest_cache/", "__pycache__/"]
        )]
        if bad:
            self.fail(
                "git status lista paths efêmeros como untracked:\n"
                + "\n".join(f"  {b}" for b in bad)
                + "\n\nAdicione ao .gitignore."
            )


if __name__ == "__main__":
    unittest.main()
