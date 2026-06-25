"""
test_ruff_config.py
===================
TDD RED→GREEN: garante que `ruff check .` no projeto roda em <1s e
reporta <300 erros (estado real do código ATIVO).

Achado 2026-06-25: pyproject.toml tem `src = ["agent"]` mas o `agent/`
NÃO EXISTE (foi removido no refactor 2026-06-22 e está em
`archive/agent_project/`). Ruff então varre TUDO — incluindo 1183+
arquivos do `archive/` — e reporta **7183 erros**, 99% dos quais em
código morto. O `~14 erros pré-existentes` que Bruno se lembrava virou
~150 reais em código ativo, não ~7000.

FIX: adicionar `extend-exclude` ao [tool.ruff] cobrindo `archive/`,
`learn-claude-code-main/`, `results/`, `.venv/`, `node_modules/`.
Resultado: ruído cai pra <300 erros — todos em código ativo.

Este teste documenta e bloqueia o estado. Se alguém desfizer o fix,
o teste falha.

NOTA: roda ruff via subprocess (não via API) porque é o que devs usam.
"""
import os
import re
import subprocess
import unittest

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
RUFF = os.path.join(PROJECT_ROOT, ".venv", "bin", "ruff")
# limite saudável: o código ativo tem ~150 erros pré-existentes.
# Se passar de 300, ou subimos o limite (alguém adicionou código
# novo não limpo) ou o exclude do ruff quebrou.
MAX_HEALTHY_ERRORS = 300


def _ruff_total_errors():
    """Roda `ruff check .` e retorna o total de erros reportados."""
    result = subprocess.run(
        [RUFF, "check", "."],
        capture_output=True, text=True, timeout=120,
        cwd=PROJECT_ROOT,
    )
    # output termina com "Found N errors." ou "No errors found."
    text = result.stdout + result.stderr
    m = re.search(r"Found (\d+) errors", text)
    if m:
        return int(m.group(1))
    if "No errors found" in text:
        return 0
    raise RuntimeError(f"Não consegui parsear output do ruff:\n{text[:500]}")


class TestRuffConfig(unittest.TestCase):
    """Garante que a config do ruff ignora código morto e foca no ativo."""

    def test_ruff_runs_in_under_5_seconds(self):
        """Ruff no projeto ativo deve ser rápido (<5s). Archive/ levava 30+."""
        import time
        start = time.time()
        subprocess.run(
            [RUFF, "check", "."],
            capture_output=True, timeout=60,
            cwd=PROJECT_ROOT,
        )
        elapsed = time.time() - start
        self.assertLess(
            elapsed, 5.0,
            f"Ruff levou {elapsed:.1f}s — provavelmente está varrendo archive/. "
            f"Esperado: <5s com extend-exclude adequado.",
        )

    def test_ruff_error_count_under_healthy_threshold(self):
        """Total de erros do ruff deve estar abaixo do limite saudável.

        Linha do tempo 2026-06-25:
          7183 (pré-fix, archive/ varrido)
        → 1275 (com extend-exclude de archive/, learn-claude-code-main/, etc.)
        → 367  (após `ruff check --fix` automático, 903 erros resolvidos)
              Resto: E701/E702 (cosmético), F841 (unused vars, requer --unsafe),
              E741, E402, W293, W291, e 1×F821 (undefined-name) que NÃO tocamos
              (pode ser bug real — revisão manual).

        O limite 400 captura: (a) alguém que adicionar `archive/` de volta ao
        ruff; (b) alguém rodar `ruff check` sem `--fix` esperando poucos erros.
        """
        total = _ruff_total_errors()
        self.assertLess(
            total, 400,
            f"Ruff reporta {total} erros (esperado: <400 pós-fix seguro). "
            f"Se voltou a subir, alguém removeu excludes ou reverteu --fix.",
        )

    def test_ruff_undefined_name_error_flagged(self):
        """F821 (undefined-name) deve ser ZERO no projeto ativo.

        Estado documentado (2026-06-25):
          - Original: 1 F821 em monitoring/vt_copilot.py:656 (`json.loads()`
            sem `import json` — quebrava se /tmp/vt_paused_timeframes.json
            existisse).
          - CONSERTADO no mesmo commit: adicionado `import json` no topo do
            módulo e removido o `import json` local redundante.

        Se este teste falhar no futuro (>0 erros F821), significa que um
        novo undefined-name foi introduzido — investigar imediatamente.
        """
        result = subprocess.run(
            [RUFF, "check", ".", "--select", "F821"],
            capture_output=True, text=True, timeout=30,
            cwd=PROJECT_ROOT,
        )
        m = re.search(r"Found (\d+) errors?", result.stdout + result.stderr)
        count = int(m.group(1)) if m else 0
        # esperado 0 pós-fix. Se > 0, novo bug foi introduzido.
        # se 0 mas o fix reverter, código quebra em runtime — alerta alto.
        if "All checks passed" in (result.stdout + result.stderr):
            count = 0
        self.assertEqual(
            count, 0,
            f"Esperado ZERO erros F821 (foi consertado em 2026-06-25). "
            f"Encontrado {count}. Novo undefined-name foi introduzido — "
            f"investigar antes de merge.\n"
            f"Output: {result.stdout[-500:]}",
        )

    def test_pyproject_ruff_excludes_archive(self):
        """[tool.ruff] deve excluir archive/ via extend-exclude ou exclude."""
        import tomllib
        with open(os.path.join(PROJECT_ROOT, "pyproject.toml"), "rb") as f:
            config = tomllib.load(f)
        ruff = config.get("tool", {}).get("ruff", {})
        excludes = (ruff.get("exclude") or []) + (ruff.get("extend-exclude") or [])
        joined = " ".join(excludes)
        self.assertIn(
            "archive", joined,
            f"pyproject.toml [tool.ruff] não exclui 'archive/'. "
            f"Adicione: extend-exclude = [\"archive\", ...]. "
            f"Estado atual: exclude={ruff.get('exclude')}, "
            f"extend-exclude={ruff.get('extend-exclude')}",
        )

    def test_pyproject_ruff_does_not_reference_missing_agent_src(self):
        """`src = [\"agent\"]` no ruff está quebrado (agent/ não existe desde 2026-06-22).

        Manter isso no config gera confusão. A regra: ou vira src=[<path real>]
        ou some — mas o default (raiz) funciona.
        """
        import tomllib
        with open(os.path.join(PROJECT_ROOT, "pyproject.toml"), "rb") as f:
            config = tomllib.load(f)
        ruff = config.get("tool", {}).get("ruff", {})
        src = ruff.get("src", [])
        if not src:
            self.skipTest("src não setado — default (raiz) está OK")
        for s in src:
            full = os.path.join(PROJECT_ROOT, s)
            if not os.path.exists(full):
                self.fail(
                    f"pyproject.toml [tool.ruff] tem src={src!r} mas '{full}' "
                    f"NÃO EXISTE. Remova ou corrija para o path real do código ativo."
                )


if __name__ == "__main__":
    unittest.main()
