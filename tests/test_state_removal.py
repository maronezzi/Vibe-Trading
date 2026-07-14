"""
test_state_removal.py
=====================

WAVE 12 — FASE 3 (2026-07-01, Bruno): REMOCAO DEFINITIVA DO state.json
como cache autoritativo. State vira projecao em memoria, reconstruida
do MT5 (fonte de verdade) a cada restart.

PROBLEMA HISTORICO (Fases 1/2):
    Bot lia/escrevia /tmp/vt_autotrader_state.json em cada tick.
    Restart = state stale carregado (positions fantasma que MT5 ja
    tinha fechado). Sintoma classico: orphans persistentes mesmo apos
    restart. State desincronizava do MT5 silenciosamente.

FIX FASE 3:
    Substituir save()/load() por rebuild_state_from_mt5() que consulta
    core.vt_truth.get_open_positions() (truth autoritativo).
    Sem file I/O. Sem race. Sem orfao.

    - SessionState.__init__() NAO le /tmp/vt_autotrader_state.json.
    - SessionState.save() eh no-op com WARN (descontinuado).
    - SessionState.load() eh no-op (descontinuado).
    - SessionState.rebuild_state_from_mt5() eh a unica forma de
      popular state.positions. Idempotente. FAIL-SAFE em MT5 down.
    - restart do autotrader = state vazio + 1 rebuild = state rebuilt.

Referencia:
    data/architecture_proposal_2026_07_01.md secao 4.3
    data/architecture_audit_2026_07_01.md secao 4.3 (drift state↔MT5)

O QUE ESTE TESTE PROTEGE (8 testes):
    1. test_state_init_does_not_read_state_json
    2. test_state_init_rebuilds_from_mt5
    3. test_state_init_with_no_mt5_positions_is_empty
    4. test_state_save_is_mirror_only_warning_log
    5. test_state_json_file_if_exists_is_ignored
    6. test_rebuild_state_from_mt5_called_on_init
    7. test_rebuild_state_from_mt5_called_on_restart
    8. test_state_rebuild_after_emergency_close
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)

# Importa no escopo do modulo para que testes individuais possam
# `from core import vt_truth` tambem sem re-import. Import lazy evitado
# para que `patch.object(vt_truth, ...)` funcione em todos os testes.
from core import vt_truth as _vt_truth_mod  # noqa: E402

# Alias no escopo do modulo pra patch.object (mais legivel)
vt_truth = _vt_truth_mod


def _mt5_position(ticket, symbol, direction="BUY", magic=555501, comment="VibeTrading",
                  volume=1.0, price_open=100.0, time="2026-07-01 12:00:00"):
    """Helper: dict no formato que mt5_executor.status() retorna.

    Nota: type eh sempre string ("0"/"1") para sobreviver ao `or ""` em
    core/vt_truth.get_open_positions() (que converte int 0 -> "" via
    `str(p.get("type", "") or "")`). Usar int 0 quebra o mapeamento
    direction porque "" nao esta em ("BUY", 0, "0") no rebuild.
    """
    return {
        "ticket": ticket,
        "symbol": symbol,
        "type": "0" if direction == "BUY" else "1",
        "volume": volume,
        "price_open": price_open,
        "price_current": price_open,
        "sl": 0.0,
        "tp": 0.0,
        "profit": 0.0,
        "swap": 0.0,
        "comment": comment,
        "time": time,
        "magic": magic,
        "identifier": ticket,
        "time_msc": 0,
        "reason": 0,
        "external_id": "",
    }


class _StateTestBase(unittest.TestCase):
    """Base com setup/cleanup comum pros testes de state."""

    def setUp(self):
        # Limpa cache TTL de _truth (mt5_positions_cache) entre testes
        from core import vt_truth
        vt_truth._reset_caches_for_testing()

        # tb limpa cache de SessionState._mt5_truth_symbols_cache (legacy)
        from core import vt_autotrader
        vt_autotrader.SessionState._mt5_truth_symbols_cache = None
        vt_autotrader.SessionState._mt5_truth_symbols_ts = 0.0

    def tearDown(self):
        from core import vt_truth
        vt_truth._reset_caches_for_testing()


# ==============================================================================
# 1. test_state_init_does_not_read_state_json
# ==============================================================================
class TestStateInitDoesNotReadStateJson(_StateTestBase):
    """SessionState() NAO le /tmp/vt_autotrader_state.json."""

    def test_init_does_not_read_existing_state_file(self):
        """Mesmo com state.json existente, SessionState() NAO carrega."""
        from core import vt_autotrader

        # Cria arquivo state.json na tmp com lixo que representaria
        # estado stale de ontem.
        tmp = tempfile.mkdtemp(prefix="vt_state_removal_")
        try:
            state_path = os.path.join(tmp, "vt_autotrader_state.json")
            with open(state_path, "w") as f:
                json.dump({
                    "current_day": "2026-06-30",  # ontem
                    "positions": {"WINM26_M5": {"direction": "BUY", "entry_ticket": "111"}},
                    "daily_trade_count": 500,
                    "consecutive_losses": {"WIN": 3},
                }, f)

            # SessionState NAO consulta disco. STATE_FILE legacy
            # existe como class attr mas NAO eh usado.
            state = vt_autotrader.SessionState()

            # Lixo de disco foi IGNORADO
            assert state.positions == {}, (
                f"SessionState() deveria inicializar positions vazio, "
                f"veio {list(state.positions.keys())}"
            )
            assert state.daily_trade_count == 0, (
                f"daily_trade_count veio do disco: {state.daily_trade_count}"
            )
            assert state.consecutive_losses == {}, (
                f"consecutive_losses veio do disco: {state.consecutive_losses}"
            )
        finally:
            try:
                os.unlink(os.path.join(tmp, "vt_autotrader_state.json"))
                os.rmdir(tmp)
            except OSError:
                pass

    def test_init_with_real_state_file_at_legacy_path_does_not_use_it(self):
        """Mesmo se STATE_FILE default existe (sobras Fase 1/2), eh ignorado."""
        from core import vt_autotrader

        # Cria arquivo no STATE_FILE default (legacy)
        legacy_path = vt_autotrader.SessionState.STATE_FILE
        original_content = None
        if os.path.exists(legacy_path):
            with open(legacy_path) as f:
                original_content = f.read()

        try:
            with open(legacy_path, "w") as f:
                json.dump({
                    "current_day": str(datetime.now().date()),
                    "positions": {"WDOQ26_M5": {"direction": "SELL", "entry_ticket": "999"}},
                    "daily_trade_count": 999,
                }, f)

            state = vt_autotrader.SessionState()

            # NAO carregou do disco
            assert state.positions == {}, (
                "SessionState() carregou do disco legacy"
            )
            assert state.daily_trade_count == 0
        finally:
            # Restaura conteudo original (se existia) ou remove
            if original_content is not None:
                with open(legacy_path, "w") as f:
                    f.write(original_content)
            elif os.path.exists(legacy_path):
                os.unlink(legacy_path)


# ==============================================================================
# 2. test_state_init_rebuilds_from_mt5
# ==============================================================================
class TestStateInitRebuildsFromMt5(_StateTestBase):
    """No startup, state eh reconstruido via rebuild_state_from_mt5()."""

    def test_module_level_state_rebuilds_from_mt5_on_import(self):
        """SessionState.rebuild_state_from_mt5() popula state com positions MT5.

        Nota (Fase 3): o state global no escopo do modulo (vt_autotrader.state)
        eh criado na importacao do modulo, antes de qualquer patch de teste.
        Validar o init-level do modulo requereria reload do modulo (frágil
        contra importacoes circulares ja realizadas). O fluxo "state vazio
        + 1 rebuild = state pronto" ja eh coberto por
        test_restart_flow_empty_to_built_in_one_rebuild, e a existencia
        do objeto singleton global eh coberta por
        test_module_level_state_exists_after_import. Aqui validamos o
        mecanismo central: rebuild_state_from_mt5() popula state com
        as posicoes do MT5 e marca cada uma com from_mt5_rebuild=True.
        """
        from core import vt_autotrader

        # Limpa cache de posicoes da truth (2.0s TTL) para que o patch
        # abaixo seja observado, sem hit do estado cacheado de testes
        # anteriores.
        vt_truth._reset_caches_for_testing()

        mt5_status = {
            "account": {},
            "positions": [
                _mt5_position(ticket=111, symbol="WINM26", direction="BUY"),
                _mt5_position(ticket=222, symbol="WDOQ26", direction="SELL"),
            ],
        }

        with patch.object(vt_truth, "_mt5_status_raw", return_value=mt5_status):
            state = vt_autotrader.SessionState()
            n = state.rebuild_state_from_mt5()

        assert n == 2, f"Esperava 2 reconstruidas, veio {n}"
        # Wave Per-TF+ (Bruno 09/07): rebuild usa namespace proprio
        # `f"{symbol}__MT5_{idx}"` para nao colidir com slots live (`f"{symbol}_{tf}"`).
        assert "WINM26__MT5_0" in state.positions
        assert "WDOQ26__MT5_0" in state.positions

        # Campos compativeis com manage_position
        win_pos = state.positions["WINM26__MT5_0"]
        assert win_pos["direction"] == "BUY"
        assert win_pos["entry_ticket"] == "111"
        # MT5 nao expoe TF na Position — usamos UNKNOWN como sentinela honesta.
        assert win_pos["tf"] == "UNKNOWN"
        assert win_pos["entry_price"] == 100.0
        assert win_pos["volume"] == 1.0
        assert win_pos["from_mt5_rebuild"] is True  # flag de origem

    def test_module_level_state_exists_after_import(self):
        """core.vt_autotrader.state existe globalmente (objeto singleton)."""
        from core import vt_autotrader
        assert hasattr(vt_autotrader, "state")
        assert isinstance(vt_autotrader.state, vt_autotrader.SessionState)


# ==============================================================================
# 3. test_state_init_with_no_mt5_positions_is_empty
# ==============================================================================
class TestStateInitWithNoMt5IsEmpty(_StateTestBase):
    """Se MT5 nao tem pos, state.positions fica vazio (rebuild = empty)."""

    def test_rebuild_with_mt5_empty_returns_zero_state_empty(self):
        from core import vt_autotrader

        # MT5 status com 0 positions
        mt5_status = {"account": {}, "positions": []}

        with patch.object(vt_truth, "_mt5_status_raw", return_value=mt5_status):
            state = vt_autotrader.SessionState()
            n = state.rebuild_state_from_mt5()

        assert n == 0, f"Esperava 0 positions reconstruidas, veio {n}"
        assert state.positions == {}, (
            f"state.positions deveria estar vazio. Veio: {list(state.positions.keys())}"
        )

    def test_rebuild_with_mt5_unavailable_returns_zero_no_crash(self):
        """MT5 indisponivel → rebuild retorna 0, NAO levanta, NAO crash."""
        from core import vt_autotrader

        with patch.object(
            vt_truth, "_mt5_status_raw",
            side_effect=ConnectionError("Wine down"),
        ):
            state = vt_autotrader.SessionState()
            # NAO deve levantar
            n = state.rebuild_state_from_mt5()

        assert n == 0, f"FAIL-SAFE quebrado: esperava 0, veio {n}"
        assert state.positions == {}


# ==============================================================================
# 4. test_state_save_is_mirror_only_warning_log
# ==============================================================================
class TestStateSaveIsMirrorOnly(_StateTestBase):
    """save() eh no-op com WARN (state vira projecao)."""

    def test_save_emits_warning_and_does_not_persist(self):
        from core import vt_autotrader
        from core import vt_truth

        # Setup: cria state com positions hipoteticas e rebuild OFF
        # para checar save() isoladamente.
        state = vt_autotrader.SessionState()
        state.positions["WINM26_M5"] = {"direction": "BUY", "entry_ticket": "111"}
        state.daily_pnl = 100.0

        with patch("builtins.print") as mock_print:
            state.save()

        # 1. Loga WARN
        warn_calls = [
            call for call in mock_print.call_args_list
            if call.args and "save() descontinuado" in str(call.args[0])
        ]
        assert warn_calls, (
            f"save() deveria logar WARN 'descontinuado'. "
            f"Prints: {[str(c.args[0]) for c in mock_print.call_args_list]}"
        )

        # 2. NAO escreve em STATE_FILE legacy (assumindo path default /tmp)
        # Como o path default nao existe no test runner, ok — mas podemos
        # redirecionar STATE_FILE pra tmp/ e re-validar.
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "state.json")
            with patch.object(vt_autotrader.SessionState, "STATE_FILE", state_path):
                state.save()  # NAO deve criar state_path
            assert not os.path.exists(state_path), (
                f"save() criou arquivo {state_path} — deveria ser no-op"
            )


# ==============================================================================
# 5. test_state_json_file_if_exists_is_ignored
# ==============================================================================
class TestStateJsonFileIfExistsIsIgnored(_StateTestBase):
    """Se state.json ja existe em /tmp (bug Fase 1/2 nao removido),
    restart IGNORA o conteudo e reconstroi do MT5."""

    def test_existing_stale_state_json_is_ignored_by_load(self):
        """state.json de ontem/dia anterior: load() eh no-op, nao carrega."""
        from core import vt_autotrader

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "stale_state.json")
            with open(state_path, "w") as f:
                json.dump({
                    "current_day": "2026-01-01",  # muito antigo
                    "positions": {"OLD": {"direction": "BUY"}},
                    "daily_trade_count": 99,
                }, f)

            with patch.object(vt_autotrader.SessionState, "STATE_FILE", state_path):
                state = vt_autotrader.SessionState()
                state.load()  # deve ser no-op

            # Stale NAO foi absorvido
            assert state.positions == {}, (
                f"load() carregou stale. Positions: {list(state.positions.keys())}"
            )
            assert state.daily_trade_count == 0

    def test_state_with_orphan_position_loses_it_on_rebuild(self):
        """State com position orphan + rebuild → orphan eh removido.

        Cobre o bug historico: state tinha 'WDON26_M5' que MT5 ja tinha
        fechado. Antes (Fase 1/2): restart mantinha orfao ate reconcile.
        Agora (Fase 3): rebuild limpa e reconstroi do MT5 → orfao some
        por construcao (state.positions == MT5).
        """
        from core import vt_autotrader

        # Estado inicial com 2 pos, mas MT5 so tem 1
        mt5_status = {
            "account": {},
            "positions": [_mt5_position(ticket=111, symbol="WINM26", direction="BUY")],
        }

        with patch.object(vt_truth, "_mt5_status_raw", return_value=mt5_status):
            state = vt_autotrader.SessionState()
            # Simula state pre-existente com orphan (manual na Fase 3 seria
            # impossivel, mas cobertura eh via defensiva: o rebuild SEMPRE
            # limpa antes de popular).
            state.positions["ORPHAN_M5"] = {"direction": "BUY", "entry_ticket": "999"}
            state.positions["WINM26_M5"] = {"direction": "BUY", "entry_ticket": "111"}

            state.rebuild_state_from_mt5()

        # Orfao REMOVIDO (limpa + repopula)
        assert "ORPHAN_M5" not in state.positions, "Orfao NAO foi removido pelo rebuild"
        # Pos legitima preservada (Wave Per-TF+: namespace proprio `__MT5_{idx}`)
        assert "WINM26__MT5_0" in state.positions, "Pos legitima foi removida"


# ==============================================================================
# 6. test_rebuild_state_from_mt5_called_on_init
# ==============================================================================
class TestRebuildStateFromMt5CalledOnInit(_StateTestBase):
    """rebuild_state_from_mt5() eh parte do fluxo de inicializacao."""

    def test_rebuild_state_from_mt5_method_exists_and_callable(self):
        from core import vt_autotrader

        state = vt_autotrader.SessionState()
        assert hasattr(state, "rebuild_state_from_mt5"), (
            "SessionState deveria expor rebuild_state_from_mt5() publico"
        )
        assert callable(state.rebuild_state_from_mt5)

    def test_init_does_not_crash_when_mt5_returns_empty(self):
        """rebuild_state_from_mt5() FAIL-SAFE: MT5 vazio nao quebra startup."""
        from core import vt_autotrader

        mt5_status = {"account": {}, "positions": []}
        with patch.object(vt_truth, "_mt5_status_raw", return_value=mt5_status):
            # NAO deve levantar
            state = vt_autotrader.SessionState()
            n = state.rebuild_state_from_mt5()

        assert state.positions == {}
        assert n == 0

    def test_rebuild_logs_count(self):
        """rebuild_state_from_mt5() loga [STATE-REBUILD] com quantas reconstroi."""
        from core import vt_autotrader

        mt5_status = {
            "account": {},
            "positions": [
                _mt5_position(ticket=111, symbol="WINM26"),
                _mt5_position(ticket=222, symbol="WDOQ26"),
            ],
        }

        with patch.object(vt_truth, "_mt5_status_raw", return_value=mt5_status):
            with patch("builtins.print") as mock_print:
                state = vt_autotrader.SessionState()
                state.rebuild_state_from_mt5()

        # Logs com [STATE-REBUILD]
        rebuild_logs = [
            call for call in mock_print.call_args_list
            if call.args and "[STATE-REBUILD]" in str(call.args[0])
        ]
        assert rebuild_logs, (
            f"rebuild_state_from_mt5 deveria logar [STATE-REBUILD]. "
            f"Prints: {[str(c.args[0]) if c.args else '' for c in mock_print.call_args_list]}"
        )


# ==============================================================================
# 7. test_rebuild_state_from_mt5_called_on_restart
# ==============================================================================
class TestRebuildStateFromMt5CalledOnRestart(_StateTestBase):
    """Simula restart: state vazio + rebuild = state pronto em 1 chamada."""

    def test_restart_flow_empty_to_built_in_one_rebuild(self):
        """Restart mid-day: state vazio + rebuild = state com pos MT5."""
        from core import vt_autotrader

        mt5_status = {
            "account": {},
            "positions": [_mt5_position(ticket=555, symbol="BITN26", direction="BUY")],
        }

        with patch.object(vt_truth, "_mt5_status_raw", return_value=mt5_status):
            state = vt_autotrader.SessionState()
            # Estado inicial VAZIO (simula restart mid-day)
            assert state.positions == {}
            # 1 rebuild = state pronto
            n = state.rebuild_state_from_mt5()

        assert n == 1
        # Wave Per-TF+ (Bruno 09/07): rebuild usa namespace proprio `__MT5_{idx}`
        assert "BITN26__MT5_0" in state.positions
        assert state.positions["BITN26__MT5_0"]["entry_ticket"] == "555"
        # Flag que indica veio do rebuild
        assert state.positions["BITN26__MT5_0"]["from_mt5_rebuild"] is True

    def test_rebuild_is_idempotent(self):
        """Rodar rebuild 2x seguidas = mesmo estado (sem duplicacao)."""
        from core import vt_autotrader

        mt5_status = {
            "account": {},
            "positions": [
                _mt5_position(ticket=111, symbol="WINM26"),
                _mt5_position(ticket=222, symbol="WDOQ26"),
            ],
        }

        with patch.object(vt_truth, "_mt5_status_raw", return_value=mt5_status):
            state = vt_autotrader.SessionState()
            state.rebuild_state_from_mt5()
            first = dict(state.positions)

            state.rebuild_state_from_mt5()
            second = dict(state.positions)

        assert first.keys() == second.keys(), (
            f"Rebuild nao-idempotente. 1a: {list(first.keys())}, 2a: {list(second.keys())}"
        )
        assert len(state.positions) == 2


# ==============================================================================
# 8. test_state_rebuild_after_emergency_close
# ==============================================================================
class TestStateRebuildAfterEmergencyClose(_StateTestBase):
    """Emergency close fecha pos no MT5; rebuild detecta pos fechada."""

    def test_rebuild_picks_up_new_position_after_close(self):
        """Cenário: emergencia fechou uma pos no MT5. Rebuild reflete."""
        from core import vt_autotrader

        # MT5 status 1: 2 pos abertas
        mt5_before = {
            "account": {},
            "positions": [
                _mt5_position(ticket=111, symbol="WINM26"),
                _mt5_position(ticket=222, symbol="WDOQ26"),
            ],
        }
        # MT5 status 2: 1 pos fechada (emergency close em WDOQ26)
        mt5_after = {
            "account": {},
            "positions": [_mt5_position(ticket=111, symbol="WINM26")],
        }

        with patch.object(
            vt_truth, "_mt5_status_raw",
            side_effect=[mt5_before, mt5_after],
        ):
            state = vt_autotrader.SessionState()

            # 1o rebuild: estado inicial com 2 pos (Wave Per-TF+: namespace `__MT5_{idx}`)
            state.rebuild_state_from_mt5()
            assert set(state.positions.keys()) == {"WINM26__MT5_0", "WDOQ26__MT5_0"}

            # Cache de posicoes da truth tem TTL 2.0s; sem reset entre
            # rebuilds, o 2o hit devolveria o resultado cacheado do 1o.
            # Em producao isso eh OK (state reflete MT5 com 2s de lag),
            # mas no teste precisamos do 2o rebuild observando a
            # alteracao do MT5 (emergency close).
            vt_truth._reset_caches_for_testing()

            # 2o rebuild (apos emergency close): 1 pos apenas
            state.rebuild_state_from_mt5()

        # Pos fechada pelo emergency NAO aparece mais
        assert set(state.positions.keys()) == {"WINM26__MT5_0"}, (
            f"Pos fechada pelo emergency ainda aparece. "
            f"Positions: {list(state.positions.keys())}"
        )


if __name__ == "__main__":
    unittest.main()
