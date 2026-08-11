"""
test_ask_llm_bridge.py — Wave 875.0 (2026-07-08)

Valida o provider público ``ask_llm`` adicionado em ``core/vt_hermes_helper``
para destravar os stages 2 e 4 do AGI v4 (que antes caiam em ImportError
silencioso e zeram o pipeline de iteração — violação silenciosa da Lei 5).

Casos cobertos:
  1. ``ask_llm`` existe, é callable, retorna Optional[str], nunca levanta.
  2. Quando ``find_hermes`` retorna None → ``ask_llm`` retorna None (sem raise).
  3. Quando subprocess roda rc!=0 → ``ask_llm`` retorna None e loga debug.
  4. Quando subprocess tem stdout não-vazio → ``ask_llm`` retorna a string.
  5. TimeoutExpired → tratado, retorna None.
  6. Hardcode de secrets (api_key) NÃO presente no source.
  7. Stages 2 e 4 importam ``ask_llm`` sem cair em ImportError (smoke).
"""
from __future__ import annotations

import importlib
import logging
import subprocess
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _import_helper():
    """Importa core.vt_hermes_helper com sys.path ajustado (conftest já injeta)."""
    mod = importlib.import_module("core.vt_hermes_helper")
    return mod


# ════════════════════════════════════════════════════════════════════
# Testes estruturais (sem subprocess real)
# ════════════════════════════════════════════════════════════════════

def test_ask_llm_exists_and_is_callable():
    """Símbolo público deve existir e ser chamável."""
    mod = _import_helper()
    assert hasattr(mod, "ask_llm"), "ask_llm deve existir em vt_hermes_helper"
    assert callable(mod.ask_llm), "ask_llm deve ser callable"


def test_ask_llm_signature():
    """Signature: (prompt, *, timeout=60, system=None) -> Optional[str]."""
    import inspect

    mod = _import_helper()
    sig = inspect.signature(mod.ask_llm)
    params = list(sig.parameters.values())
    assert params[0].name == "prompt"
    assert params[1].name == "timeout"
    assert params[1].default == 60
    assert params[1].kind == inspect.Parameter.KEYWORD_ONLY
    assert params[2].name == "system"
    assert params[2].default is None
    assert params[2].kind == inspect.Parameter.KEYWORD_ONLY
    # retorno: str | None (anotação, não obrigatório, mas confere se existir)
    if sig.return_annotation is not inspect.Signature.empty:
        ra = str(sig.return_annotation)
        assert "str" in ra
        assert "None" in ra


def test_helpers_no_hardcoded_secrets():
    """Nenhum secret (api_key, token, password) hardcoded no módulo."""
    mod = _import_helper()
    src = Path(mod.__file__).read_text(encoding="utf-8")
    forbidden = [
        "sk-",                 # OpenAI-style
        "xai-",
        "ghp_",
        "API_KEY=",            # assignment literal
        "PASSWORD=",
        "TOKEN=",
    ]
    # Excluir comentários/docstrings com placeholders "VT_*_API_KEY" via env
    stripped = "\n".join(
        line for line in src.splitlines()
        if not line.strip().startswith("#")
        and "VT_" not in line
    )
    for needle in forbidden:
        assert needle not in stripped, f"Possível secret hardcoded: {needle!r}"


# ════════════════════════════════════════════════════════════════════
# Testes de fluxo (com find_hermes mockado)
# ════════════════════════════════════════════════════════════════════

def test_ask_llm_returns_none_when_hermes_missing(monkeypatch):
    """Se ``find_hermes()`` retorna None, ask_llm deve retornar None sem raise."""
    mod = _import_helper()
    monkeypatch.setattr(mod, "find_hermes", lambda: None)
    result = mod.ask_llm("qualquer prompt", timeout=10)
    assert result is None


def test_ask_llm_returns_string_on_first_provider_success(monkeypatch):
    """Se o primeiro provider retorna stdout não-vazio, ask_llm retorna."""
    mod = _import_helper()
    fake_hermes = "/fake/hermes"
    monkeypatch.setattr(mod, "find_hermes", lambda: fake_hermes)
    fake_completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="resposta-model-1 " + "x"*60, stderr=""
    )
    with mock.patch.object(subprocess, "run", return_value=fake_completed) as m:
        result = mod.ask_llm("p", timeout=30)
    assert result == "resposta-model-1 " + "x"*60
    # Primário (Wave 880.F) = zenmux/deepseek-v4-flash-free → passa -m/--provider
    args, _ = m.call_args
    cmd = args[0]
    assert "-m" in cmd
    assert "deepseek/deepseek-v4-flash-free" in cmd
    assert "--provider" in cmd
    assert "zenmux" in cmd


