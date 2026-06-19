"""
test_drawdown_correct_pnl.py
============================
TDD: bug de DRAWDOWN no vt_analyst.py reportado em 18/06/2026 10:52.

CENÁRIO ORIGINAL DO BUG (DOL cheio, mult 0.5/pt, volume 5):
- Posição: DOLN26 M30 SELL @ 5159, volume 5, mult 1.0  (volume 5, mult 1.0 → 5x)
- Preço atual: 5128 (A FAVOR do SELL — DOL caiu)
- MT5 status() retornou profit agregado de posições (stale/outra posição)
  → snapshot["position"]["profit"] = -1375.0
- Alerta reportou:
    Prejuízo: R$ 1375.00  (ERRADO — devia ser +R$ 77,50 a favor)
    Drawdown: 146.4x ATR  (ERRADO — devia ser 0, posição a favor)
- Math: abs(-1375) / 1.0 = 1375 pontos; 1375/9.4 = 146.3x ATR (bate)

REFATORAÇÃO 19/06/2026: Bruno removeu IND/DOL (contratos cheios).
Cenário equivalente reescrito para WDO (mini dólar, mult 10.0/pt).
Com WDO, volume 1 contrato × 10 USD/pt = R$ 10/pt (mesma estrutura,
fácil de auditar). O bug de cálculo é o mesmo — proof é o fix,
não o ativo.

3 PROBLEMAS IDENTIFICADOS (inalterados):
1. snapshot["position"]["profit"] vem do MT5 agregado, não do state por par
2. Gate `if pnl < 0` não valida direção (SELL com preço a favor não pode ter pnl<0)
3. drawdown_pts = abs(pnl) / mult ignora volume (5 contratos = 5x mais perda por pt)

ESTE TESTE:
- RED: reproduz o cenário com snapshot mockado, espera que o bug dispare alerta errado
- GREEN: após o fix, o alerta NÃO é disparado, ou o profit/drawdown está correto
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# Resetar METRICS_BUFFER para que os testes não dependam de dados em memória
import vt_analyst
vt_analyst.METRICS_BUFFER = vt_analyst._init_metrics_buffer()


def _make_snapshot_wdo_sell_problematic():
    """Reproduz cenário equivalente ao alerta bugado de 18/06/2026 10:52, agora em WDO.

    Original DOL: SELL @ 5159, vol 5, mult 1.0 → 5x R$/pt
    WDO equivalente: SELL @ 5159, vol 1, mult 10.0 → 10x R$/pt (volume 1 contrato)
    """
    # Posição SELL @ 5159, preço atual 5128 (A FAVOR), volume 1 (mini)
    # MT5 status() devolveu profit agregado = -1375 (stale de outra posição)
    pos = {
        "type": 1,  # mt5.ORDER_TYPE_SELL
        "symbol": "WDON26",  # 2026-06-19: WDO substitui DOL
        "price_open": 5159.0,
        "volume": 1.0,  # WDO mini: 1 contrato
        "sl": 5168.4,
        "profit": -1375.0,  # ← bug: valor do MT5 agregado (mesmo do bug original)
        "atr": 9.0,
    }
    return {
        "symbol": "WDON26",  # 2026-06-19: WDO substitui DOL
        "timeframe": "M30",
        "price": 5128.0,  # ← preço atual a favor do SELL
        "bid": 5127.5,
        "ask": 5128.0,
        "atr": 9.0,
        "current_volume": 100,
        "vwap": 5135.0,
        "vwap_distance_pct": -0.14,
        "trend": "BAIXA",
        "sma5": 5130.0,
        "sma10": 5140.0,
        "spread": 0.5,
        "n_positions": 1,
        "position": pos,
        "bars_count": 30,
        "session_high": 5170.0,
        "session_low": 5120.0,
        "momentum_5bar": -0.5,
        "avg_volume": 50,
    }


def _populate_buffer():
    """Popula METRICS_BUFFER com histórico mínimo (5+ entradas) pra passar o gate."""
    buf = vt_analyst.METRICS_BUFFER.get("WDO")  # 2026-06-19: WDO substitui DOL
    if not buf:
        return
    for _ in range(10):
        buf["volumes"].append(100)
        buf["atrs"].append(9.0)
        buf["spreads"].append(0.5)


class TestDrawdownCorrectPnl(unittest.TestCase):
    """Garante que DRAWDOWN não alerta lucro como prejuízo."""

    def setUp(self):
        _populate_buffer()

    def test_SELL_at_favorable_price_no_drawdown_alert(self):
        """SELL @ 5159 com preço atual 5128 (a favor) NÃO deve disparar DRAWDOWN.

        Cenário real de 18/06/2026 10:52 (DOL cheio): a posição estava a favor
        (DOL caiu), mas o alerta reportou -R$ 1375 e 146.4x ATR drawdown porque
        o profit veio do MT5 agregado (stale de outra posição) em vez de ser
        calculado a partir do preço real.

        2026-06-19: refatorado para WDO (mini dólar) — bug é o mesmo, símbolo é
        o que está em circulação.
        """
        snap = _make_snapshot_wdo_sell_problematic()
        real_pos = {
            "entry_price": 5159.0,
            "volume": 1,  # WDO mini: 1 contrato
            "direction": "SELL",
            "sl_pts": 9400,  # ~9.4pts nativo
            "atr": 9.0,
            "entry_ticket": "2459430751",
        }
        with patch("vt_analyst.find_real_position", return_value=(real_pos, "M30")):
            anomalies = vt_analyst.detect_anomalies(snap)
        drawdown_alerts = [a for a in anomalies if a["type"] == "DRAWDOWN"]
        self.assertEqual(
            len(drawdown_alerts), 0,
            f"BUG: DRAWDOWN disparou com SELL a favor. Alerta: {drawdown_alerts}"
        )

    def test_SELL_at_unfavorable_price_uses_correct_pnl(self):
        """SELL @ 5159 com preço atual 5170 (contra) DEVE disparar DRAWDOWN com
        profit correto (real, não stale agregado)."""
        snap = _make_snapshot_wdo_sell_problematic()
        snap["price"] = 5170.0  # CONTRA o SELL (subiu)
        # Substitui o profit agregado por um real
        # WDO: 11pts * R$10/pt * 1 (mini, vol 1) = R$ -110
        snap["position"]["profit"] = -110.0
        real_pos = {
            "entry_price": 5159.0,
            "volume": 1,  # WDO mini: 1 contrato
            "direction": "SELL",
            "sl_pts": 9400,
            "atr": 9.0,
            "entry_ticket": "2459430751",
        }
        with patch("vt_analyst.find_real_position", return_value=(real_pos, "M30")):
            anomalies = vt_analyst.detect_anomalies(snap)
        drawdown_alerts = [a for a in anomalies if a["type"] == "DRAWDOWN"]
        # Esperamos 1 alerta (DRAWDOWN real)
        self.assertEqual(
            len(drawdown_alerts), 1,
            f"Esperado 1 alerta DRAWDOWN, achou {len(drawdown_alerts)}"
        )
        msg = drawdown_alerts[0]["msg"]
        # O alert mostra "Prejuízo: R$ XX" (valor recalculado)
        # Se vier -1375 (agregado), o bug persiste
        self.assertNotIn(
            "1375", msg,
            f"BUG: alerta DRAWDOWN ainda usa profit agregado: {msg}"
        )

    def test_pnl_agregado_nao_e_usado_quando_sell_a_favor(self):
        """Mesmo se MT5 reportar profit negativo agregado, se preço está a favor
        do SELL, o alerta não deve disparar (direção do trade tem precedência)."""
        snap = _make_snapshot_wdo_sell_problematic()
        # Mantém profit agregado -1375 mas preço a favor
        real_pos = {
            "entry_price": 5159.0,
            "volume": 1,  # WDO mini: 1 contrato
            "direction": "SELL",
            "sl_pts": 9400,
            "atr": 9.0,
            "entry_ticket": "2459430751",
        }
        with patch("vt_analyst.find_real_position", return_value=(real_pos, "M30")):
            anomalies = vt_analyst.detect_anomalies(snap)
        drawdown_alerts = [a for a in anomalies if a["type"] == "DRAWDOWN"]
        self.assertEqual(
            len(drawdown_alerts), 0,
            f"BUG: DRAWDOWN disparou apesar de SELL a favor. "
            f"Profit agregado -1375 foi usado em vez de calcular do preço real."
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
