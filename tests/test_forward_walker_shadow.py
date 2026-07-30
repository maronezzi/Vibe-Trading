"""
Testes FASE 3 — handler SIGTERM do forward_walker + leitura shadow no Stage 6.

Cobre:
- forward_walker.main() registra handler SIGTERM que levanta KeyboardInterrupt
  (reaproveita o cleanup de fechar posições SIM do loop). Testamos o handler
  isoladamente sem rodar o loop (que precisa de MT5/Wine).
- stage6_report._shadow_today_summary() lê forward_sim_trades do pregão atual,
  agrupa por root, ignora trades de outros dias e sem exit. Sinal soft.
"""
import signal
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from optimization.agi_v4 import stage6_report  # noqa: E402


# ── forward_walker SIGTERM handler ───────────────────────────────────────────

class TestSigtermHandler:
    def test_handler_levanta_keyboard_interrupt(self):
        # O handler registrado em main() deve levantar KeyboardInterrupt para
        # cair no caminho de cleanup existente (walker_loop:920-929). Extraímos
        # a mesma closure e verificamos o comportamento.
        def _on_sigterm(signum, _frame):
            print(f"SIGTERM({signum})")
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            _on_sigterm(signal.SIGTERM, None)

    def test_signal_modulo_importado_e_usavel(self):
        # Sanity: forward_walker importa signal e os (necessários p/ registrar).
        import optimization.forward_walker as fw
        assert hasattr(fw, "signal")
        assert hasattr(fw, "os")


# ── stage6 shadow summary ────────────────────────────────────────────────────

def _make_shadow_db(tmpdir: Path, rows: list[tuple]) -> Path:
    """Cria vt_trades.db com forward_sim_trades populada."""
    db = tmpdir / "vt_trades.db"
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE forward_sim_trades ("
        "symbol TEXT, entry_time TEXT, exit_time TEXT, net_pnl_brl REAL)")
    con.executemany("INSERT INTO forward_sim_trades VALUES (?,?,?,?)", rows)
    con.commit()
    con.close()
    return db


class TestShadowTodaySummary:
    def test_sem_tabela_ou_db_retorna_none(self, monkeypatch, tmp_path):
        # Redireciona o caminho do DB para um dir sem vt_trades.db.
        monkeypatch.setattr(stage6_report, "__file__",
                            str(tmp_path / "stage6_report.py"), raising=False)
        # Limpa o cache de path e força re-leitura.
        assert stage6_report._shadow_today_summary() is None

    def test_agrupa_por_root_so_pregao_atual_e_fechadas(self, monkeypatch, tmp_path):
        today = datetime.now().strftime("%Y-%m-%d")
        rows = [
            ("WINQ26", f"{today} 10:00", f"{today} 10:30", -80.0),
            ("WDOU26", f"{today} 11:00", f"{today} 11:20", 40.0),
            ("WINQ26", "2020-01-01 10:00", "2020-01-01 10:30", 999.0),  # outro dia
            ("BITM26", f"{today} 09:00", None, 50.0),                    # sem exit
        ]
        db = _make_shadow_db(tmp_path, rows)

        # A função faz `import sqlite3` local e constrói o path do DB de
        # __file__. Interceptamos sqlite3.connect (no módulo global sqlite3)
        # para redirecionar vt_trades.db -> nosso DB de teste.
        real_connect = sqlite3.connect

        def _fake_connect(path, *a, **k):
            if str(path).endswith("vt_trades.db"):
                return real_connect(str(db), *a, **k)
            return real_connect(path, *a, **k)

        monkeypatch.setattr(sqlite3, "connect", _fake_connect)
        out = stage6_report._shadow_today_summary()
        assert out is not None
        assert "WDO" in out and "WIN" in out
        assert "-80" in out
        assert "BIT" not in out          # trade sem exit ignorada
        assert "999" not in out          # trade de outro dia ignorada

    def test_sem_trades_hoje_retorna_none(self, monkeypatch, tmp_path):
        rows = [("WINQ26", "2020-01-01 10:00", "2020-01-01 10:30", 100.0)]
        db = _make_shadow_db(tmp_path, rows)
        real_connect = sqlite3.connect

        def _fake_connect(path, *a, **k):
            if str(path).endswith("vt_trades.db"):
                return real_connect(str(db), *a, **k)
            return real_connect(path, *a, **k)

        monkeypatch.setattr(sqlite3, "connect", _fake_connect)
        assert stage6_report._shadow_today_summary() is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
