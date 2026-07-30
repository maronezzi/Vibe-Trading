"""
Helper para localizar o binário do hermes em ambientes com PATH restrito (cron, systemd).
Usado por todos os scripts do Vibe-Trading para garantir que notificações Telegram funcionem.

Telegram limits (Bot API):
- Texto: 4096 caracteres por mensagem
- Caption de mídia: 1024 caracteres
- Parse mode: Markdown ou HTML

Pitfall: mensagens com LLM analysis podem estourar 4096 chars e o Telegram
retorna 400 Bad Request — a mensagem aparece truncada ou some silenciosamente.
Solução: split_and_send divide mensagens longas em múltiplos envios.
"""
import os
import shutil
import subprocess
import time
import logging
from pathlib import Path as _Path


# Limites do Telegram Bot API (com margem de segurança)
TELEGRAM_MAX_MESSAGE_CHARS = 4000  # 4096 limite, 4000 margem segura para tags Markdown
TELEGRAM_MAX_CAPTION_CHARS = 1024  # 1024 limite, 1000 margem segura


def find_hermes() -> str | None:
    """Localiza o binário do hermes. Retorna path absoluto ou None."""
    for p in [
        os.path.expanduser("~/.local/bin/hermes"),
        os.path.expanduser("~/.hermes/hermes-agent/venv/bin/hermes"),
        shutil.which("hermes"),
    ]:
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _split_long_message(message: str, max_chars: int = TELEGRAM_MAX_MESSAGE_CHARS) -> list[str]:
    """
    Divide uma mensagem longa em chunks respeitando o limite do Telegram.

    Estratégia:
    1. Se cabe em max_chars, retorna 1 elemento
    2. Senão, tenta quebrar em \n\n (parágrafos)
    3. Se um parágrafo sozinho for maior que max_chars, quebra por \n
    4. Se uma linha sozinha for maior que max_chars, quebra em palavras
    5. Cada chunk recebe um prefixo "[N/M]" se for parte de mensagem dividida

    Pitfall: o prefixo [N/M] é adicionado DEPOIS do split, então descontamos
    a margem de prefixo (10 chars) do max_chars efetivo para evitar estourar
    o limite do Telegram quando a mensagem é dividida.
    """
    if len(message) <= max_chars:
        return [message]

    # Margem para prefixo [N/M] (até 10 chars: [999/999])
    prefix_margin = 10
    effective_max = max_chars - prefix_margin

    chunks = []

    # Primeiro, tentar quebrar por parágrafos (\n\n)
    paragraphs = message.split("\n\n")

    current = ""
    for p in paragraphs:
        # Se o parágrafo sozinho excede o limite, vai ser subdividido depois
        test = (current + "\n\n" + p) if current else p
        if len(test) <= effective_max:
            current = test
        else:
            if current:
                chunks.append(current)
            current = p

    if current:
        chunks.append(current)

    # Se ainda houver chunks maiores que o limite, subdividir por linhas
    final_chunks = []
    for chunk in chunks:
        if len(chunk) <= effective_max:
            final_chunks.append(chunk)
            continue
        # Quebrar por \n
        lines = chunk.split("\n")
        current = ""
        for line in lines:
            test = (current + "\n" + line) if current else line
            if len(test) <= effective_max:
                current = test
            else:
                if current:
                    final_chunks.append(current)
                current = line
        if current:
            final_chunks.append(current)

    # Se ainda houver chunks > limite (linha única gigante), quebrar por palavras
    safe_chunks = []
    for chunk in final_chunks:
        if len(chunk) <= effective_max:
            safe_chunks.append(chunk)
            continue
        # Linha muito longa — quebrar em palavras
        words = chunk.split(" ")
        current = ""
        for w in words:
            test = (current + " " + w) if current else w
            if len(test) <= effective_max:
                current = test
            else:
                if current:
                    safe_chunks.append(current)
                current = w
        if current:
            safe_chunks.append(current)

    # Se AINDA houver chunks > limite (palavra única gigante sem espaços),
    # quebrar caractere por caractere em fatias
    char_chunks = []
    for chunk in safe_chunks:
        if len(chunk) <= effective_max:
            char_chunks.append(chunk)
            continue
        # String sem espaços — quebrar em fatias de effective_max chars
        for i in range(0, len(chunk), effective_max):
            char_chunks.append(chunk[i:i + effective_max])

    safe_chunks = char_chunks

    # Prefixar com "[N/M]" se a mensagem original foi dividida
    if len(safe_chunks) > 1:
        total = len(safe_chunks)
        # Recalcular margem real com base no número de dígitos do total
        prefix_len = len(f"[{total}/{total}] ")  # ex: "[3/3] " = 6
        safe_chunks = [f"[{i+1}/{total}] {c[:effective_max - prefix_len]}" for i, c in enumerate(safe_chunks)]

    return safe_chunks


