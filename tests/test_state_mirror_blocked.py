"""
test_state_mirror_blocked.py
============================

Wave 12 — Fase 2 (2026-07-01, Bruno): state.json vira MIRROR do MT5,
NAO cache autoritativo. Defesa em profundidade ate Fase 3 quando
state for removido de fato.

PROBLEMA:
    Bot escrevia state.json com positions baseado no DB logico
    (state.positions_set), NAO consultava MT5 diretamente. Resultado:
    quando havia drift DB↔MT5 (MT5 fechou sozinho, mas DB ainda
    marcava a posicao como aberta), state virava ORPHAN — segurava
    informacao stale que reaparecia em todo restart ate o proximo
    reconcile explicito.

FIX:
    SessionState.save() (em core/vt_autotrader.py) agora consulta
    MT5.status() ANTES de gravar. Filtra state.positions removendo
    qualquer symbol que nao esteja aberto no MT5 com magic=555501 +
    comment="VibeTrading". Log [STATE-MIRROR] para cada filtragem
    (audit trail).

    FAIL-SAFE: se MT5 falhar (status() exception ou retorno malformado),
    NAO filtra — preserva state. Bloquear save por causa de MT5 down
    seria pior que o orphan original.

O QUE ESTE TESTE PROTEGE:
    1. State com 3 symbols, MT5 com 1 → save() grava state com 1 so.
    2. Os 2 symbols removidos logam [STATE-MIRROR] (warn visivel).
    3. Symbol que NAO tem magic=555501 no MT5 (outro EA) tambem eh filtrado.
    4. Symbol sem comment VibeTrading (ex.: "manual") tambem eh filtrado.
    5. status() exception → FAIL-SAFE: state gravado sem filtro.
    6. status() retornando tipo invalido → FAIL-SAFE.
    7. Filtro idempotente: rodar 2x seguidas tem mesmo efeito.
    8. Cache TTL: 2 save() rapidos = 1 so chamada Wine (throttle).

Referencia:
    data/architecture_proposal_2026_07_01.md (Wave 12 — MT5 autoritativo).
    data/architecture_audit_2026_07_01.md secao 4.3 (drift state↔MT5).
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
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

    SessionState.STATE_FILE eh class attribute (="/tmp/vt_autotrader_state.json").
    Patchamos pra tmp_path/.../state.json. Isso evita poluir o /tmp real
    durante a suite de testes (cada teste ganha path NOVO).
    """

    STATE_FILE_KEY = "STATE_FILE"

    def setUp(self):
        # Limpa cache class-level de truth (senao teste A pode ver cache de teste B)
        from core import vt_autotrader
        vt_autotrader.SessionState._mt5_truth_symbols_cache = None
        vt_autotrader.SessionState._mt5_truth_symbols_ts = 0.0

        # tmp file path
        self._tmpdir = tempfile.mkdtemp(prefix="vt_state_mirror_")
        self._state_path = os.path.join(self._tmpdir, "vt_autotrader_state.json")
        self._tmp_state_path = self._state_path + ".tmp"

        # Patch STATE_FILE no SessionState (class attr, afeta todas as instancias).
        # Usamos setattr/getattr porque Pyright infere STATE_FILE como Literal type
        # (definido literalmente em core/vt_autotrader.py:167), o que impede assign
        # direto de outro str. setattr contorna isso em runtime.
        self._orig_state_file = getattr(vt_autotrader.SessionState, "STATE_FILE")
        setattr(vt_autotrader.SessionState, "STATE_FILE", self._state_path)

    def tearDown(self):
        from core import vt_autotrader
        setattr(vt_autotrader.SessionState, "STATE_FILE", self._orig_state_file)
        vt_autotrader.SessionState._mt5_truth_symbols_cache = None
        vt_autotrader.SessionState._mt5_truth_symbols_ts = 0.0
        # Cleanup tmpdir (best-effort)
        try:
            if os.path.exists(self._state_path):
                os.unlink(self._state_path)
            if os.path.exists(self._tmp_state_path):
                os.unlink(self._tmp_state_path)
            os.rmdir(self._tmpdir)
        except OSError:
            pass


