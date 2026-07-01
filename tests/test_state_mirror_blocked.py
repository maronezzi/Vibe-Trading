"""
test_state_mirror_blocked.py
============================

Wave 12 — FASE 3 (2026-07-01, Bruno): state.json REMOVIDO.
State vira projecao em memoria, reconstruida do MT5 a cada restart.

CONTEXTO HISTORICO (FASE 1/2):
    save() escrevia /tmp/vt_autotrader_state.json apos filtrar
    state.positions via _fetch_mt5_truth_symbols() (magic=555501 +
    comment=VibeTrading). Serve como "defesa em profundidade" ate Fase 3.

FASE 3 (2026-07-01):
    save() eh descontinuado (no-op com WARN). Load() tambem. Toda escrita
    em /tmp/vt_autotrader_state.json eh removida. State reconstruido
    exclusivamente via rebuild_state_from_mt5() consultando
    core.vt_truth.get_open_positions().

O QUE ESTE TESTE PROTEGE (apos adaptacao Fase 3):
    1. save() NAO escreve arquivo /tmp/vt_autotrader_state.json (no-op).
    2. save() loga WARN "save() descontinuado" cada vez que eh chamado.
    3. load() NAO le disco: state vazio permanece vazio.
    4. restart do autotrader = state vazio + WARN log.
    5. _fetch_mt5_truth_symbols() ainda funciona (utilitario mantido
       para reuse futuro / debug).
    6. FAIL-SAFE: _fetch_mt5_truth_symbols() retorna None em MT5 down.

Referencia:
    data/architecture_proposal_2026_07_01.md secao 4.3 (state rebuild).
    tests/test_state_removal.py — suite NOVA da Fase 3 (8 testes
    focalizados em rebuild_state_from_mt5() + no save/load on disk).
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


def _mt5_position(symbol, direction="BUY", magic=555501, comment="VibeTrading",
                  ticket=12345, price_open=100.0, volume=1.0):
    """Helper: monta um dict de posicao no formato que mt5_executor.status() retorna."""
    return {
        "ticket": ticket,
        "symbol": symbol,
        "type": 0 if direction == "BUY" else 1,
        "volume": volume,
        "price_open": price_open,
        "price_current": price_open,
        "sl": 0.0,
        "tp": 0.0,
        "profit": 0.0,
        "swap": 0.0,
        "comment": comment,
        "time": "2026-07-01 12:00:00",
        "magic": magic,
        "identifier": ticket,
        "time_msc": 0,
        "reason": 0,
        "external_id": "",
    }


class _IsolatedStateMixin:
    """Redireciona STATE_FILE do SessionState para tmp por teste.

    STATE_FILE foi descontinuado na Fase 3, mas o atributo de classe
    ainda existe (legado) — testes legacy podem redirecionar pra tmp
    sem poluir /tmp real. Se Fase 3 ativa corretamente, save() NAO
    escreve mesmo com STATE_FILE redirecionado.
    """

    STATE_FILE_KEY = "STATE_FILE"

    def setUp(self):
        # Limpa cache class-level de truth
        from core import vt_autotrader
        vt_autotrader.SessionState._mt5_truth_symbols_cache = None
        vt_autotrader.SessionState._mt5_truth_symbols_ts = 0.0

        # tmp file path (NAO deve ser criado por save())
        self._tmpdir = tempfile.mkdtemp(prefix="vt_state_mirror_")
        self._state_path = os.path.join(self._tmpdir, "vt_autotrader_state.json")
        self._tmp_state_path = self._state_path + ".tmp"

        # Patch STATE_FILE no SessionState
        self._orig_state_file = getattr(vt_autotrader.SessionState, "STATE_FILE")
        setattr(vt_autotrader.SessionState, "STATE_FILE", self._state_path)

    def tearDown(self):
        from core import vt_autotrader
        setattr(vt_autotrader.SessionState, "STATE_FILE", self._orig_state_file)
        vt_autotrader.SessionState._mt5_truth_symbols_cache = None
        vt_autotrader.SessionState._mt5_truth_symbols_ts = 0.0
        try:
            if os.path.exists(self._state_path):
                os.unlink(self._state_path)
            if os.path.exists(self._tmp_state_path):
                os.unlink(self._tmp_state_path)
            os.rmdir(self._tmpdir)
        except OSError:
            pass


class TestStateSaveDiscontinued(_IsolatedStateMixin, unittest.TestCase):
    """FASE 3: save() eh no-op. NAO escreve arquivo. Loga WARN."""

    def test_save_does_not_create_state_json(self):
        """save() NAO cria /tmp/vt_autotrader_state.json (Fase 3)."""
        from core import vt_autotrader

        state = vt_autotrader.SessionState()
        state.positions["WINM26_M5"] = {"direction": "BUY", "entry_ticket": "111"}
        state.daily_pnl = 100.0

        # Garante que arquivo NAO existe antes do save
        if os.path.exists(self._state_path):
            os.unlink(self._state_path)

        state.save()

        # Arquivo NAO deve ter sido criado por save()
        assert not os.path.exists(self._state_path), (
            f"Fase 3 quebrada: save() criou {self._state_path}. "
            f"State deveria ser projecao em memoria (no-op)."
        )

    def test_save_logs_warn_state_mirror(self):
        """save() loga WARN avisando que foi descontinuado."""
        from core import vt_autotrader

        state = vt_autotrader.SessionState()

        with patch("builtins.print") as mock_print:
            state.save()

        # Pelo menos um print com substring "save() descontinuado"
        warn_logs = [
            call for call in mock_print.call_args_list
            if call.args and "save()" in str(call.args[0])
            and "descontinuado" in str(call.args[0])
        ]
        assert warn_logs, (
            f"save() deveria logar WARN 'descontinuado'. Print: "
            f"{[str(c.args[0]) for c in mock_print.call_args_list]}"
        )

    def test_save_does_not_query_mt5_status(self):
        """save() descontinuado NAO chama status() do MT5.

        Era usado para filtrar pre-write (Fase 2). Fase 3 NAO faz I/O,
        NAO chama Wine. Toda decisao passa por _truth no proximo tick.
        """
        from core import vt_autotrader

        state = vt_autotrader.SessionState()

        with patch.object(vt_autotrader, "status") as mock_status:
            state.save()

        # save() eh no-op puro: NAO consulta MT5 status()
        assert mock_status.call_count == 0, (
            f"save() nao deveria consultar MT5 (no-op). "
            f"Chamadas status() durante save(): {mock_status.call_count}"
        )

    def test_save_safe_to_call_repeatedly(self):
        """save() pode ser chamado N vezes sem efeito (no-op idempotente)."""
        from core import vt_autotrader

        state = vt_autotrader.SessionState()
        state.positions["WINM26_M5"] = {"direction": "BUY"}

        for _ in range(10):
            state.save()

        # Nenhuma escrita em disco apos 10 save() — idem-pure no-op.
        assert not os.path.exists(self._state_path), (
            "save() criou arquivo apos 10 chamadas — deveria ser no-op"
        )


class TestStateLoadDiscontinued(_IsolatedStateMixin, unittest.TestCase):
    """FASE 3: load() eh no-op. NAO le disco."""

    def test_load_does_not_read_state_json(self):
        """load() NAO le /tmp/vt_autotrader_state.json."""
        from core import vt_autotrader

        # Mesmo se o arquivo existir (sobras de Fase 1/2), load() ignora.
        with open(self._state_path, "w") as f:
            json.dump({
                "positions": {"WINM26_M5": {"direction": "BUY", "entry_ticket": "111"}},
                "daily_trade_count": 999,  # lixo
            }, f)

        state = vt_autotrader.SessionState()  # __init__ chama _sync_daily_pnl (DB, nao disco)
        state.load()

        # State NAO foi populado com lixo do disco
        assert state.positions == {}, (
            f"load() deveria ser no-op (Fase 3). Positions apos load(): "
            f"{list(state.positions.keys())}"
        )
        assert state.daily_trade_count == 0, (
            f"load() NAO deveria ler disco. daily_trade_count={state.daily_trade_count}"
        )

    def test_load_init_does_not_read_existing_state_file(self):
        """Mesmo se arquivo existe, __init__ + load() NAO le.

        Cobre o cenario: cron reinicia autotrader, arquivo stale do
        dia anterior existe em /tmp, mas o estado eh reconstruido do MT5.
        """
        from core import vt_autotrader

        with open(self._state_path, "w") as f:
            json.dump({
                "current_day": "2026-06-30",  # ontem
                "positions": {"WINM26_M5": {"direction": "BUY"}},
                "daily_trade_count": 500,
            }, f)

        state = vt_autotrader.SessionState()
        state.load()

        # Lixo de ontem foi IGNORADO (Fase 3)
        assert state.positions == {}, "state carregado de arquivo stale"
        assert state.daily_trade_count == 0, "daily_trade_count carregado de disco"


class TestStateMirrorDirectUnit(_IsolatedStateMixin, unittest.TestCase):
    """Testes unitarios diretos em _fetch_mt5_truth_symbols (util mantido)."""

    def test_fetch_returns_set_filtered_by_magic_and_comment(self):
        from core import vt_autotrader

        mt5_status = {
            "account": {},
            "positions": [
                _mt5_position("WINM26", magic=555501, comment="VibeTrading"),  # NOSSO
                _mt5_position("WDOQ26", magic=555501, comment="VibeTrading"),  # NOSSO
                _mt5_position("BITN26", magic=999999, comment="VibeTrading"),  # outro EA (magic)
                _mt5_position("INDQ26", magic=555501, comment="manual"),       # manual (comment)
                _mt5_position("", magic=555501, comment="VibeTrading"),        # sem symbol
            ],
        }

        with patch.object(vt_autotrader, "status", return_value=mt5_status):
            truth = vt_autotrader.SessionState._fetch_mt5_truth_symbols()

        assert truth == {"WINM26", "WDOQ26"}, (
            f"Truth deveria ter so magic=555501+comment=VibeTrading. Veio: {truth}"
        )

    def test_fetch_returns_none_on_status_exception(self):
        from core import vt_autotrader

        with patch.object(vt_autotrader, "status", side_effect=ConnectionError("Wine timeout")):
            truth = vt_autotrader.SessionState._fetch_mt5_truth_symbols()

        assert truth is None, f"FAIL-SAFE: deveria retornar None quando MT5 falha. Veio: {truth}"

    def test_fetch_returns_none_on_invalid_type(self):
        from core import vt_autotrader

        with patch.object(vt_autotrader, "status", return_value="unexpected string"):
            truth = vt_autotrader.SessionState._fetch_mt5_truth_symbols()

        assert truth is None, f"FAIL-SAFE: deveria retornar None com tipo invalido. Veio: {truth}"

    def test_fetch_handles_positions_as_none(self):
        """positions=None (status() retornou dict mas sem positions) → set vazio, nao crash."""
        from core import vt_autotrader

        mt5_status = {"account": {}, "positions": None}
        with patch.object(vt_autotrader, "status", return_value=mt5_status):
            truth = vt_autotrader.SessionState._fetch_mt5_truth_symbols()

        assert truth == set(), f"Esperava set vazio. Veio: {truth}"

    def test_fetch_handles_magic_as_string(self):
        """magic vem como int 555501 mas pode vir como string '555501' (de JSON). Tratar."""
        from core import vt_autotrader

        mt5_status = {
            "account": {},
            "positions": [_mt5_position("WINM26", magic=555501)],
        }
        mt5_status["positions"][0]["magic"] = "555501"

        with patch.object(vt_autotrader, "status", return_value=mt5_status):
            truth = vt_autotrader.SessionState._fetch_mt5_truth_symbols()

        assert truth is not None and "WINM26" in truth, (
            f"magic como string deveria ser parseado. Veio: {truth}"
        )


if __name__ == "__main__":
    unittest.main()
