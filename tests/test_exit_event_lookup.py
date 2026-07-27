"""
test_exit_event_lookup.py
=================================
Valida _lookup_exit_event_from_db() — broker-truth via EA events
(mt5_trade_events) usado no fechamento server-side (SL_SERVIDOR).

Contexto (Wave 880.J): o alerta "⚡ Fechou ..." saía com Entrada==Saída porque,
quando _truth.get_position_history() (Wine) falhava, current_price ficava stale
(== entry). O helper busca o deal de SAÍDA real no DB alimentado pelo EA
(OnTradeTransaction → CSV → watcher → SQLite), que é broker-truth local.

Por que extração AST: importar core.vt_autotrader constrói estado global, lê o
DB real e contacta o MT5 (ver AGENTS.md). O helper é auto-contido (só sqlite3/
time), então extraímos o FunctionDef e executamos isolado — teste comportamental
real sem os side-effects do import. Mesmo princípio de test_exit_sl_price_recorded.
"""
import ast
import os
import sqlite3
import tempfile
import unittest

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
SRC = os.path.join(PROJECT_ROOT, "core", "vt_autotrader.py")

SCHEMA = """
CREATE TABLE mt5_trade_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER, event_time TEXT NOT NULL, trans_type TEXT NOT NULL,
    order_ticket INTEGER, deal_ticket INTEGER, symbol TEXT,
    order_type TEXT, order_state TEXT, volume REAL, price REAL,
    sl REAL, tp REAL, deal_type TEXT, deal_entry TEXT,
    deal_profit REAL, deal_commission REAL, deal_swap REAL,
    deal_price REAL, deal_volume REAL, position_ticket INTEGER, comment TEXT)
"""


def _load_helper():
    """Extrai _lookup_exit_event_from_db do fonte via AST e executa isolado."""
    with open(SRC) as f:
        src = f.read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_lookup_exit_event_from_db":
            mod = ast.Module(body=[node], type_ignores=[])
            ns = {}
            exec(compile(mod, SRC, "exec"), ns)
            return ns["_lookup_exit_event_from_db"]
    raise AssertionError("_lookup_exit_event_from_db não encontrado em vt_autotrader.py")


HELPER = _load_helper()

# Tickets reais observados: entry_ticket 2488320248 (>= 2^31) vira -1806647048
# no EA (impresso com %d int32). position_ticket -1806775834.
ENTRY_TICKET = 2488320248
ENTRY_TICKET_I32 = -1806647048
POSITION_TICKET = -1806775834


def _insert(conn, *, trans_type, deal_entry, order_ticket, deal_ticket,
            deal_type, position_ticket, deal_price=0.0, deal_profit=0.0,
            deal_commission=0.0, deal_swap=0.0, event_time="2026-07-27T10:41:22"):
    conn.execute(
        "INSERT INTO mt5_trade_events (event_time, trans_type, order_ticket, "
        "deal_ticket, symbol, deal_type, deal_entry, deal_profit, deal_commission, "
        "deal_swap, deal_price, position_ticket) "
        "VALUES (?,?,?,?, 'WINQ26', ?,?,?,?,?,?,?)",
        (event_time, trans_type, order_ticket, deal_ticket, deal_type, deal_entry,
         deal_profit, deal_commission, deal_swap, deal_price, position_ticket))