class TestStateMirrorFiltersOrphans(_IsolatedStateMixin, unittest.TestCase):
    """save() consulta MT5 e remove symbols nao abertos (defesa em profundidade)."""

    def test_save_filters_symbols_not_in_mt5(self):
        """3 symbols em state, MT5 retorna 1. save() grava so o que esta no MT5."""
        from core import vt_autotrader

        # Mock status() retornando MT5 com APENAS WINM26 aberta
        mt5_status = {
            "account": {"balance": 1000.0, "equity": 1000.0, "free_margin": 1000.0},
            "positions": [_mt5_position("WINM26", direction="BUY", ticket=111)],
        }

        # Monta state com 3 symbols (so 1 estah no MT5)
        state = vt_autotrader.SessionState()
        state.positions["WINM26_M5"] = {
            "direction": "BUY", "entry_price": 100.0, "entry_ticket": "111",
            "symbol": "WINM26", "tf": "M5",
        }
        state.positions["WDOQ26_M5"] = {
            "direction": "SELL", "entry_price": 200.0, "entry_ticket": "222",
            "symbol": "WDOQ26", "tf": "M5",
        }  # <-- ORPHAN: MT5 nao tem
        state.positions["BITN26_M5"] = {
            "direction": "BUY", "entry_price": 300.0, "entry_ticket": "333",
            "symbol": "BITN26", "tf": "M5",
        }  # <-- ORPHAN: MT5 nao tem

        with patch.object(vt_autotrader, "status", return_value=mt5_status):
            state.save()

        # Verifica que o JSON em disco tem APENAS o symbol que estava no MT5
        assert os.path.exists(self._state_path), f"state.json nao foi gravado em {self._state_path}"
        with open(self._state_path) as f:
            data = json.load(f)

        saved_positions = data.get("positions", {})
        assert "WINM26_M5" in saved_positions, f"WINM26_M5 deveria estar no state (estah no MT5). Tem: {list(saved_positions)}"
        assert "WDOQ26_M5" not in saved_positions, f"WDOQ26_M5 deveria ter sido FILTRADO. Tem: {list(saved_positions)}"
        assert "BITN26_M5" not in saved_positions, f"BITN26_M5 deveria ter sido FILTRADO. Tem: {list(saved_positions)}"
        assert len(saved_positions) == 1, f"Deveria ter 1 position. Tem {len(saved_positions)}: {list(saved_positions)}"

    def test_save_logs_state_mirror_warning_when_filtering(self):
        """Quando filtra, loga [STATE-MIRROR] com nome do symbol (audit trail)."""
        from core import vt_autotrader

        mt5_status = {
            "account": {},
            "positions": [_mt5_position("WINM26", ticket=111)],
        }
        state = vt_autotrader.SessionState()
        state.positions["WINM26_M5"] = {
            "direction": "BUY", "entry_price": 100.0, "entry_ticket": "111",
            "symbol": "WINM26", "tf": "M5",
        }
        state.positions["WDOQ26_M5"] = {
            "direction": "SELL", "entry_price": 200.0, "entry_ticket": "222",
            "symbol": "WDOQ26", "tf": "M5",
        }  # ORPHAN

        with patch.object(vt_autotrader, "status", return_value=mt5_status):
            with patch("builtins.print") as mock_print:
                state.save()

        # Verifica que houve pelo menos um log [STATE-MIRROR] mencionando o symbol filtrado
        mirror_logs = [
            call for call in mock_print.call_args_list
            if call.args and "[STATE-MIRROR]" in str(call.args[0])
            and "WDOQ26" in str(call.args[0])
        ]
        assert mirror_logs, (
            f"Esperava log [STATE-MIRROR] para WDOQ26. Print calls: "
            f"{[str(c.args[0]) for c in mock_print.call_args_list]}"
        )

    def test_save_filters_symbol_with_wrong_magic(self):
        """MT5 com pos mas magic != 555501 (outro EA) → filtra do state."""
        from core import vt_autotrader

        # MT5 retorna pos com magic 999999 (outro EA, nao nosso)
        mt5_status = {
            "account": {},
            "positions": [_mt5_position("WDOQ26", magic=999999, ticket=999)],
        }
        state = vt_autotrader.SessionState()
        state.positions["WDOQ26_M5"] = {
            "direction": "BUY", "entry_price": 200.0, "entry_ticket": "999",
            "symbol": "WDOQ26", "tf": "M5",
        }

        with patch.object(vt_autotrader, "status", return_value=mt5_status):
            state.save()

        with open(self._state_path) as f:
            data = json.load(f)
        assert "WDOQ26_M5" not in data.get("positions", {}), (
            "Magic != 555501 nao deveria ser reconhecido como posicao nossa"
        )

    def test_save_filters_symbol_without_vibetrading_comment(self):
        """MT5 com pos mas comment != VibeTrading (manual ou outro bot) → filtra."""
        from core import vt_autotrader

        mt5_status = {
            "account": {},
            "positions": [_mt5_position("BITN26", comment="manual", ticket=555)],
        }
        state = vt_autotrader.SessionState()
        state.positions["BITN26_M5"] = {
            "direction": "BUY", "entry_price": 300.0, "entry_ticket": "555",
            "symbol": "BITN26", "tf": "M5",
        }

        with patch.object(vt_autotrader, "status", return_value=mt5_status):
            state.save()

        with open(self._state_path) as f:
            data = json.load(f)
        assert "BITN26_M5" not in data.get("positions", {}), (
            "Comment != VibeTrading nao deveria ser reconhecido como posicao nossa"
        )

    def test_save_keeps_all_when_all_in_mt5(self):
        """Happy path: tudo que esta no state tambem esta no MT5 → nada filtrado."""
        from core import vt_autotrader

        mt5_status = {
            "account": {},
            "positions": [
                _mt5_position("WINM26", ticket=111),
                _mt5_position("WDOQ26", ticket=222),
            ],
        }
        state = vt_autotrader.SessionState()
        state.positions["WINM26_M5"] = {"direction": "BUY", "entry_ticket": "111"}
        state.positions["WDOQ26_M5"] = {"direction": "SELL", "entry_ticket": "222"}

        with patch.object(vt_autotrader, "status", return_value=mt5_status):
            state.save()

        with open(self._state_path) as f:
            data = json.load(f)
        assert set(data["positions"].keys()) == {"WINM26_M5", "WDOQ26_M5"}, (
            f"Deveria manter ambos. Tem: {list(data['positions'])}"
        )


