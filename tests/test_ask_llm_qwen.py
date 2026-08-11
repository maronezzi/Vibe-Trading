"""
Testes do ask_llm com a cadeia LLM unificada (Wave 880.F, Bruno 09/08).

Cadeia nova (mesma de core/vt_order_validator_v2.py), em TODOS os cron scripts
(hermes + openclaw):
  1º zenmux/deepseek/deepseek-v4-flash-free
  2º zenmux/deepseek/deepseek-v4-flash
  3º alibaba-token-plan/deepseek-v4-flash-0731
  4º alibaba-token-plan/qwen3.8-max (último recurso)

deepseek-v4-pro foi REMOVIDO. Todos os providers agora são explícitos
(provider+model), cada um com timeout 180 (AGI gera código noturno).

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

# Cadeia esperada (mesma do validator_v2)
_EXPECTED_CHAIN = [
    ("zenmux", "deepseek/deepseek-v4-flash-free"),
    ("zenmux", "deepseek/deepseek-v4-flash"),
    ("alibaba-token-plan", "deepseek-v4-flash-0731"),
    ("alibaba-token-plan", "qwen3.8-max"),
]


class _FakeResult:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.fixture(autouse=True)
def _mock_hermes(monkeypatch):
    """Garante que find_hermes() retorna um path (simula hermes presente)."""
    monkeypatch.setattr(vt_hermes_helper, "find_hermes", lambda: "/fake/hermes")


class TestAskLlmChain:
    def test_cadeia_tem_4_modelos_ordem_correta(self):
        """_ASK_LLM_PROVIDERS deve ter exatamente os 4 modelos da cadeia nova."""
        got = [(p["provider"], p["model"]) for p in vt_hermes_helper._ASK_LLM_PROVIDERS]
        assert got == _EXPECTED_CHAIN, f"cadeia ≠ definida pelo Bruno. Got: {got}"

    def test_deepseek_v4_pro_removido(self):
        """deepseek-v4-pro NÃO pode estar na cadeia (removido 09/08)."""
        models = [p["model"] for p in vt_hermes_helper._ASK_LLM_PROVIDERS]
        assert "deepseek-v4-pro" not in models, "deepseek-v4-pro deveria estar removido"

    def test_todos_providers_explicitos(self):
        """Nenhum provider com model=None (todos usam -m/--provider explícito)."""
        for p in vt_hermes_helper._ASK_LLM_PROVIDERS:
            assert p["model"] is not None, f"model None não permitido: {p}"
            assert p["provider"] is not None, f"provider None não permitido: {p}"

    def test_primario_zenmux_free_passa_flags(self, monkeypatch):
        """O primário (zenmux-free) chama hermes COM -m e --provider."""
        calls = []

        def _fake_run(args, **kwargs):
            calls.append(args)
            return _FakeResult(stdout="code " + "x"*60)

        monkeypatch.setattr(subprocess, "run", _fake_run)
        resp = vt_hermes_helper.ask_llm("gere soma", timeout=180)

        assert resp is not None and resp.startswith("code ")
        assert len(calls) == 1, "primário respondeu → não deve tentar fallback"
        primario_args = calls[0]
        assert "-m" in primario_args
        assert "--provider" in primario_args
        assert primario_args[primario_args.index("-m") + 1] == "deepseek/deepseek-v4-flash-free"
        assert primario_args[primario_args.index("--provider") + 1] == "zenmux"

    def test_timeout_180_para_geracao_codigo(self):
        """Cada provider deve ter timeout 180 (AGI gera código noturno)."""
        for p in vt_hermes_helper._ASK_LLM_PROVIDERS:
            assert p["timeout"] == 180, f"{p['model']} timeout ≠ 180: {p['timeout']}"

    def test_fallback_para_segundo_provider(self, monkeypatch):
        """Se o primário (zenmux-free) falha, cai pro zenmux-flash."""
        seen = []

        def _fake_run(args, **kwargs):
            if "-m" in args and args[args.index("-m") + 1] == "deepseek/deepseek-v4-flash-free":
                seen.append("primario")
                return _FakeResult(stdout="err", stderr="err primary", returncode=1)
            seen.append("fallback")
            return _FakeResult(stdout="resposta flash " + "x"*60)

        monkeypatch.setattr(subprocess, "run", _fake_run)
        resp = vt_hermes_helper.ask_llm("teste", timeout=180)

        assert resp is not None and resp.startswith("resposta flash ")
        assert seen == ["primario", "fallback"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))