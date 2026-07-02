"""
test_watchdog_excludes_excluded_trades.py
=========================================
Wave 1C.1 (2026-07-02): 28 trades com ticket=12345/99999 eram teste,
marcados [EXCLUDED_TEST_2026_07_02]. Nao sao operacao real. Bruno confirmou.

Pitfall #12 no-vt-config-write-safety: garantir que o watchdog NAO alarma
sobre esses trades como "fantasmas".

Testes:
- test_watchdog_filters_excluded_from_db_query: filtra DB query
- test_watchdog_filters_excluded_from_open_trades_query: filtra open_trades
- test_excluded_trades_count_28_in_db: pre-condicao (senao o teste e vazio)
"""
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


class TestWatchdogExcludesExcludedTrades(unittest.TestCase):
    """Wave 1C.1: watchdog NAO deve ver trades com [EXCLUDED] como fantasmas."""

    def setUp(self):
        # Garantir pre-condicao: 28 trades com ticket 12345/99999 no DB
        import sqlite3
        db_path = os.path.join(PROJECT_ROOT, "vt_trades.db")
        db = sqlite3.connect(db_path, timeout=5)
        n_test = db.execute(
            "SELECT COUNT(*) FROM trades "
            "WHERE entry_ticket IN ('12345','99999') OR exit_ticket IN ('12345','99999')"
        ).fetchone()[0]
        self.assertGreaterEqual(
            n_test, 28,
            f"Pre-condicao quebrada: esperado >=28 trades com ticket teste, achou {n_test}. "
            f"Se voce rodou este teste antes, os trades podem ter sido deletados. "
            f"Re-criar via INSERT (ver setUp do conftest)."
        )

        # Garantir que TODOS esses 28 estao marcados [EXCLUDED]
        n_excluded = db.execute(
            "SELECT COUNT(*) FROM trades "
            "WHERE (entry_ticket IN ('12345','99999') OR exit_ticket IN ('12345','99999')) "
            "AND INSTR(strategy, '[EXCLUDED') > 0"
        ).fetchone()[0]
        self.assertEqual(
            n_excluded, n_test,
            f"Trades teste ({n_test}) NAO estao todos marcados [EXCLUDED] "
            f"(so {n_excluded} estao). Rodar fix Wave 1C.1: UPDATE trades SET strategy = ... [EXCLUDED_TEST_2026_07_02]."
        )
        db.close()

    def test_watchdog_filters_excluded_from_db_query(self):
        """A query em vt_trade_watchdog.py:~L110 DEVE filtrar [EXCLUDED]."""
        import re
        src_path = os.path.join(PROJECT_ROOT, "monitoring", "vt_trade_watchdog.py")
        src = open(src_path).read()
        # Procura padrao buggy: query sem filtro EXCLUDED
        bad_pattern = re.compile(
            r'FROM trades WHERE\s*\(exit_time IS NULL OR exit_time = .*.\)\s*"\s*\)',
            re.DOTALL,
        )
        match = bad_pattern.search(src)
        # Aceita so se o filtro EXCLUDED esta presente perto
        self.assertIsNotNone(
            match,
            "Query L110 sem filtro EXCLUDED detectado. Pitfall #12: 28 trades teste "
            "sao alarmados como fantasmas a cada 2min. Fix: adicionar "
            "'AND (strategy IS NULL OR INSTR(strategy, \\'[EXCLUDED\\') = 0)'"
        )
        # Confirmar que a regiao apos a query tem o filtro
        region = src[match.start():match.start() + 600]
        self.assertIn(
            "[EXCLUDED", region,
            "Query L110 existe mas SEM filtro [EXCLUDED] na regiao subsequente. "
            "Fix Wave 1C.1 deve adicionar 'AND (strategy IS NULL OR INSTR(strategy, "
            "'[EXCLUDED') = 0)' logo apos a clausula WHERE."
        )

    def test_watchdog_filters_excluded_from_get_db_open_trades(self):
        """A query em get_db_open_trades() (L108) DEVE filtrar [EXCLUDED]."""
        import re
        src_path = os.path.join(PROJECT_ROOT, "monitoring", "vt_trade_watchdog.py")
        src = open(src_path).read()

        # Procura funcao get_db_open_trades() e le ate a primeira "return" no escopo da funcao
        func_start = src.find("def get_db_open_trades")
        self.assertGreater(
            func_start, 0, "Funcao get_db_open_trades() nao encontrada"
        )
        # Pega os proximos 1500 chars (query inteira + return)
        body = src[func_start:func_start + 1500]
        self.assertIn(
            "[EXCLUDED", body,
            "get_db_open_trades() SEM filtro [EXCLUDED]! "
            "28 trades teste serao alarmados como fantasmas. "
            "Fix Wave 1C.1: adicionar 'AND (strategy IS NULL OR INSTR(strategy, "
            "'[EXCLUDED') = 0)' na query."
        )

    def test_watchdog_filters_excluded_from_diff_query(self):
        """A query open_trades (L227) usada em diff_db_vs_mt5() DEVE filtrar [EXCLUDED]."""
        import re
        src_path = os.path.join(PROJECT_ROOT, "monitoring", "vt_trade_watchdog.py")
        src = open(src_path).read()

        # Procura trecho de L220-230 (open_trades = conn.execute ...)
        # Match ate o .fetchall() no fim (pode ter ) aninhados no comment, cuidado)
        pattern = re.compile(
            r'open_trades\s*=\s*conn\.execute\([\s\S]+?\.fetchall',
        )
        matches = list(pattern.finditer(src))
        self.assertGreaterEqual(
            len(matches), 1,
            "open_trades = conn.execute(...) nao encontrada. "
            "Verificar se L227 mudou."
        )
        for m in matches:
            # Pega janela de 500 chars apos o match (toda a query)
            region = src[m.start():m.start() + 500]
            self.assertIn(
                "[EXCLUDED", region,
                f"Query open_trades (offset {m.start()}) SEM filtro [EXCLUDED]:\n{region}\n"
                "Fix Wave 1C.1: adicionar 'AND (strategy IS NULL OR INSTR(strategy, "
                "'[EXCLUDED') = 0)'"
            )

    def test_excluded_trades_count_28_in_db(self):
        """Pre-condicao: 28 trades com ticket teste devem existir E estar marcados."""
        import sqlite3
        db_path = os.path.join(PROJECT_ROOT, "vt_trades.db")
        db = sqlite3.connect(db_path, timeout=5)
        n = db.execute(
            "SELECT COUNT(*) FROM trades "
            "WHERE (entry_ticket IN ('12345','99999') OR exit_ticket IN ('12345','99999')) "
            "AND INSTR(strategy, '[EXCLUDED') > 0"
        ).fetchone()[0]
        self.assertEqual(
            n, 28,
            f"Esperado 28 trades teste marcados [EXCLUDED], achou {n}. "
            f"Se mudou, atualizar teste (mas antes verificar com Bruno)."
        )
        db.close()

    def test_excluded_trades_filtered_from_live_count(self):
        """Stats live (sem EXCLUDED) nao devem incluir ticket 12345/99999."""
        import sqlite3
        db_path = os.path.join(PROJECT_ROOT, "vt_trades.db")
        db = sqlite3.connect(db_path, timeout=5)
        n_live_test = db.execute(
            "SELECT COUNT(*) FROM trades "
            "WHERE entry_ticket IN ('12345','99999') "
            "AND (strategy IS NULL OR INSTR(strategy, '[EXCLUDED') = 0)"
        ).fetchone()[0]
        self.assertEqual(
            n_live_test, 0,
            f"Ticket 12345/99999 contando em LIVE: {n_live_test}. "
            f"Fix Wave 1C.1 falhou - filtro EXCLUDED nao esta sendo aplicado."
        )
        db.close()


if __name__ == "__main__":
    unittest.main()