def test_ask_llm_falls_back_to_second_provider(monkeypatch):
    """Se o primário falha (rc!=0), tenta o fallback (MiMo v2.5 pro)."""
    mod = _import_helper()
    fake_hermes = "/fake/hermes"
    monkeypatch.setattr(mod, "find_hermes", lambda: fake_hermes)

    primary_fail = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="err primary"
    )
    fallback_ok = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="resposta-fallback " + "x"*60, stderr=""
    )
    with mock.patch.object(
        subprocess, "run", side_effect=[primary_fail, fallback_ok]
    ) as m:
        result = mod.ask_llm("p", timeout=60)
    assert result == "resposta-fallback " + "x"*60
    assert m.call_count == 2
    # 2ª chamada (fallback 1) deve usar zenmux/deepseek-v4-flash
    cmd2 = m.call_args_list[1].args[0]
    assert "deepseek/deepseek-v4-flash" in cmd2
    assert "zenmux" in cmd2


def test_ask_llm_returns_none_when_all_providers_fail(monkeypatch):
    """Se AMBOS falham, retorna None."""
    mod = _import_helper()
    monkeypatch.setattr(mod, "find_hermes", lambda: "/fake/hermes")
    fail = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="boom"
    )
    with mock.patch.object(subprocess, "run", return_value=fail):
        result = mod.ask_llm("p", timeout=60)
    assert result is None


def test_ask_llm_handles_timeout(monkeypatch):
    """TimeoutExpired num provider → tenta o próximo."""
    mod = _import_helper()
    monkeypatch.setattr(mod, "find_hermes", lambda: "/fake/hermes")
    fallback_ok = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="ok-after-timeout " + "x"*60, stderr=""
    )
    with mock.patch.object(
        subprocess, "run",
        side_effect=[subprocess.TimeoutExpired(cmd="hermes", timeout=10),
                     fallback_ok],
    ) as m:
        result = mod.ask_llm("p", timeout=60)
    assert result == "ok-after-timeout " + "x"*60
    assert m.call_count == 2


def test_ask_llm_handles_subprocess_exception(monkeypatch):
    """Exception genérica em subprocess.run → tenta o próximo."""
    mod = _import_helper()
    monkeypatch.setattr(mod, "find_hermes", lambda: "/fake/hermes")
    fallback_ok = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="ok-after-exc " + "x"*60, stderr=""
    )
    with mock.patch.object(
        subprocess, "run", side_effect=[OSError("fake"), fallback_ok]
    ):
        result = mod.ask_llm("p", timeout=60)
    assert result == "ok-after-exc " + "x"*60


def test_ask_llm_system_prompt_passed_as_flag(monkeypatch):
    """Se system é fornecido, hermes recebe flag ``-s``."""
    mod = _import_helper()
    monkeypatch.setattr(mod, "find_hermes", lambda: "/fake/hermes")
    fake_completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="ok", stderr=""
    )
    with mock.patch.object(subprocess, "run", return_value=fake_completed) as m:
        mod.ask_llm("user prompt", timeout=30, system="system instruction")
    args, _ = m.call_args
    cmd = args[0]
    assert "-s" in cmd
    assert "system instruction" in cmd
    assert "user prompt" in cmd


def test_ask_llm_logs_to_dedicated_file(monkeypatch):
    """Logger escreve em /tmp/vt_ask_llm.log (cobre sucesso, timeout, falha).

    Smoke mínimo: verifica que ``logging.getLogger('vt_ask_llm')`` é o
    logger usado — pytest caplog captura via root, então o handler dedicado
    existe mas o assert crítico é "log entrou no logger correto".
    """
    mod = _import_helper()
    log_obj = mod._get_ask_llm_logger()
    assert log_obj.name == "vt_ask_llm"
    assert any(
        isinstance(h, logging.FileHandler) for h in log_obj.handlers
    ), "ask_llm deve registrar FileHandler para /tmp/vt_ask_llm.log"


def _get_ask_llm_logger():
    return _import_helper()._get_ask_llm_logger()


# ════════════════════════════════════════════════════════════════════
# Smoke: stages 2 e 4 importam ask_llm sem ImportError em runtime
# ════════════════════════════════════════════════════════════════════

def test_stage4_generate_imports_ask_llm():
    """Garante que o import usado em stage4_generate funciona."""
    import optimization.agi_v4.stage4_generate as s4
    assert hasattr(s4, "_generate_code_via_llm")
    # chamada direta ao symbol — se ask_llm sumir de hermes_helper, isto quebra
    from core.vt_hermes_helper import ask_llm as _alias
    assert _alias is not None


def test_stage2_intel_imports_ask_llm():
    """Garante que o import usado em stage2_intel funciona."""
    import optimization.agi_v4.stage2_intel as s2
    assert hasattr(s2, "_ask_llm_for_hypotheses")
    from core.vt_hermes_helper import ask_llm as _alias
    assert _alias is not None


def test_ask_llm_no_real_subprocess_when_hermes_missing(monkeypatch):
    """Sem hermes, subprocess.run NÃO é chamado (curto-circuito)."""
    mod = _import_helper()
    monkeypatch.setattr(mod, "find_hermes", lambda: None)
    with mock.patch.object(subprocess, "run") as m:
        result = mod.ask_llm("p", timeout=5)
    assert result is None
    assert m.call_count == 0
