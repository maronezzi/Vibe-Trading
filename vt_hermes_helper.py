"""
Helper para localizar o binário do hermes em ambientes com PATH restrito (cron, systemd).
Usado por todos os scripts do Vibe-Trading para garantir que notificações Telegram funcionem.
"""
import os
import shutil


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


def hermes_send(telegram_target: str, message: str, timeout: int = 30) -> bool:
    """Envia mensagem via hermes send. Retorna True se sucesso."""
    import subprocess
    hermes_bin = find_hermes()
    if not hermes_bin:
        return False
    try:
        subprocess.run(
            [hermes_bin, "send", "-t", telegram_target, message],
            capture_output=True, timeout=timeout,
            env={**os.environ, "PATH": os.environ.get("PATH", "") + f":{os.path.dirname(hermes_bin)}"},
        )
        return True
    except Exception:
        return False