def hermes_send(telegram_target: str, message: str, timeout: int = 30) -> bool:
    """
    Envia mensagem via hermes send. Retorna True se TODOS os chunks foram enviados.

    Pitfall: mensagens longas do LLM analysis podem estourar 4096 chars (limite
    Telegram). Esta função divide automaticamente em chunks menores e envia
    cada um como mensagem separada, prefixada com "[N/M]".

    Pitfall: hermes_send silenciosamente engolia erros ANTES — agora retorna
    False se o subprocess falha, mas o caller precisa checar (commit 3ebffa0e).
    """
    import subprocess
    hermes_bin = find_hermes()
    if not hermes_bin:
        return False

    chunks = _split_long_message(message)

    try:
        all_ok = True
        for chunk in chunks:
            result = subprocess.run(
                [hermes_bin, "send", "-t", telegram_target, chunk],
                capture_output=True, timeout=timeout,
                env={**os.environ, "PATH": os.environ.get("PATH", "") + f":{os.path.dirname(hermes_bin)}"},
            )
            if result.returncode != 0:
                all_ok = False
        return all_ok
    except Exception:
        return False


def hermes_send_caption(telegram_target: str, message: str, timeout: int = 30) -> bool:
    """Igual a hermes_send, mas com limite menor (caption de mídia = 1024)."""
    chunks = _split_long_message(message, max_chars=TELEGRAM_MAX_CAPTION_CHARS)
    import subprocess
    hermes_bin = find_hermes()
    if not hermes_bin:
        return False
    try:
        all_ok = True
        for chunk in chunks:
            result = subprocess.run(
                [hermes_bin, "send", "-t", telegram_target, chunk],
                capture_output=True, timeout=timeout,
                env={**os.environ, "PATH": os.environ.get("PATH", "") + f":{os.path.dirname(hermes_bin)}"},
            )
            if result.returncode != 0:
                all_ok = False
        return all_ok
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════
# Provider LLM — usado pelo AGI v4 (stage2_intel, stage4_generate)
# Adicionado em Wave 875.0 (2026-07-08) — antes desta wave, stage2/stage4
# caiam em `except ImportError` silencioso e o AGI não iterava (Lei 5 violada).
# ═══════════════════════════════════════════════════════════════════
_ASK_LLM_LOG_PATH = _Path("/tmp/vt_ask_llm.log")
_ASK_LLM_LOGGER: logging.Logger | None = None


def _get_ask_llm_logger() -> logging.Logger:
    """Logger único para ask_llm — arquivo dedicado pra debug + telemetria."""
    global _ASK_LLM_LOGGER
    if _ASK_LLM_LOGGER is None:
        ask_logger = logging.getLogger("vt_ask_llm")
        if not ask_logger.handlers:
            ask_handler = logging.FileHandler(_ASK_LLM_LOG_PATH)
            ask_handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            )
            ask_logger.addHandler(ask_handler)
            ask_logger.setLevel(logging.DEBUG)
            ask_logger.propagate = False
        _ASK_LLM_LOGGER = ask_logger
    return _ASK_LLM_LOGGER


