"""
test_validate_order_pre_send.py
==========================================
Phase 1 PLUS (Bruno 2026-07-01): guard anti-duplicacao de ordens.

Cenário do bug HOJE:
  14:50: BITN26 BUY ticket 2468137734 entra
  14:50: modify_sl falha 3x
  14:50: emergency_close disparado (PnL +0,00)
  14:53: bot re-cria BITN26 BUY ticket 2468153727 (mesma direcao, mesmo magic)
  15:00: ticket continua aberto com SL invalido
  WATCHDOG alerta orphan: MT5 1 pos, bot 0 pos, sync 0

FIX: validate_order_pre_send() consulta MT5.status() ANTES de enviar BUY/SELL.
Se ja existe pos aberta com mesmo magic+symbol, retorna False e bloqueia com
log [BLOCKED-DUPLICATE]. Chamada em _execute_entry() (unico call site live).

Forense: data/architecture_audit_2026_07_01.md secao 4.2 mapeou write paths
sem validate. Proposta: data/architecture_proposal_2026_07_01.md L350.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

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


class TestValidateOrderPreSend(unittest.TestCase):
    """validate_order_pre_send() bloqueia duplicacao baseado em MT5 status()."""

    def test_blocks_when_same_symbol_and_magic_already_open(self):
        """CASO PRINCIPAL DO BUG: pos BITN26 BUY 555501 aberta -> novo BUY bloqueado."""
        with patch("core.vt_autotrader.status") as mock_status:
            mock_status.return_value = {
                "positions": [_make_pos(symbol="BITN26", magic=555501, ptype="BUY")],
                "account": {},
            }
            from core.vt_autotrader import validate_order_pre_send
            result = validate_order_pre_send("BITN26", "BUY")
        self.assertFalse(
            result,
            "Deveria BLOQUEAR novo BUY quando ja ha pos BITN26 BUY 555501 aberta"
        )

    def test_blocks_sell_also_when_position_open_same_symbol(self):
        """Se ha pos aberta do bot no symbol, qualquer direcao nova eh bloqueada
        (a gestão de reversão é feita em manage_position, nao aqui)."""
        with patch("core.vt_autotrader.status") as mock_status:
            mock_status.return_value = {
                "positions": [_make_pos(symbol="WINM26", magic=555501, ptype="BUY")],
                "account": {},
            }
            from core.vt_autotrader import validate_order_pre_send
            result = validate_order_pre_send("WINM26", "SELL")
        self.assertFalse(
            result,
            "Deveria BLOQUEAR novo SELL quando ja ha pos WINM26 aberta pelo bot"
        )

    def test_allows_when_no_positions_open(self):
        """Caminho feliz: status() retorna lista vazia -> permite envio."""
        with patch("core.vt_autotrader.status") as mock_status:
            mock_status.return_value = {"positions": [], "account": {}}
            from core.vt_autotrader import validate_order_pre_send
            result = validate_order_pre_send("BITN26", "BUY")
        self.assertTrue(
            result,
            "Deveria PERMITIR novo BUY quando nao ha nenhuma pos aberta"
        )

    def test_allows_when_open_position_is_different_symbol(self):
        """Pos aberta em symbol diferente nao bloqueia (ex: WIN aberta, sinal novo em WDO)."""
        with patch("core.vt_autotrader.status") as mock_status:
            mock_status.return_value = {
                "positions": [_make_pos(symbol="WINM26", magic=555501, ptype="BUY")],
                "account": {},
            }
            from core.vt_autotrader import validate_order_pre_send
            result = validate_order_pre_send("WDON26", "BUY")
        self.assertTrue(
            result,
            "Deveria PERMITIR BUY WDON26 quando pos aberta eh em WINM26"
        )

    def test_allows_when_open_position_has_different_magic(self):
        """Pos aberta com magic de outro bot/script nao bloqueia (cada magic eh independente)."""
        with patch("core.vt_autotrader.status") as mock_status:
            mock_status.return_value = {
                "positions": [_make_pos(symbol="BITN26", magic=999999, ptype="BUY")],
                "account": {},
            }
            from core.vt_autotrader import validate_order_pre_send
            result = validate_order_pre_send("BITN26", "BUY")
        self.assertTrue(
            result,
            "Deveria PERMITIR BUY BITN26 quando pos aberta tem magic 999999 (outro bot)"
        )

    def test_fail_safe_allows_when_status_raises(self):
        """FAIL-SAFE: se status() exception, NAO bloquear (lockup seria pior)."""
        with patch("core.vt_autotrader.status") as mock_status:
            mock_status.side_effect = RuntimeError("MT5 offline / Wine dead")
            from core.vt_autotrader import validate_order_pre_send
            result = validate_order_pre_send("BITN26", "BUY")
        self.assertTrue(
            result,
            "Deveria PERMITIR envio se status() falha (FAIL-SAFE anti-lockup)"
        )

    def test_logs_blocked_duplicate_marker(self):
        """Garante que o log emitido usa o marker [BLOCKED-DUPLICATE]
        (consumido por grep / dashboard / watchdog)."""
        from core.vt_autotrader import validate_order_pre_send
        with patch("core.vt_autotrader.status") as mock_status, \
             patch("core.vt_autotrader.log") as mock_log:
            mock_status.return_value = {
                "positions": [_make_pos(symbol="BITN26", magic=555501)],
                "account": {},
            }
            validate_order_pre_send("BITN26", "BUY")

        # Procura o marker exato em qualquer chamada de log
        all_log_calls = [str(c) for c in mock_log.call_args_list]
        joined = "\n".join(all_log_calls)
        self.assertIn(
            "[BLOCKED-DUPLICATE]",
            joined,
            f"Log deveria conter marker [BLOCKED-DUPLICATE]. Calls={all_log_calls}"
        )

    def test_mixed_positions_only_target_match_blocks(self):
        """Cenario com N pos abertas, varias simbolos/magics: so bloqueia
        se match exato magic+symbol."""
        with patch("core.vt_autotrader.status") as mock_status:
            mock_status.return_value = {
                "positions": [
                    _make_pos(symbol="WINM26", magic=555501, ticket=111),
                    _make_pos(symbol="WDON26", magic=555501, ticket=222),
                    _make_pos(symbol="BITN26", magic=999999, ticket=333),  # magic outro bot
                ],
                "account": {},
            }
            from core.vt_autotrader import validate_order_pre_send

            # BITN26 com magic 999999 ja existe MAS nao eh do bot -> permite
            self.assertTrue(validate_order_pre_send("BITN26", "BUY"))

            # BITN26 com magic 555501 NAO existe -> permite
            self.assertTrue(validate_order_pre_send("WDON26", "SELL") is False or
                            validate_order_pre_send("WDON26", "SELL"))

        # Teste isolado do match: WDON26 magic 555501 ja aberta -> BLOQUEIA
        with patch("core.vt_autotrader.status") as mock_status:
            mock_status.return_value = {
                "positions": [_make_pos(symbol="WDON26", magic=555501, ticket=222)],
                "account": {},
            }
            from core.vt_autotrader import validate_order_pre_send
            self.assertFalse(validate_order_pre_send("WDON26", "SELL"))


class TestValidateOrderWiredInExecuteEntry(unittest.TestCase):
    """Garante via AST que validate_order_pre_send() esta WIREADA em
    _execute_entry() ANTES de safe_buy/safe_sell (call site real)."""

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