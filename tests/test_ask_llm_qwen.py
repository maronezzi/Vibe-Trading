"""
Testes do ask_llm com qwen3.8 primário (Wave qwen-primário, 30/07).

Cobre:
- O PRIMÁRIO chama hermes SEM -m/--provider (usa o default qwen3.8 do hermes).
- Se o primário (default) responde, retorna direto (não tenta fallbacks).
- Se o primário timeout, cai pro MiniMax-M3 com timeout 25 (não mais 10).
- Se todos falham, retorna None.

Mocka subprocess.run e find_hermes — não depende do hermes real/MT5.
"""
import subprocess
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core import vt_hermes_helper  # noqa: E402


class _FakeResult:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.fixture(autouse=True)
def _mock_hermes(monkeypatch):
    """Garante que find_hermes() retorna um path (simula hermes presente)."""
    monkeypatch.setattr(vt_hermes_helper, "find_hermes", lambda: "/fake/hermes")


class TestAskLlmQwenDefault:
    def test_primario_nao_passa_flags_de_modelo(self, monkeypatch):
        """O primário (qwen3.8 default) chama hermes SEM -m/--provider."""
        calls = []

        def _fake_run(args, **kwargs):
            calls.append(args)
            return _FakeResult(stdout="def soma(a, b): return a + b")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        resp = vt_hermes_helper.ask_llm("gere soma", timeout=60)

        assert resp == "def soma(a, b): return a + b"
        assert len(calls) == 1, "primário respondeu → não deve tentar fallback"
        primario_args = calls[0]
        assert "-m" not in primario_args, "primário NÃO deve passar -m (usa default)"
        assert "--provider" not in primario_args, "primário NÃO deve passar --provider"

    def test_primario_responde_nao_tenta_fallbacks(self, monkeypatch):
        """Se o default do hermes responde, MiniMax/mimo não são chamados."""
        call_count = [0]

        def _fake_run(args, **kwargs):
            call_count[0] += 1
            return _FakeResult(stdout="resposta do qwen")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        vt_hermes_helper.ask_llm("teste", timeout=60)
        assert call_count[0] == 1, "só o primário deveria rodar"

    def test_fallback_minimax_timeout_e_25_nao_10(self, monkeypatch):
        """Se o primário (qwen) timeout, cai pro MiniMax-M3 com timeout 25."""
        seen_timeouts = []

        def _fake_run(args, **kwargs):
            # primário (sem -m) → timeout; MiniMax → responde.
            if "-m" not in args:
                seen_timeouts.append(("primario", kwargs.get("timeout")))
                raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 0))
            seen_timeouts.append((args[args.index("-m") + 1] if "-m" in args else "?",
                                  kwargs.get("timeout")))
            return _FakeResult(stdout="resposta minimax")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        resp = vt_hermes_helper.ask_llm("teste", timeout=120)

        assert resp == "resposta minimax"
        assert len(seen_timeouts) == 2, "primário + 1 fallback"
        # primário timeout 60, MiniMax timeout 25 (não 10).
        assert seen_timeouts[0] == ("primario", 60)
        assert seen_timeouts[1] == ("MiniMax-M3", 25), \
            f"MiniMax deve ter timeout 25 (não 10), got {seen_timeouts[1]}"

    def test_todos_falham_retorna_none(self, monkeypatch):
        """Se primário + todos fallbacks falham/timeout, retorna None."""
        def _fake_run(args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 0))

        monkeypatch.setattr(subprocess, "run", _fake_run)
        assert vt_hermes_helper.ask_llm("teste", timeout=120) is None

    def test_cadeia_tem_qwen_como_primeiro(self):
        """_ASK_LLM_PROVIDERS[0] deve ser o default do hermes (model=None)."""
        primario = vt_hermes_helper._ASK_LLM_PROVIDERS[0]
        assert primario["model"] is None, "primário deve usar default do hermes (model=None)"
        assert primario["provider"] is None
        assert primario["timeout"] >= 30, "primário precisa de budget p/ gerar código"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