class TestStateMirrorFailSafe(_IsolatedStateMixin, unittest.TestCase):
    """FAIL-SAFE: MT5 falhou → state gravado sem filtro (bot nao trava)."""

    def test_save_preserves_state_when_status_raises(self):
        """status() exception → state gravado como esta (FAIL-SAFE)."""
        from core import vt_autotrader

        state = vt_autotrader.SessionState()
        state.positions["WINM26_M5"] = {"direction": "BUY", "entry_ticket": "111"}
        state.positions["WDOQ26_M5"] = {"direction": "SELL", "entry_ticket": "222"}

        with patch.object(vt_autotrader, "status", side_effect=RuntimeError("Wine down")):
            # NAO deve levantar
            state.save()

        # State gravado COM TUDO (FAIL-SAFE: nao filtra quando MT5 down)
        with open(self._state_path) as f:
            data = json.load(f)
        assert set(data["positions"].keys()) == {"WINM26_M5", "WDOQ26_M5"}, (
            f"FAIL-SAFE quebrado: state foi filtrado mesmo com status() falhando. "
            f"Positions: {list(data['positions'])}"
        )

    def test_save_preserves_state_when_status_returns_invalid_type(self):
        """status() retorna tipo nao-dict → FAIL-SAFE: state gravado sem filtro."""
        from core import vt_autotrader

        state = vt_autotrader.SessionState()
        state.positions["WINM26_M5"] = {"direction": "BUY", "entry_ticket": "111"}

        with patch.object(vt_autotrader, "status", return_value=["unexpected", "list"]):
            state.save()  # NAO deve levantar

        with open(self._state_path) as f:
            data = json.load(f)
        assert "WINM26_M5" in data.get("positions", {}), (
            "FAIL-SAFE quebrado: state foi filtrado com retorno invalido"
        )

    def test_save_filters_when_mt5_returns_empty(self):
        """MT5 retorna positions vazias → state eh espelhado (vazio).

        Comportamento intencional (mirror): se MT5 consistentemente diz que
        nao ha pos aberta, state NAO pode mentir segurando pos "fantasma".
        Caso diferente de status() exception (FAIL-SAFE preserva state):
        aqui a leitura foi BEM SUCEDIDA, so retornou vazio. Confia no MT5.

        Razao arquitetural: data/architecture_proposal_2026_07_01.md
        define state como "espelho" do MT5, NAO cache autoritativo. Se
        preservarmos state quando MT5 diz vazio, voltamos ao bug original
        (orphan persistente). O reconcile_positions_with_mt5() roda em
        paralelo e valida o mesmo truth, entao nao ha risco de remover
        pos legitima por race — se MT5 diz vazio em 2 leituras seguidas,
        eh legitimo.
        """
        from core import vt_autotrader

        mt5_status = {"account": {}, "positions": []}  # vazio
        state = vt_autotrader.SessionState()
        state.positions["WINM26_M5"] = {"direction": "BUY", "entry_ticket": "111"}

        with patch.object(vt_autotrader, "status", return_value=mt5_status):
            state.save()

        with open(self._state_path) as f:
            data = json.load(f)
        # Mirror behavior: MT5 vazio → state vazio
        assert data.get("positions", {}) == {}, (
            f"MT5 vazio deveria espelhar (state vazio). FAIL-SAFE so se aplica "
            f"a status() exception, nao a retorno vazio. Tem: {list(data['positions'])}"
        )


