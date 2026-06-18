"""
test_copilot_intraday.py
========================
TDD: o relatorio do Copilot (vt_copilot.py) deve ser INTRADAY, nao historico.

Problema (jun/2026): generate_report() chamava check_performance() com janela
de 5 dias, gerando tabela com 22 linhas de SÍMBOLO+TF acumulado, incluindo
contratos vencidos (WDOU26, WINM26). Poluía o canal e nao respondia a pergunta
do Bruno: "como esta o dia ate agora?"

Este teste:
- RED: confirma que check_intraday_stats() nao existe ainda
- GREEN: apos implementacao, valida que
  * retorna trades SOMENTE do dia atual
  * calcula PnL realizado + flutuante + max drawdown
  * gera grafico PNG
  * generate_report() NAO contem "5 dias"
"""
import os
import sys
import unittest
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class TestCheckIntradayStats(unittest.TestCase):
    """check_intraday_stats() deve contar SÓ o dia atual."""

    def setUp(self):
        # Cria DB temporário com trades de hoje e de 5 dias atrás
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp_db.close()
        self.db_path = Path(self.tmp_db.name)

        today = datetime.now().strftime("%Y-%m-%d")
        old_date = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")

        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                symbol TEXT,
                timeframe TEXT,
                entry_time TEXT,
                exit_time TEXT,
                net_pnl REAL
            )
        """)
        # Trade de HOJE (deve contar)
        conn.execute("""
            INSERT INTO trades (symbol, timeframe, entry_time, exit_time, net_pnl)
            VALUES (?, ?, ?, ?, ?)
        """, ("WINQ26", "M15", f"{today} 09:30:00", f"{today} 09:45:00", 50.0))
        # Trade de HOJE (deve contar)
        conn.execute("""
            INSERT INTO trades (symbol, timeframe, entry_time, exit_time, net_pnl)
            VALUES (?, ?, ?, ?, ?)
        """, ("DOLN26", "M5", f"{today} 10:00:00", f"{today} 10:05:00", -30.0))
        # Trade de 5 dias atrás (NAO deve contar)
        conn.execute("""
            INSERT INTO trades (symbol, timeframe, entry_time, exit_time, net_pnl)
            VALUES (?, ?, ?, ?, ?)
        """, ("WDOU26", "M5", f"{old_date} 09:30:00", f"{old_date} 09:45:00", -100.0))
        # Trade de hoje SEM exit (ainda aberto, NAO deve contar como realizado)
        conn.execute("""
            INSERT INTO trades (symbol, timeframe, entry_time, exit_time, net_pnl)
            VALUES (?, ?, ?, NULL, NULL)
        """, ("BITM26", "H1", f"{today} 09:00:00"))
        conn.commit()
        conn.close()

    def tearDown(self):
        if self.db_path.exists():
            self.db_path.unlink()

    def test_check_intraday_stats_only_counts_today(self):
        """check_intraday_stats() deve contar apenas trades de HOJE."""
        from vt_copilot import check_intraday_stats
        with patch("vt_copilot.DB_PATH", self.db_path):
            stats = check_intraday_stats()
        # 2 trades fechados hoje (50 + -30), 1 trade antigo NAO conta, 1 trade aberto NAO conta
        self.assertEqual(stats["ops"], 2,
                         f"Esperado 2 trades fechados hoje, achou {stats['ops']}")
        self.assertEqual(stats["wins"], 1)
        self.assertEqual(stats["losses"], 1)
        self.assertAlmostEqual(stats["pnl_realized"], 20.0, places=2,
                               msg=f"Esperado PnL = 50-30=20, achou {stats['pnl_realized']}")

    def test_pnl_cum_series_in_trade_order(self):
        """pnl_cum deve ser série temporal em ordem cronológica com acumulado."""
        from vt_copilot import check_intraday_stats
        with patch("vt_copilot.DB_PATH", self.db_path):
            stats = check_intraday_stats()
        # 2 pontos: 50 (depois WIN) → 20 (depois DOL)
        self.assertEqual(len(stats["pnl_cum"]), 2)
        self.assertEqual(stats["pnl_cum"][0][1], 50.0)
        self.assertEqual(stats["pnl_cum"][1][1], 20.0)

    def test_max_drawdown_calculation(self):
        """Max DD = queda maxima do peak até o fundo subsequente."""
        from vt_copilot import check_intraday_stats
        with patch("vt_copilot.DB_PATH", self.db_path):
            stats = check_intraday_stats()
        # Curva: 50 → 20, peak=50, drawdown=20-50=-30
        self.assertAlmostEqual(stats["max_drawdown"], -30.0, places=2,
                               msg=f"Esperado max DD = -30, achou {stats['max_drawdown']}")

    def test_best_and_worst_trade(self):
        """best_trade = max dos PnLs, worst_trade = min."""
        from vt_copilot import check_intraday_stats
        with patch("vt_copilot.DB_PATH", self.db_path):
            stats = check_intraday_stats()
        self.assertAlmostEqual(stats["best_trade"], 50.0, places=2)
        self.assertAlmostEqual(stats["worst_trade"], -30.0, places=2)


class TestRenderPnlChart(unittest.TestCase):
    """render_pnl_chart() deve gerar PNG válido."""

    def test_creates_png_with_data(self):
        from vt_copilot import render_pnl_chart
        today = datetime.now().strftime("%Y-%m-%d")
        pnl_cum = [
            (f"{today} 09:30:00", 50.0),
            (f"{today} 09:45:00", 20.0),
            (f"{today} 10:00:00", 80.0),
        ]
        out = render_pnl_chart(pnl_cum, today)
        self.assertTrue(out.exists(), f"PNG não foi criado em {out}")
        self.assertGreater(out.stat().st_size, 1000, "PNG muito pequeno (suspeito)")
        # Limpa
        out.unlink(missing_ok=True)

    def test_creates_png_with_empty_data(self):
        """Sem trades, deve gerar gráfico com placeholder."""
        from vt_copilot import render_pnl_chart
        today = datetime.now().strftime("%Y-%m-%d")
        out = render_pnl_chart([], today)
        self.assertTrue(out.exists())
        self.assertGreater(out.stat().st_size, 500)
        out.unlink(missing_ok=True)


class TestGenerateReportNoHistoricalLeak(unittest.TestCase):
    """generate_report() NÃO deve mencionar '5 dias' nem histórico."""

    def test_no_five_days_string_in_report(self):
        from vt_copilot import generate_report
        # Mocka tudo que generate_report() toca
        with patch("vt_copilot.check_autotrader_health",
                   return_value={"running": True, "pid": 12345, "log_fresh": True}), \
             patch("vt_copilot.check_intraday_stats",
                   return_value={"ops": 2, "wins": 1, "losses": 1,
                                 "pnl_realized": 20.0, "open_count": 0,
                                 "open_pnl": 0.0, "pnl_total": 20.0,
                                 "pnl_cum": [], "max_drawdown": -30.0,
                                 "best_trade": 50.0, "worst_trade": -30.0}):
            report = generate_report()
        # NUNCA deve ter "5 dias" (era o bug original)
        self.assertNotIn("5 dias", report,
                         f"Report ainda contém '5 dias': {report[:200]}")
        self.assertNotIn("Performance (5", report,
                         f"Report ainda tem cabeçalho histórico: {report[:200]}")
        # DEVE ter label novo "Intrade" ou similar
        self.assertTrue("Intrade" in report or "intraday" in report.lower(),
                        f"Report sem marcador de intraday: {report[:300]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
