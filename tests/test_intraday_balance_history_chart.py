"""
test_intraday_balance_history_chart.py
======================================
TDD para o bug 09/07/2026: gráfico intraday aparece VAZIO quando:

- MT5 demo não persiste history deals (sempre vazio)
- DB trades só tem registros com exit_reason='GHOST' (excluídos por filtro
  intencional Bruno 02/07)

Resultado: pnl_series=[] → render_pnl_chart() cai no placeholder.

Wave N+1B (Bruno 09/07):
- Adicionar /tmp/vt_intraday_balance_history.json que acumula snapshots
  (timestamp, balance) a cada chamada do copilot
- check_intraday_stats() plota série temporal baseada no balance_history
  quando DB+MT5 history estão vazios
- Texto continua usando balance_delta (sem regressão)

RED: este teste falha até vt_balance_history existir e render_pnl_chart
usar a série de balance quando pnl_cum vem vazio.
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "monitoring"))


class TestBalanceHistoryAppendAndRead(unittest.TestCase):
    """vt_balance_history.append_snapshot() e read_history() devem funcionar."""

    def setUp(self):
        # Arquivo temporário isolado por teste
        self.tmpdir = tempfile.mkdtemp()
        self.path = Path(self.tmpdir) / "vt_intraday_balance_history.json"

    def test_append_creates_file_with_first_snapshot(self):
        from vt_balance_history import append_snapshot, read_history

        append_snapshot(self.path, balance=1002230.57, pnl_delta=0.0,
                       source="MT5_HISTORY")
        history = read_history(self.path)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["balance"], 1002230.57)
        self.assertIn("ts", history[0])

    def test_append_multiple_keeps_chronological_order(self):
        from vt_balance_history import append_snapshot, read_history

        append_snapshot(self.path, balance=1002230.57, pnl_delta=0.0)
        append_snapshot(self.path, balance=1002250.00, pnl_delta=19.43)
        append_snapshot(self.path, balance=1002290.00, pnl_delta=59.43)

        history = read_history(self.path)
        self.assertEqual(len(history), 3)
        balances = [h["balance"] for h in history]
        self.assertEqual(balances, [1002230.57, 1002250.00, 1002290.00])

    def test_dedup_repeated_balance_within_60_sec(self):
        """Se MT5 status não mudou entre ticks, não polui o histórico."""
        from vt_balance_history import append_snapshot, read_history

        for _ in range(3):
            append_snapshot(self.path, balance=1002230.57, pnl_delta=0.0)

        history = read_history(self.path)
        self.assertEqual(len(history), 1)

    def test_keep_only_today_snapshots(self):
        """Snapshots de dias anteriores devem ser descartados na leitura."""
        from vt_balance_history import append_snapshot, read_history

        # Simula arquivo persistido com dados antigos
        old_ts = (datetime.now() - timedelta(days=2)).isoformat()
        self.path.write_text(json.dumps([
            {"ts": old_ts, "balance": 999999.99, "pnl_delta": 0.0},
        ]))
        append_snapshot(self.path, balance=1002230.57, pnl_delta=0.0)

        history = read_history(self.path)
        # Só o snapshot novo sobrevive
        self.assertEqual(len(history), 1)
        self.assertGreater(history[0]["balance"], 1000000)


class TestRenderPnlChartUsesBalanceHistoryWhenPnlEmpty(unittest.TestCase):
    """render_pnl_chart deve plotar a evolução do saldo quando pnl_cum vazio."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = Path(self.tmpdir) / "vt_intraday_balance_history.json"
        # Snapshot hoje
        from vt_balance_history import append_snapshot
        append_snapshot(self.path, balance=1002230.57, pnl_delta=0.0)
        append_snapshot(self.path, balance=1002250.00, pnl_delta=19.43)
        append_snapshot(self.path, balance=1002290.00, pnl_delta=59.43)

    def test_pnl_cum_built_from_balance_history(self):
        from monitoring.vt_copilot import build_pnl_series_from_balance_history
        series = build_pnl_series_from_balance_history(self.path)
        # 3 pontos: (ts, pnl_delta acumulado)
        self.assertEqual(len(series), 3)
        deltas = [v for _, v in series]
        self.assertEqual(deltas, [0.0, 19.43, 59.43])

    def test_render_chart_not_empty_when_pnl_cum_empty_but_balance_history_exists(
        self,
    ):
        """Caso de bug: pnl_cum vazio mas histórico do saldo tem dados.

        Verifica que o PNG gerado contém a série do balance history (não
        fica no placeholder) usando contagem de bytes como proxy: placeholder
        texto é bem menor que gráfico com linha real.
        """
        from monitoring.vt_copilot import render_pnl_chart
        today = datetime.now().strftime("%Y-%m-%d")
        out_path = render_pnl_chart(
            [], today, balance_history_path=str(self.path)
        )
        self.assertTrue(out_path.exists())
        size = out_path.stat().st_size
        # Gráfico com linha + fill + grid = ~20-40KB;
        # placeholder só com texto = ~3-5KB. Threshold conservador 8KB.
        self.assertGreater(
            size, 8000,
            f"PNG muito pequeno ({size}B) — provavelmente placeholder",
        )


class TestCheckIntradayStatsPopulatesPnlCumFromBalance(unittest.TestCase):
    """check_intraday_stats() deve popular pnl_cum via balance history quando DB vazio."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.bh_path = Path(self.tmpdir) / "bh.json"
        from vt_balance_history import append_snapshot
        append_snapshot(self.bh_path, balance=1002230.57, pnl_delta=0.0)
        append_snapshot(self.bh_path, balance=1002290.00, pnl_delta=59.43)


if __name__ == "__main__":
    import unittest
    unittest.main()
