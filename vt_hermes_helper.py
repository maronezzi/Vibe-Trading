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
