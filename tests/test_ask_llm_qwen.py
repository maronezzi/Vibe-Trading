"""
Testes do ask_llm com provider único yolo (Wave 892, Bruno 08/09).

Cadeia nova: APENAS yolo/qwen3.8-27b — o mesmo provider default do hermes no
VPS (~/.hermes/config.yaml). A cadeia anterior (zenmux free/flash + alibaba
deepseek/qwen) foi REMOVIDA: morta desde 03/09 (zenmux 404 invalid_model /
402 no_credit; alibaba 403 AccessDenied.Unpurchased — 161 falhas seguidas).

Transporte é HTTP direto (yolo está em _PROVIDER_ENDPOINTS), com User-Agent
próprio (Cloudflare 1010 barra Python-urllib) e chat_template_kwargs para
desligar o thinking do qwen3.8-27b (senão o content chega vazio).

Mocka _ask_llm_http_direct e find_hermes — não depende de rede/hermes real.
"""
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core import vt_hermes_helper  # noqa: E402

# Cadeia esperada (Wave 892 — yolo only)
_EXPECTED_CHAIN = [("yolo", "qwen3.8-27b")]


@pytest.fixture(autouse=True)
def _mock_hermes_and_health(monkeypatch, tmp_path):
    """Hermes presente (path fake) + health file isolado em tmp_path."""
    monkeypatch.setattr(vt_hermes_helper, "find_hermes", lambda: "/fake/hermes")
    monkeypatch.setattr(vt_hermes_helper, "_LLM_HEALTH_PATH",
                        tmp_path / "vt_llm_health.json")


class TestAskLlmChain:
    def test_cadeia_tem_so_yolo(self):
        """_ASK_LLM_PROVIDERS deve ter exatamente yolo/qwen3.8-27b."""
        got = [(p["provider"], p["model"]) for p in vt_hermes_helper._ASK_LLM_PROVIDERS]
        assert got == _EXPECTED_CHAIN, f"cadeia ≠ definida pelo Bruno. Got: {got}"

    def test_providers_antigos_removidos(self):
        """zenmux/alibaba NÃO podem restar na cadeia nem nos endpoints."""
        provs = [p["provider"] for p in vt_hermes_helper._ASK_LLM_PROVIDERS]
        assert "zenmux" not in provs
        assert "alibaba-token-plan" not in provs
        for legacy in ("zenmux", "alibaba-token-plan"):
            assert legacy not in vt_hermes_helper._PROVIDER_ENDPOINTS, (
                f"{legacy} deveria ter sido removido (Wave 892)")

    def test_endpoint_yolo_igual_ao_hermes(self):
        """Endpoint/chave do yolo espelham o config.yaml do hermes."""
        url, key_env = vt_hermes_helper._PROVIDER_ENDPOINTS["yolo"]
        assert url == "https://yolo-auto.com/v1/chat/completions"
        assert key_env == "YOLO_AUTO_API_KEY"

    def test_todos_providers_explicitos(self):
        """Nenhum provider com model=None (todos explícitos)."""
        for p in vt_hermes_helper._ASK_LLM_PROVIDERS:
            assert p["model"] is not None, f"model None não permitido: {p}"
            assert p["provider"] is not None, f"provider None não permitido: {p}"

    def test_timeout_180_para_geracao_codigo(self):
        """Provider deve ter timeout 180 (AGI gera código noturno)."""
        for p in vt_hermes_helper._ASK_LLM_PROVIDERS:
            assert p["timeout"] == 180, f"{p['model']} timeout ≠ 180: {p['timeout']}"


class TestAskLlmHttpDirectPath:
    def test_primario_yolo_usa_http_sem_cli(self, monkeypatch):
        """yolo tem endpoint HTTP → NÃO passa pelo CLI do hermes."""
        calls_cli = []

        def _no_cli(args, **kwargs):
            calls_cli.append(args)
            raise AssertionError("CLI do hermes não deveria ser chamado (yolo é HTTP direto)")

        monkeypatch.setattr(subprocess, "run", _no_cli)
        monkeypatch.setattr(
            vt_hermes_helper, "_ask_llm_http_direct",
            lambda *a, **k: "resposta " + "x" * 60,
        )
        resp = vt_hermes_helper.ask_llm("gere soma", timeout=180)

        assert resp is not None and resp.startswith("resposta ")
        assert calls_cli == []

    def test_args_do_http_direct(self, monkeypatch):
        """HTTP direto recebe provider/model do chain e o system."""
        got = {}

        def _fake_direct(prompt, provider, model, timeout, system=None):
            got.update(prompt=prompt, provider=provider, model=model,
                       timeout=timeout, system=system)
            return "ok " + "x" * 60

        monkeypatch.setattr(vt_hermes_helper, "_ask_llm_http_direct", _fake_direct)
        resp = vt_hermes_helper.ask_llm("prompt teste", timeout=60, system="sys")

        assert resp is not None
        assert got["provider"] == "yolo"
        assert got["model"] == "qwen3.8-27b"
        assert got["system"] == "sys"

    def test_resposta_curta_falha_e_retorna_none(self, monkeypatch):
        """Resposta < MIN_VALID_RESPONSE_CHARS = provider morto → None."""
        monkeypatch.setattr(vt_hermes_helper, "_ask_llm_http_direct",
                            lambda *a, **k: "curto")
        resp = vt_hermes_helper.ask_llm("teste", timeout=60)
        assert resp is None

    def test_http_excecao_nao_levanta(self, monkeypatch):
        """Exceção no transporte vira None (contrato fail-safe)."""
        def _boom(*a, **k):
            raise RuntimeError("rede caiu")
        monkeypatch.setattr(vt_hermes_helper, "_ask_llm_http_direct", _boom)
        assert vt_hermes_helper.ask_llm("teste", timeout=60) is None


class TestYoloHttpRequest:
    """Corpo/headers da chamada HTTP direta ao yolo (detalhes do Wave 892)."""

    def _capture(self, monkeypatch, status=200, content="ok " + "x" * 60):
        captured = {}

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(
                    {"choices": [{"message": {"content": content}}]}
                ).encode()

        def _fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["body"] = json.loads(req.data.decode())
            return _FakeResp()

        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
        return captured

    def test_ua_contra_cloudflare(self, monkeypatch):
        """User-Agent explícito obrigatório (Python-urllib leva 403/1010)."""
        captured = self._capture(monkeypatch)
        vt_hermes_helper._ask_llm_http_direct("p", "yolo", "qwen3.8-27b", 30)
        headers = {k.lower(): v for k, v in captured["headers"].items()}
        assert "user-agent" in headers
        assert "python-urllib" not in headers["user-agent"].lower()

    def test_yolo_desliga_thinking(self, monkeypatch):
        """qwen3.8-27b do yolo emite reasoning antes do content — o request
        precisa de chat_template_kwargs.enable_thinking=False."""
        captured = self._capture(monkeypatch)
        vt_hermes_helper._ask_llm_http_direct("p", "yolo", "qwen3.8-27b", 30)
        assert captured["body"]["chat_template_kwargs"] == {"enable_thinking": False}

    def test_sem_chave_retorna_none(self, monkeypatch):
        """Sem YOLO_AUTO_API_KEY em .env nem environ → None, sem rede."""
        monkeypatch.setattr(vt_hermes_helper, "_load_hermes_env", lambda: {})
        monkeypatch.setenv("YOLO_AUTO_API_KEY", "")
        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("não deveria chamar rede")),
        )
        assert vt_hermes_helper._ask_llm_http_direct("p", "yolo", "qwen3.8-27b", 30) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