class TestExitEventLookup(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.db)
        self.conn.execute(SCHEMA)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db)

    def _seed_entry_deal(self):
        # Deal de abertura (IN) — ancora o position_ticket via order_ticket
        _insert(self.conn, trans_type="DEAL_ADD", deal_entry="IN",
                order_ticket=ENTRY_TICKET_I32, deal_ticket=1811220711,
                deal_type="BUY", position_ticket=POSITION_TICKET,
                deal_price=175895.0)

    def test_finds_exit_deal_broker_truth(self):
        self._seed_entry_deal()
        _insert(self.conn, trans_type="DEAL_ADD", deal_entry="OUT",
                order_ticket=-1806631228, deal_ticket=1811231994,
                deal_type="SELL", position_ticket=POSITION_TICKET,
                deal_price=175720.0, deal_profit=-104.20, deal_commission=-1.20,
                event_time="2026-07-27T10:43:00")
        self.conn.commit()

        res = HELPER("WINQ26", "BUY", ENTRY_TICKET, db_path=self.db, retries=0)
        self.assertIsNotNone(res, "devia achar o deal de saída")
        self.assertEqual(res["price"], 175720.0)
        self.assertEqual(res["profit"], -104.20)
        self.assertEqual(res["commission"], -1.20)
        self.assertEqual(res["swap"], 0.0)
        self.assertEqual(res["ticket"], 1811231994)

    def test_int32_ticket_conversion(self):
        # entry_ticket >= 2^31 deve casar com order_ticket negativo do EA
        self._seed_entry_deal()
        _insert(self.conn, trans_type="DEAL_ADD", deal_entry="OUT",
                order_ticket=-1, deal_ticket=42, deal_type="SELL",
                position_ticket=POSITION_TICKET, deal_price=175000.0,
                deal_profit=-50.0)
        self.conn.commit()
        res = HELPER("WINQ26", "BUY", ENTRY_TICKET, db_path=self.db, retries=0)
        self.assertIsNotNone(res)
        self.assertEqual(res["profit"], -50.0)

    def test_dedup_live_plus_backfill(self):
        # Mesmo deal (deal_ticket igual) capturao ao vivo + backfill: 1 resultado
        self._seed_entry_deal()
        for _ in range(2):
            _insert(self.conn, trans_type="DEAL_ADD", deal_entry="OUT",
                    order_ticket=-1806631228, deal_ticket=1811231994,
                    deal_type="SELL", position_ticket=POSITION_TICKET,
                    deal_price=175720.0, deal_profit=-104.20)
        self.conn.commit()
        res = HELPER("WINQ26", "BUY", ENTRY_TICKET, db_path=self.db, retries=0)
        self.assertIsNotNone(res)
        self.assertEqual(res["profit"], -104.20)  # valor de 1 deal, não somado

    def test_opposite_direction_filter(self):
        # Para posição BUY, só casa OUT do tipo SELL (fechamento). Um OUT do
        # tipo BUY (re-abertura/outra posição) não pode ser o fechamento.
        self._seed_entry_deal()
        _insert(self.conn, trans_type="DEAL_ADD", deal_entry="OUT",
                order_ticket=-999, deal_ticket=111, deal_type="BUY",
                position_ticket=POSITION_TICKET, deal_price=176000.0,
                deal_profit=999.0, event_time="2026-07-27T10:44:00")
        _insert(self.conn, trans_type="DEAL_ADD", deal_entry="OUT",
                order_ticket=-888, deal_ticket=222, deal_type="SELL",
                position_ticket=POSITION_TICKET, deal_price=175720.0,
                deal_profit=-104.20, event_time="2026-07-27T10:43:00")
        self.conn.commit()
        res = HELPER("WINQ26", "BUY", ENTRY_TICKET, db_path=self.db, retries=0)
        self.assertIsNotNone(res)
        self.assertEqual(res["ticket"], 222, "deve pegar o SELL OUT (fechamento)")
        self.assertEqual(res["profit"], -104.20)

    def test_sell_position_looks_for_buy_out(self):
        # Posição SELL fecha com deal OUT do tipo BUY
        _insert(self.conn, trans_type="DEAL_ADD", deal_entry="IN",
                order_ticket=ENTRY_TICKET_I32, deal_ticket=1, deal_type="SELL",
                position_ticket=POSITION_TICKET, deal_price=5750.0)
        _insert(self.conn, trans_type="DEAL_ADD", deal_entry="OUT",
                order_ticket=-2, deal_ticket=2, deal_type="BUY",
                position_ticket=POSITION_TICKET, deal_price=5740.0,
                deal_profit=10.0)
        self.conn.commit()
        res = HELPER("WINQ26", "SELL", ENTRY_TICKET, db_path=self.db, retries=0)
        self.assertIsNotNone(res)
        self.assertEqual(res["profit"], 10.0)

    def test_no_exit_deal_returns_none(self):
        self._seed_entry_deal()  # só entrada, sem saída
        self.conn.commit()
        res = HELPER("WINQ26", "BUY", ENTRY_TICKET, db_path=self.db,
                     retries=0, retry_sleep=0)
        self.assertIsNone(res)

    def test_no_entry_deal_returns_none(self):
        # Sem deal IN p/ esse order_ticket → não descobre position_ticket
        _insert(self.conn, trans_type="DEAL_ADD", deal_entry="OUT",
                order_ticket=-1, deal_ticket=2, deal_type="SELL",
                position_ticket=POSITION_TICKET, deal_price=175720.0,
                deal_profit=-104.20)
        self.conn.commit()
        res = HELPER("WINQ26", "BUY", ENTRY_TICKET, db_path=self.db,
                     retries=0, retry_sleep=0)
        self.assertIsNone(res)

    def test_invalid_entry_ticket_returns_none(self):
        self.assertIsNone(HELPER("WINQ26", "BUY", None, db_path=self.db, retries=0))
        self.assertIsNone(HELPER("WINQ26", "BUY", "abc", db_path=self.db, retries=0))

    def test_missing_db_returns_none(self):
        # DB inexistente não pode levantar — retorna None (fallback seguro)
        res = HELPER("WINQ26", "BUY", ENTRY_TICKET,
                     db_path="/tmp/nao_existe_vt_test.db", retries=0, retry_sleep=0)
        self.assertIsNone(res)


class TestSlServidorIntegration(unittest.TestCase):
    """Inspeção de fonte: o path SL_SERVIDOR usa o helper e o preço corrigido."""

    @classmethod
    def setUpClass(cls):
        with open(SRC) as f:
            cls.src = f.read()

    def test_notification_uses_corrected_exit_price(self):
        # A notificação de fechamento server-side deve mostrar _exit_price_for_db
        # (preço corrigido), não o current_price stale (que causava Entrada==Saída).
        self.assertIn(
            'f"• Entrada: {entry_price:.2f} → Saída: {_exit_price_for_db:.2f}\\n"',
            self.src,
            "Notificação SL_SERVIDOR deve usar _exit_price_for_db na Saída")

    def test_sl_servidor_path_calls_helper(self):
        self.assertIn(
            "_lookup_exit_event_from_db(symbol, direction, pos.get(\"entry_ticket\"))",
            self.src,
            "Path SL_SERVIDOR deve consultar o EA events via _lookup_exit_event_from_db")


if __name__ == "__main__":
    unittest.main()