# Provedores LLM — mesma cadeia de fallback de core/vt_order_validator_v2.py
# Wave qwen-primário (Bruno 30/07): o PRIMÁRIO agora é o default do hermes
# (qwen3.8-max-preview / alibaba-token-plan) — NÃO passamos -m/--provider,
# deixando o hermes usar o modelo configurado. Antes a cadeia forçava MiniMax-M3
# com timeout 10s, mas o MiniMax leva ~15s de cold-start → toda chamada do Stage
# 4 do AGI timeout antes de completar (168 chamadas, 0 sucessos em 30/07).
# model=None sinaliza "usar default do hermes" no loop abaixo.
_ASK_LLM_PROVIDERS = [
    {"provider": None,           "model": None,             "timeout": 60},   # default hermes (qwen3.8) — cold-start + geração de código
    {"provider": "minimax-oauth", "model": "MiniMax-M3",    "timeout": 25},   # fallback 1 (sync validator_v2: 10s→25s)
    {"provider": "xiaomi",        "model": "mimo-v2.5-pro", "timeout": 25},   # fallback 2
]


def ask_llm(
    prompt: str,
    *,
    timeout: int = 60,
    system: str | None = None,
) -> str | None:
    """Provider LLM único para o AGI e futuros callers cross-module.

    Tenta provedores em ordem: default do hermes (qwen3.8) → MiniMax-M3 →
    MiMo v2.5 Pro. Retorna a primeira resposta não-vazia ou ``None`` em
    qualquer falha — nunca levanta.

    Args:
        prompt: texto a enviar.
        timeout: budget total em segundos (default 60). Cada provedor tem
            seu próprio timeout interno; o deadline global limita a soma.
        system: prompt de sistema (opcional). Pré-penda via flag ``-s`` do
            hermes se suportado; ignorado silenciosamente caso contrário.

    Returns:
        Resposta (str) ou ``None`` se todos os provedores falharam/hermes ausente.

    Notas:
        - Adicionado em Wave 875.0 (2026-07-08) — corrige ImportError silencioso
          que deixava ``optimization/agi_v4/stage2_intel.py:153`` e
          ``stage4_generate.py:268`` retornando listas vazias em vez de
          hipóteses.
        - Logs em ``/tmp/vt_ask_llm.log`` (separado do validator_v2).
        - Cache explícito por prompt fica fora deste contrato — callers que
          quiserem cache devem implementar wrapper próprio (validator_v2 já
          tem cache próprio em ``_llm_cache``).
    """
    ask_log = _get_ask_llm_logger()
    hermes_bin = find_hermes()
    if not hermes_bin:
        ask_log.debug("ask_llm: hermes não encontrado no PATH")
        return None

    deadline = time.time() + timeout
    for prov in _ASK_LLM_PROVIDERS:
        remaining = deadline - time.time()
        if remaining <= 2:
            ask_log.debug("ask_llm: sem budget restante para próximo provedor")
            break
        per_timeout = min(prov["timeout"], int(remaining))

        # model=None → default do hermes; usa um label legível no log.
        label = prov["model"] or "hermes-default(qwen)"

        args = [hermes_bin, "-z", prompt]
        # model=None → usa o default do hermes (qwen3.8-max-preview). Não
        # passamos -m/--provider, deixando o hermes usar o que está configurado
        # (robusto: se o Bruno trocar o modelo no hermes, o AGI segue automático).
        if prov["model"] is not None and prov["provider"] is not None:
            args += ["-m", prov["model"], "--provider", prov["provider"]]
        if system:
            args += ["-s", system]

        t0 = time.time()
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=per_timeout,
            )
        except subprocess.TimeoutExpired:
            ask_log.warning(
                f"ask_llm: {label} timeout após {per_timeout}s"
            )
            continue
        except Exception as exc:
            ask_log.warning(f"ask_llm: {label} erro: {exc}")
            continue

        elapsed = time.time() - t0
        if result.returncode == 0 and result.stdout and result.stdout.strip():
            resp = result.stdout.strip()
            ask_log.debug(
                f"ask_llm: {label} OK ({elapsed:.1f}s, {len(resp)} chars)"
            )
            return resp
        stderr_snip = (result.stderr or "")[:200]
        ask_log.debug(
            f"ask_llm: {label} falhou rc={result.returncode} "
            f"stderr={stderr_snip}"
        )

    ask_log.debug("ask_llm: todos os provedores falharam")
    return None