class TestStateMirrorIdempotency(_IsolatedStateMixin, unittest.TestCase):
    """Filtro eh idempotente e cacheado (evita spam Wine)."""

    def test_save_is_idempotent(self):
        """Rodar save() 2x seguidas com mesmo MT5 → mesmo resultado."""
        from core import vt_autotrader

        mt5_status = {
            "account": {},
            "positions": [_mt5_position("WINM26", ticket=111)],
        }
        state = vt_autotrader.SessionState()
        state.positions["WINM26_M5"] = {"direction": "BUY", "entry_ticket": "111"}
        state.positions["WDOQ26_M5"] = {"direction": "SELL", "entry_ticket": "222"}

        with patch.object(vt_autotrader, "status", return_value=mt5_status) as mock_status:
            state.save()
            state.save()
            state.save()

        with open(self._state_path) as f:
            data = json.load(f)
        assert list(data["positions"].keys()) == ["WINM26_M5"], (
            f"Idempotencia quebrada. Tem: {list(data['positions'])}"
        )

        # Cache TTL (5s) → 3 save() = 1 chamada status() (cache hit nas outras 2)
        # Pode ser 1 ou 2 dependendo se cache foi invalidado entre saves.
        assert mock_status.call_count <= 2, (
            f"Cache TTL quebrado: {mock_status.call_count} chamadas status() em 3 save() "
            f"rapidos (esperado 1-2)"
        )

    def test_save_invalidates_cache_after_ttl(self):
        """Apos TTL expirar, cache eh refeito (truth fresh)."""
        from core import vt_autotrader
        import time as _time

        # Mock status retornando conjunto diferente entre chamadas
        mt5_v1 = {"account": {}, "positions": [_mt5_position("WINM26", ticket=111)]}
        mt5_v2 = {
            "account": {},
            "positions": [
                _mt5_position("WINM26", ticket=111),
                _mt5_position("WDOQ26", ticket=222),
            ],
        }

        state = vt_autotrader.SessionState()
        state.positions["WINM26_M5"] = {"direction": "BUY", "entry_ticket": "111"}

        with patch.object(vt_autotrader, "status", side_effect=[mt5_v1, mt5_v2]) as mock_status:
            state.save()
            # Forca expiracao do cache (TTL=5s) sem dormir 5s
            vt_autotrader.SessionState._mt5_truth_symbols_ts -= 10
            state.positions["WDOQ26_M5"] = {"direction": "SELL", "entry_ticket": "222"}
            state.save()

        with open(self._state_path) as f:
            data = json.load(f)
        # Segunda save() deve ter usado mt5_v2 (WDOQ26 incluido)
        assert "WINM26_M5" in data["positions"]
        assert "WDOQ26_M5" in data["positions"], (
            "Cache TTL nao foi invalidado — WDOQ26 deveria ter sido mantido "
            "(estava no MT5 v2). Tem: " + str(list(data['positions']))
        )


class TestStateMirrorDirectUnit(_IsolatedStateMixin, unittest.TestCase):
    """Testes unitarios diretos em _fetch_mt5_truth_symbols e _filter_positions_via_mt5_truth."""

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

        # Simula payload cru de JSON onde magic ficou string (cenario real quando
        # algum intermediario serializa/desserializa via json.dumps/loads).
        mt5_status = {
            "account": {},
            "positions": [_mt5_position("WINM26", magic=555501)],
        }
        # Forca magic a virar string APOS construcao (preserva o resto da estrutura)
        mt5_status["positions"][0]["magic"] = "555501"

        with patch.object(vt_autotrader, "status", return_value=mt5_status):
            truth = vt_autotrader.SessionState._fetch_mt5_truth_symbols()

        assert truth is not None and "WINM26" in truth, (
            f"magic como string deveria ser parseado. Veio: {truth}"
        )


if __name__ == "__main__":
    unittest.main()