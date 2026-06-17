#!/usr/bin/env python3
"""TDD — alerts de DRAWDOWN devem usar metadados da POSIÇÃO ESPECÍFICA (tf/volume),
não do symbol agregado nem do snapshot do TF da iteração.

Bugs reproduzidos (ocorreram em 17/06 16:11 com INDM26 M30 ticket 2459077217):

1. TF errado no alert:
   - Posição aberta em M30
   - Alert chegou como M5 (usou o tf da iteração do snapshot)

2. Volume errado no alert:
   - Posição com volume 1
   - Alert mostrou Vol: 5.0 (agregado de 5 ordens abertas do mesmo symbol no MT5)

3. SL: 0.0x ATR na entrada (em msg_parts do entry signal):
   - O cálculo "SL: 0.0x ATR" aparece em outros alerts
   - Atr_ratio = drawdown_pts / atr, mas se atr é 0 (snapshot do TF errado),
     o ratio vira 0.0x — formato enganoso

O fix: o detect_anomalies() deve carregar a posição via state.positions (que tem
chave f"{symbol}_{tf}"), não via status()["positions"] (que é agregado do MT5).
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))


def _build_snapshot(symbol="INDM26", tf="M5", atr=131.0, current=171145.0, **overrides):
    """Snapshot mínimo para testar detect_anomalies()."""
    snap = {
        "time": "16:11:30",
        "symbol": symbol,
        "timeframe": tf,
        "price": current,
        "bid": current - 0.5,
        "ask": current + 0.5,
        "spread": 1.0,
        "vwap": 171000.0,
        "vwap_distance_pct": 0.1,
        "atr": atr,
        "avg_volume": 1000,
        "current_volume": 5000,
        "session_high": 171500.0,
        "session_low": 170500.0,
        "momentum_5bar": 0.5,
        "sma5": current,
        "sma10": current - 50,
        "trend": "ALTA",
        "n_positions": 1,
        "position": {
            # Posição "agregada" do MT5 status (NÃO tem TF, tem volume somado)
            "type": "BUY",
            "symbol": symbol,
            "volume": 5.0,  # ← AGREGADO de 5 ordens abertas
            "price_open": 170620.0,
            "sl": 170420.0,
            "profit": -200.0,  # PnL negativo para disparar DRAWDOWN
            "ticket": 2459077217,
        },
    }
    snap.update(overrides)
    return snap


def _build_state_positions(positions_dict):
    """Mock do state.positions com chaves f'{symbol}_{tf}'."""
    return positions_dict


class TestAnalystPositionMetadata(unittest.TestCase):
    """Bug fix: DRAWDOWN/VOLATILITY alerts devem usar dados da posição ESPECÍFICA."""

    def setUp(self):
        import vt_analyst
        self.analyst = vt_analyst
        # Inicializa METRICS_BUFFER para "IND" (root de "INDM26")
        from collections import deque
        self.analyst.METRICS_BUFFER["IND"] = {
            "volumes": deque([100, 200, 300, 400, 500, 600], maxlen=40),
            "atrs": deque([100, 110, 120, 130, 131, 132], maxlen=40),
            "spreads": deque([1, 1, 1, 1, 1, 1], maxlen=40),
        }

    def _patch_fetch_snapshot(self, snapshot):
        """Substitui fetch_snapshot para retornar nosso snapshot fixo."""
        return patch.object(self.analyst, "fetch_snapshot", return_value=snapshot)

    def _patch_state(self, positions):
        """Substitui o state em /tmp/vt_autotrader_state.json com positions customizadas."""
        state = {
            "positions": positions,
            "daily_pnl": 1153.0,
            "consecutive_losses": {"INDM26": 1},
            "max_consecutive_losses": 999,
            "halt_until": {},
        }
        # Patch both: the state read inside detect_anomalies
        # AND the state in /tmp/vt_autotrader_state.json (some fns read from disk)
        import tempfile
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(state, tmp)
        tmp.close()
        return patch("builtins.open", create=True) if False else None

    def test_drawdown_alert_uses_real_tf_not_iteration_tf(self):
        """Bug 1: TF no alert deve ser o TF da POSIÇÃO (M30), não o TF do snapshot (M5)."""
        # Posição REAL aberta em M30 (state.positions)
        real_state_positions = {
            "INDM26_M30": {
                "direction": "BUY",
                "entry_price": 170620.0,
                "entry_ticket": 2459077217,
                "tf": "M30",
                "volume": 1,
                "sl_pts": 200,
                "atr": 131,
            }
        }
        # Snapshot gerado na iteração M5 (TF da iteração, não da posição)
        snapshot = _build_snapshot(symbol="INDM26", tf="M5", atr=131.0, current=171145.0)

        with patch.object(self.analyst, "fetch_snapshot", return_value=snapshot), \
             patch.object(self.analyst, "load_state_from_disk", return_value={
                 "positions": real_state_positions,
                 "daily_pnl": 1153.0,
                 "consecutive_losses": {"INDM26": 1},
                 "halt_until": {},
             }):
            anomalies = self.analyst.detect_anomalies(snapshot)

        # Encontrar o alert de DRAWDOWN
        drawdown = [a for a in anomalies if a["type"] == "DRAWDOWN"]
        self.assertEqual(len(drawdown), 1, f"Esperava 1 DRAWDOWN, achei {len(drawdown)}: {anomalies}")
        # ASSERT RED: TF no alert deve ser M30, não M5
        self.assertEqual(drawdown[0]["tf"], "M30",
                         f"Bug: TF do alert está {drawdown[0]['tf']}, deveria ser M30")
        # ASSERT RED: o msg também deve mencionar M30
        self.assertIn("INDM26 M30", drawdown[0]["msg"],
                      f"Bug: msg não contém 'INDM26 M30': {drawdown[0]['msg']}")

    def test_drawdown_alert_uses_real_volume_not_aggregated(self):
        """Bug 2: Volume no alert deve ser 1 (volume da trade específica), não 5.0 (agregado MT5)."""
        real_state_positions = {
            "INDM26_M30": {
                "direction": "BUY",
                "entry_price": 170620.0,
                "entry_ticket": 2459077217,
                "tf": "M30",
                "volume": 1,  # volume real
                "sl_pts": 200,
                "atr": 131,
            }
        }
        snapshot = _build_snapshot(symbol="INDM26", tf="M5", current=171145.0)
        # O snapshot tem pos.volume=5.0 (agregado MT5) — isso é o bug

        with patch.object(self.analyst, "fetch_snapshot", return_value=snapshot), \
             patch.object(self.analyst, "load_state_from_disk", return_value={
                 "positions": real_state_positions,
                 "daily_pnl": 1153.0,
                 "consecutive_losses": {"INDM26": 1},
                 "halt_until": {},
             }):
            anomalies = self.analyst.detect_anomalies(snapshot)

        drawdown = [a for a in anomalies if a["type"] == "DRAWDOWN"]
        self.assertEqual(len(drawdown), 1)
        # ASSERT RED: Volume no alert deve ser 1.0, não 5.0
        # Aceita "Vol: 1" ou "Vol: 1.0" (formato Python)
        self.assertRegex(drawdown[0]["msg"], r"Vol:\s*1(\.0)?\b",
                         f"Bug: volume no alert está errado. Msg: {drawdown[0]['msg']}")
        self.assertNotIn("Vol: 5.0", drawdown[0]["msg"],
                         f"Bug: alert mostra volume agregado 5.0 em vez de 1.0")

    def test_drawdown_atr_ratio_not_zero_when_atr_positive(self):
        """Bug 3: Quando ATR do state é positivo, ratio não pode ser 0.0x."""
        real_state_positions = {
            "INDM26_M30": {
                "direction": "BUY",
                "entry_price": 170620.0,
                "entry_ticket": 2459077217,
                "tf": "M30",
                "volume": 1,
                "sl_pts": 200,
                "atr": 131,  # ATR real do M30
            }
        }
        # Snapshot com ATR baixo (cálculo em M5 pode dar diferente)
        snapshot = _build_snapshot(symbol="INDM26", tf="M5", atr=10.0, current=171145.0)

        with patch.object(self.analyst, "fetch_snapshot", return_value=snapshot), \
             patch.object(self.analyst, "load_state_from_disk", return_value={
                 "positions": real_state_positions,
                 "daily_pnl": 1153.0,
                 "consecutive_losses": {"INDM26": 1},
                 "halt_until": {},
             }):
            anomalies = self.analyst.detect_anomalies(snapshot)

        drawdown = [a for a in anomalies if a["type"] == "DRAWDOWN"]
        self.assertEqual(len(drawdown), 1)
        # ASSERT: o ratio do drawdown deve usar o ATR da posição real (131)
        # 200 / 131 ≈ 1.53x — não 0.0x
        import re
        m = re.search(r"Drawdown: ([\d.]+)x ATR", drawdown[0]["msg"])
        self.assertIsNotNone(m, f"Não achei 'Drawdown: Xx ATR' no msg: {drawdown[0]['msg']}")
        ratio = float(m.group(1))
        self.assertGreater(ratio, 0.5,
                           f"Bug: drawdown ratio está {ratio}x, deveria ser >0.5x (ATR real {131})")

    def test_falls_back_to_snapshot_when_state_empty(self):
        """Se state.positions está vazio, mantém comportamento atual (usa snapshot)."""
        snapshot = _build_snapshot(symbol="INDM26", tf="M5", current=171145.0)

        with patch.object(self.analyst, "fetch_snapshot", return_value=snapshot), \
             patch.object(self.analyst, "load_state_from_disk", return_value={
                 "positions": {},  # state vazio
                 "daily_pnl": 1153.0,
                 "consecutive_losses": {},
                 "halt_until": {},
             }):
            anomalies = self.analyst.detect_anomalies(snapshot)

        drawdown = [a for a in anomalies if a["type"] == "DRAWDOWN"]
        # Quando state vazio, fallback para snapshot
        self.assertEqual(len(drawdown), 1)
        # Usa o tf do snapshot (M5) como fallback
        self.assertEqual(drawdown[0]["tf"], "M5")


if __name__ == "__main__":
    unittest.main(verbosity=2)
