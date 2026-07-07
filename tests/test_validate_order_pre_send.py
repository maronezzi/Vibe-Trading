"""
test_validate_order_pre_send.py
==========================================
Phase 1 PLUS (Bruno 2026-07-01): guard anti-duplicacao de ordens.

Cenário do bug ORIGINAL (Fase 1):
  14:50: BITN26 BUY ticket 2468137734 entra
  14:50: modify_sl falha 3x
  14:50: emergency_close disparado (PnL +0,00)
  14:53: bot re-cria BITN26 BUY ticket 2468153727 (mesma direcao, mesmo magic)
  15:00: ticket continua aberto com SL invalido
  WATCHDOG alerta orphan: MT5 1 pos, bot 0 pos, sync 0

FIX original: validate_order_pre_send() consultava MT5.status() ANTES de
enviar BUY/SELL. Se ja existe pos aberta com mesmo magic+symbol, retornava
False e bloqueava.

Wave Per-TF (Bruno 2026-07-07): semantica muda de (magic+symbol) para
(magic+symbol+tf). Cada TF vira slot independente — M15 BUY aberto NAO
bloqueia mais M30 BUY no mesmo symbol. validate_order_pre_send agora
consulta state.positions[f"{symbol}_{tf}"] em vez de MT5.

Cenarios cobertos pelos testes:
  - Mesmo (symbol, tf) ja aberto → BLOQUEIA (defesa anti-duplicacao mantida)
  - Mesmo symbol mas TF diferente → PERMITE (novo modelo per-TF)
  - state.positions vazio → PERMITE (caminho feliz)
  - tf vazio (legado) → cai no fallback magic+symbol via MT5

Fase 2.5 (Bruno 2026-07-01): refactor — validate_order_pre_send agora delega
para core.vt_truth.validate_order_pre_send (truth layer autoritativo).
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


def _make_pos(ticket=2468137734, symbol="BITN26", magic=555501, ptype="BUY", volume=1.0):
    """Helper: cria uma posicao no formato de MT5 status()['positions']."""
    return {
        "ticket": ticket,
        "symbol": symbol,
        "magic": magic,
        "type": ptype,
        "volume": volume,
        "price_open": 312600.0,
        "profit": 0.0,
    }


def _reset_truth_caches():
    """Limpa TTL cache do truth layer entre testes."""
    from core import vt_truth
    vt_truth._reset_caches_for_testing()


def _reset_state_positions():
    """Limpa state.positions entre testes (per-TF eh state-based)."""
    from core.vt_autotrader import state
    state.positions.clear()
    state.cross_tf_cooldown.clear()


class TestValidateOrderPreSend(unittest.TestCase):
    """validate_order_pre_send() bloqueia duplicacao POR TIMEFRAME."""

    def setUp(self):
        _reset_truth_caches()
        _reset_state_positions()

    def tearDown(self):
        _reset_truth_caches()
        _reset_state_positions()

    def test_blocks_when_same_symbol_and_tf_already_open(self):
        """CASO PRINCIPAL DO BUG: pos BITN26_M15 BUY ja aberta -> novo BITN26_M15 BUY bloqueado."""
        from core.vt_autotrader import state
        state.positions["BITN26_M15"] = {
            "direction": "BUY",
            "entry_price": 312600.0,
            "entry_ticket": "2468137734",
        }
        from core.vt_autotrader import validate_order_pre_send
        result = validate_order_pre_send("BITN26", tf="M15", direction="BUY")
        self.assertFalse(
            result,
            "Deveria BLOQUEAR novo BUY quando slot BITN26_M15 ja tem pos BUY aberta"
        )

    def test_allows_different_tf_same_symbol(self):
        """Wave Per-TF: M15 BUY aberto NAO bloqueia M30 BUY (slot independente)."""
        from core.vt_autotrader import state
        state.positions["WDON26_M15"] = {
            "direction": "BUY",
            "entry_price": 5500.0,
            "entry_ticket": "111",
        }
        from core.vt_autotrader import validate_order_pre_send
        result = validate_order_pre_send("WDON26", tf="M30", direction="BUY")
        self.assertTrue(
            result,
            "Deveria PERMITIR M30 BUY quando so M15 BUY esta aberto (per-TF)"
        )

    def test_allows_opposite_direction_different_tf(self):
        """Wave Per-TF: M15 BUY aberto NAO bloqueia M30 SELL no mesmo symbol."""
        from core.vt_autotrader import state
        state.positions["WINM26_M15"] = {
            "direction": "BUY",
            "entry_price": 120000.0,
            "entry_ticket": "222",
        }
        from core.vt_autotrader import validate_order_pre_send
        result = validate_order_pre_send("WINM26", tf="M30", direction="SELL")
        self.assertTrue(
            result,
            "Deveria PERMITIR M30 SELL quando so M15 BUY esta aberto (per-TF)"
        )

    def test_blocks_when_same_tf_same_direction_open(self):
        """Mesmo TF + mesma direction ja aberto: BLOQUEIA (anti-duplicacao)."""
        from core.vt_autotrader import state
        state.positions["BITN26_M5"] = {
            "direction": "BUY",
            "entry_price": 312600.0,
            "entry_ticket": "333",
        }
        from core.vt_autotrader import validate_order_pre_send
        result = validate_order_pre_send("BITN26", tf="M5", direction="BUY")
        self.assertFalse(
            result,
            "Deveria BLOQUEAR novo BUY quando slot M5 ja tem pos BUY aberta"
        )

    def test_blocks_reverse_direction_same_tf(self):
        """Mesmo TF mas direction reversa: BLOQUEIA (slot ja ocupado)."""
        from core.vt_autotrader import state
        state.positions["BITN26_M5"] = {
            "direction": "BUY",
            "entry_price": 312600.0,
            "entry_ticket": "444",
        }
        from core.vt_autotrader import validate_order_pre_send
        result = validate_order_pre_send("BITN26", tf="M5", direction="SELL")
        self.assertFalse(
            result,
            "Deveria BLOQUEAR SELL quando M5 ja tem BUY aberta (slot per-TF ja ocupado)"
        )

    def test_allows_when_no_positions_open(self):
        """Caminho feliz: state.positions vazio -> permite envio."""
        from core.vt_autotrader import validate_order_pre_send
        result = validate_order_pre_send("BITN26", tf="M5", direction="BUY")
        self.assertTrue(
            result,
            "Deveria PERMITIR novo BUY quando nao ha nenhuma pos aberta"
        )

    def test_allows_when_open_position_is_different_symbol(self):
        """Pos aberta em symbol diferente nao bloqueia."""
        from core.vt_autotrader import state
        state.positions["WINM26_M5"] = {
            "direction": "BUY",
            "entry_price": 120000.0,
            "entry_ticket": "555",
        }
        from core.vt_autotrader import validate_order_pre_send
        result = validate_order_pre_send("WDON26", tf="M5", direction="BUY")
        self.assertTrue(
            result,
            "Deveria PERMITIR BUY WDON26_M5 quando pos aberta eh em WINM26_M5"
        )

    def test_logs_blocked_duplicate_marker(self):
        """Garante que o log emitido usa o marker [BLOCKED-DUPLICATE-TF]
        (consumido por grep / dashboard / watchdog)."""
        from core.vt_autotrader import state
        state.positions["BITN26_M15"] = {
            "direction": "BUY",
            "entry_price": 312600.0,
            "entry_ticket": "777",
        }
        from core.vt_autotrader import validate_order_pre_send
        with patch("core.vt_truth._log") as mock_log:
            validate_order_pre_send("BITN26", tf="M15", direction="BUY")

        all_log_calls = [str(c) for c in mock_log.call_args_list]
        joined = "\n".join(all_log_calls)
        self.assertIn(
            "[BLOCKED-DUPLICATE-TF]",
            joined,
            f"Log deveria conter marker [BLOCKED-DUPLICATE-TF]. Calls={all_log_calls}"
        )

    def test_legacy_no_tf_falls_back_to_mt5(self):
        """Compat: sem tf no caller, fallback consulta MT5 (legado)."""
        with patch("core.vt_truth._mt5_status") as mock_status:
            mock_status.return_value = {
                "positions": [_make_pos(symbol="BITN26", magic=555501, ptype="BUY")],
                "account": {},
            }
            from core.vt_autotrader import validate_order_pre_send
            result = validate_order_pre_send("BITN26", tf="", direction="BUY")
        self.assertFalse(
            result,
            "Deveria BLOQUEAR via fallback MT5 quando tf vazio e pos aberta"
        )

    def test_legacy_no_tf_allows_when_mt5_empty(self):
        """Compat: sem tf, sem pos no MT5 -> permite."""
        with patch("core.vt_truth._mt5_status") as mock_status:
            mock_status.return_value = {"positions": [], "account": {}}
            from core.vt_autotrader import validate_order_pre_send
            result = validate_order_pre_send("BITN26", tf="", direction="BUY")
        self.assertTrue(
            result,
            "Deveria PERMITIR via fallback MT5 quando tf vazio e MT5 vazio"
        )

    def test_fail_safe_allows_when_state_unavailable(self):
        """FAIL-SAFE: se state indisponivel, NAO bloquear."""
        # Import direto do truth layer — simular falha importando state None
        with patch("core.vt_autotrader.state", new=None):
            from core.vt_truth import validate_order_pre_send as truth_vops
            # truth layer faz import lazy; se falhar, retorna True
            result = truth_vops("BITN26", tf="M5", direction="BUY")
        # Se chegou aqui sem crash, eh boa: FAIL-SAFE nao bloqueia por defeito de leitura
        self.assertTrue(
            result,
            "Deveria PERMITIR envio se state indisponivel (FAIL-SAFE anti-lockup)"
        )


class TestValidateOrderWiredInExecuteEntry(unittest.TestCase):
    """Garante via AST que validate_order_pre_send() esta WIREADA em
    _execute_entry() ANTES de safe_buy/safe_sell (call site real)."""

    def setUp(self):
        _reset_truth_caches()
        _reset_state_positions()

    def tearDown(self):
        _reset_truth_caches()
        _reset_state_positions()

    def test_function_called_before_safe_buy_in_execute_entry(self):
        src_path = Path(PROJECT_ROOT) / "core" / "vt_autotrader.py"
        src = src_path.read_text()

        # Posicao da chamada a validate_order_pre_send (FORA da definicao)
        def_idx = src.find("def validate_order_pre_send")
        call_idx = src.find("validate_order_pre_send(", def_idx + 1)
        self.assertGreater(
            call_idx, 0,
            "validate_order_pre_send() NAO esta sendo chamada em vt_autotrader.py "
            "FORA da definicao. Phase 1 PLUS precisa wirear antes de safe_buy/safe_sell."
        )

        # Posicao da primeira chamada a safe_buy
        first_safe_buy = src.find("safe_buy(")
        first_safe_sell = src.find("safe_sell(")
        candidates = [x for x in (first_safe_buy, first_safe_sell) if x > 0]
        first_order = min(candidates) if candidates else 10**9

        self.assertLess(
            call_idx, first_order,
            f"validate_order_pre_send() chamada em L{call_idx} mas safe_buy/sell "
            f"em L{first_order} — validate DEVE estar ANTES do envio"
        )

    def test_execute_entry_returns_blocked_duplicate_when_validation_fails(self):
        """Integracao leve: _execute_entry() com validate_order_pre_send
        bloqueando deve retornar BLOCKED-DUPLICATE e NAO chamar safe_buy/safe_sell."""
        from core import vt_autotrader

        with patch.object(vt_autotrader, "validate_order_pre_send", return_value=False), \
             patch.object(vt_autotrader, "safe_buy") as mock_safe_buy, \
             patch.object(vt_autotrader, "safe_sell") as mock_safe_sell:
            result = vt_autotrader._execute_entry(
                symbol="BITN26",
                tf="M5",
                direction="BUY",
                price=312600.0,
                sl_pts=150,
                atr=1500.0,
                bar_ts=None,
                strategy="VWAP",
            )

        self.assertIsNotNone(result)
        assert result is not None  # narrowing para Pyright
        self.assertEqual(result.get("status"), "BLOCKED")
        self.assertEqual(result.get("reason"), "BLOCKED-DUPLICATE")
        self.assertEqual(result.get("symbol"), "BITN26")
        mock_safe_buy.assert_not_called()
        mock_safe_sell.assert_not_called()


if __name__ == "__main__":
    unittest.main()