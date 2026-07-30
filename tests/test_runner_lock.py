"""
Testes do lock anti-paralelismo do runner.py (Wave anti-colisão 30/07).

Cobre:
- _acquire_run_lock() pega lock exclusivo na 1ª chamada.
- 2ª chamada (lock ocupado) é rejeitada (retorna None).
- Após liberar o lock, uma nova chamada consegue pegar.
- Comportamento cross-process: um subprocess segurando o lock impede o atual.

O lock impede que duas runs do AGI v4 rodem simultaneamente disputando o
Wine/MT5 (single-session) — foi a causa raiz do "16/16 failing" das 17h de
30/07 (job externo disparou runner.py direto, sem respeitar o PID lock do
wrapper, e colidiu com a run do cron).
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from optimization.agi_v4.runner import RUN_LOCK_PATH, _acquire_run_lock  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_lock():
    """Garante lock livre antes/depois de cada teste."""
    try:
        os.remove(str(RUN_LOCK_PATH))
    except FileNotFoundError:
        pass
    yield
    try:
        os.remove(str(RUN_LOCK_PATH))
    except FileNotFoundError:
        pass


class TestAcquireRunLock:
    def test_primeira_chamada_pegao_lock(self):
        fd = _acquire_run_lock()
        assert fd is not None, "1ª chamada deve conseguir pegar o lock"
        os.close(fd)

    def test_segunda_chamada_rejeitada_quando_ocupado(self):
        fd1 = _acquire_run_lock()
        assert fd1 is not None
        fd2 = _acquire_run_lock()
        assert fd2 is None, "2ª chamada com lock ocupado deve retornar None"
        os.close(fd1)

    def test_apos_liberar_nova_chamada_consegue(self):
        fd1 = _acquire_run_lock()
        os.close(fd1)
        fd2 = _acquire_run_lock()
        assert fd2 is not None, "após liberar, nova chamada deve pegar o lock"
        os.close(fd2)

    def test_lock_registra_pid_no_arquivo(self):
        fd = _acquire_run_lock()
        assert fd is not None
        os.lseek(fd, 0, os.SEEK_SET)
        conteudo = os.read(fd, 64).decode().strip()
        assert conteudo == str(os.getpid()), f"lock file deve conter o PID, got {conteudo!r}"
        os.close(fd)


class TestLockCrossProcess:
    """O lock deve funcionar entre processos diferentes (o caso real: cron vs job externo)."""

    def test_subprocess_segurando_lock_impede_processo_atual(self, tmp_path):
        # Subprocess que pega o lock e espera um sinal (arquivo) antes de sair.
        ready = tmp_path / "ready"
        go = tmp_path / "go"
        holder_script = tmp_path / "holder.py"
        holder_script.write_text(textwrap.dedent(f"""
            import sys, time
            from pathlib import Path
            sys.path.insert(0, "{_PROJECT_ROOT}")
            from optimization.agi_v4.runner import _acquire_run_lock
            fd = _acquire_run_lock()
            assert fd is not None, "holder deveria pegar o lock"
            Path("{ready}").write_text("ok")   # avisa que pegou
            # espera o sinal 'go' (até 30s) pra segurar o lock
            for _ in range(300):
                if Path("{go}").exists():
                    break
                time.sleep(0.1)
        """))
        # limpa o lock (o fixture já limpou, mas seguro)
        try:
            os.remove(str(RUN_LOCK_PATH))
        except FileNotFoundError:
            pass

        proc = subprocess.Popen([sys.executable, str(holder_script)])
        try:
            # espera o holder pegar o lock
            for _ in range(200):
                if ready.exists():
                    break
                import time as _t
                _t.sleep(0.1)
            assert ready.exists(), "holder não sinalizou que pegou o lock"

            # Agora este processo NÃO deve conseguir pegar (lock ocupado pelo holder).
            fd = _acquire_run_lock()
            assert fd is None, "processo atual deve falhar com lock ocupado pelo subprocess"
        finally:
            # libera o holder
            go.write_text("go")
            proc.wait(timeout=15)

        # Após o holder terminar, o lock deve estar livre de novo.
        # (pequena tolerância: o SO pode levar um instante pra liberar o flock)
        import time as _t
        got = None
        for _ in range(50):
            got = _acquire_run_lock()
            if got is not None:
                break
            _t.sleep(0.1)
        assert got is not None, "após holder terminar, lock deve estar livre"
        os.close(got)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
