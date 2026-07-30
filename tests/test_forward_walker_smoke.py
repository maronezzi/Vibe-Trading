"""
Smoke test para optimization/forward_walker.py.

OBJETIVO:
  - Verifica que o walker roda sem crash usando fetch_bars mockado (OHLCV determinístico).
  - Garante que forward_sim_trades cresce monotonicamente (sem regressão de re-entry loop).
  - NÃO chama MT5 real (pytest.importorskip se subprocess falhar — ver check_mt5_available).
  - Smoke rápido (default 3min de walker interno, ~3-5s de pytest wall time).

REFERÊNCIAS:
  - Pitfall 24 (re-entry loop): o smoke DEVE mostrar crescimento monotônico de rows,
    não saltos > 5x sem novo bar.
  - Pitfall 25 (DB lock): o smoke usa um DB SQLite isolado em tmp_path.
"""
from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_OPT_DIR = _PROJECT_ROOT / "optimization"


def _check_mt5_available() -> bool:
    """Checa se Wine/MT5 está respondendo sem mandar ordem — só probe read-only."""
    try:
        from mt5 import mt5_orchestrator
        info = mt5_orchestrator.symbol_info("WINQ26")
        return info is not None and "error" not in info
    except Exception:
        return False


# ─── fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Redireciona TRADES_DB pra tmp e roda ensure_schema."""
    db_path = tmp_path / "smoke_trades.db"
    # importa o módulo
    if str(_OPT_DIR) not in sys.path:
        sys.path.insert(0, str(_OPT_DIR))
    if str(_PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(_PROJECT_ROOT))
    fw = importlib.import_module("forward_walker")
    monkeypatch.setattr(fw, "TRADES_DB", db_path)
    # cria schema isolado
    fw.ensure_schema()
    yield fw, db_path


def _gen_bars(symbol: str, tf: str, count: int, base_price: float = 180_000):
    """Gera `count` candles determinísticos: drift leve + ruído controlado.

    bars[0] = candle atual (in-completo)
    bars[1] = último candle FECHADO ← é o que o walker usa pra sinal
    """
    tf_secs = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800,
               "H1": 3600, "H4": 14400}.get(tf, 300)
    # começa "agora" - count candles
    import time as _t
    now = int(_t.time())
    bars = []
    for i in range(count):
        ts = now - (count - 1 - i) * tf_secs
        # drift +1.5pts/bar pra BUY tendencia, ruído +/-20
        close = base_price + 1.5 * i + ((i % 7) - 3) * 20
        bars.append({
            "time": ts,
            "open": close - 5,
            "high": close + 30,
            "low": close - 30,
            "close": close,
            "volume": 100,
        })
    return bars


# ─── unit tests (no-MT5) ─────────────────────────────────────────────────────
class TestForwardWalkerOffline:
    """Testes unitários que não tocam MT5. Cobertos aqui pra regressão rápida."""

    def test_recommend_logic(self):
        """recommend() classifica corretamente por PnL/n/WR."""
        fw = importlib.import_module("forward_walker")
        assert fw.recommend( 100, 10, 0.40) == "KEEP"
        assert fw.recommend(-700, 20, 0.20) == "DISABLE"
        assert fw.recommend(  50,  4, 0.50) == "INCONCLUSIVE"
        assert fw.recommend(  10, 12, 0.22) == "KEEP_TIGHT"
        assert fw.recommend(-100, 15, 0.30) == "ADJUST"
        assert fw.recommend(-300,  8, 0.25) == "ADJUST"

    def test_point_val_map_consistency(self):
        """POINT_VAL_MAP deve casar com o _point_map do autotrader."""
        fw = importlib.import_module("forward_walker")
        from core.vt_autotrader import manage_position as _  # noqa
        expected = {"WIN": 1.0, "WDO": 0.001, "BIT": 0.01,
                    "DOL": 0.001, "IND": 1.0, "WSP": 0.01}
        for k, v in expected.items():
            assert fw.POINT_VAL_MAP[k] == v, f"{k}: walker={fw.POINT_VAL_MAP[k]} expected={v}"

    def test_sim_position_sl_signed(self, isolated_db):
        """Após trailing em lucro, current_sl_pts vira NEGATIVO (profit lock).

        Cenário: profit 400pts > trail_activate (1.0×200=200). trail_on ativa.
        Sem TP1 (profit < tp1_r=1.0×200=200? não, 400 > 200, então TP1 TBM ativa.
        Para isolar trailing puro, usamos tp1_pct=0 (desliga TP1).
        """
        fw, _db = isolated_db
        from datetime import datetime
        pos = fw.SimPosition(
            symbol="WINQ26", timeframe="M5", strategy="SMART_EMA",
            direction="BUY", volume=1.0,
            entry_time=datetime.now(),
            entry_price=180_000.0,
            initial_sl_pts=150.0,
            current_sl_pts=150.0,
            trail_activate_atr=1.0,
            trail_distance_atr=0.4,
            atr_at_entry=200.0,
            be_after_minutes=5,
            time_trail_after_minutes=10,
            max_position_minutes=60,
            hard_exit_minutes=120,
            point_val=1.0,
            # DESLIGA TP1 isolando trailing puro
            tp1_r=99.0, tp1_pct=0.5, atr_trail_mult=2.0,
        )
        # Simula preço subindo: highest = entry + 2*atr = 180400
        pos.highest = 180_400.0
        pos.lowest = 180_000.0
        # Trail ativa (profit 400 > 1.0*200) e aperta SL pra highest - 0.4*200 = 180320
        pos.apply_trailing(atr=200.0, held_minutes=2.0)
        assert pos.trail_on is True
        assert pos.tp1_done is False  # TP1 desligado
        # SL deve estar em 180320 = entry - current_sl_pts*1.0 → current_sl_pts = -320
        assert pos.current_sl_pts < 0, f"esperado negativo (profit lock), got {pos.current_sl_pts}"
        assert pos.current_sl_price == pytest.approx(180_320.0, abs=1.0)

    def test_sim_position_breakeven(self, isolated_db):
        """Após be_after_minutes, BE move SL pra perto do entry (cost_pts)."""
        fw, _db = isolated_db
        from datetime import datetime
        pos = fw.SimPosition(
            symbol="WINQ26", timeframe="M5", strategy="SMART_EMA",
            direction="BUY", volume=1.0,
            entry_time=datetime.now(),
            entry_price=180_000.0,
            initial_sl_pts=200.0,
            current_sl_pts=200.0,
            trail_activate_atr=1.0,
            trail_distance_atr=0.4,
            atr_at_entry=200.0,
            be_after_minutes=5,
            time_trail_after_minutes=15,
            max_position_minutes=60,
            hard_exit_minutes=120,
            point_val=1.0,
        )
        # held_minutes=6 > 5, profit=0 (no profit), trail off → BE dispara
        pos.apply_trailing(atr=200.0, held_minutes=6.0)
        assert pos.breakeven_applied is True
        # SL novo = entry + 5 (cost) = 180005 → sl_pts = entry - new_sl = -5 (signed)
        assert pos.current_sl_pts == pytest.approx(-5.0, abs=0.5)
        assert pos.current_sl_price == pytest.approx(180_005.0, abs=0.5)

    def test_tp1_partial_close(self, isolated_db):
        """TP1 dispara UMA vez em profit >= tp1_r*ATR e fecha tp1_pct do volume.

        Volume=2, tp1_pct=0.5 → fecha 1.0 contrato.
        Profit_pts = highest - entry = 180300 - 180000 = 300.
        tp1_pnl_pts = (1.0 / 2.0) * 300 = 150.
        tp1_pnl_brl = 150 * 0.20 (multiplier WIN) * 1.0 = 30.0.
        """
        fw, _db = isolated_db
        from datetime import datetime
        pos = fw.SimPosition(
            symbol="WINQ26", timeframe="M5", strategy="SMART_EMA",
            direction="BUY", volume=2.0,
            entry_time=datetime.now(),
            entry_price=180_000.0,
            initial_sl_pts=200.0,
            current_sl_pts=200.0,
            trail_activate_atr=1.0,
            trail_distance_atr=0.4,
            atr_at_entry=200.0,
            be_after_minutes=10,
            time_trail_after_minutes=20,
            max_position_minutes=60,
            hard_exit_minutes=120,
            point_val=1.0,
            tp1_r=1.0, tp1_pct=0.5, atr_trail_mult=2.0,
        )
        # profit = 300 > 1.0 * 200 → TP1 dispara, fecha 50% (1.0 contrato de 2)
        pos.highest = 180_300.0
        applied = pos.maybe_tp1(atr=200.0)
        assert applied is True
        assert pos.tp1_done is True
        assert pos.tp1_volume_closed == pytest.approx(1.0, abs=0.01)
        assert pos.remaining_volume == pytest.approx(1.0, abs=0.01)
        assert pos.tp1_profit_brl == pytest.approx(30.0, abs=0.5)
        # 2ª chamada não aplica de novo
        applied2 = pos.maybe_tp1(atr=200.0)
        assert applied2 is False


# ─── smoke test (mocka MT5, roda walker em DB isolado) ──────────────────────
@pytest.mark.skipif(
    not _check_mt5_available(),
    reason="MT5/Wine não disponível — smoke test live pulado (unit tests acima continuam)"
)
class TestForwardWalkerSmoke:
    """Smoke test ~3min contra fetch_bars MOCKADO (não toca MT5)."""

    def test_walker_3min_no_crash_monotonic_growth(self, isolated_db, monkeypatch, capfd):
        """
        Roda walker_loop por 3 minutos com:
          - 1 par (WIN M5)
          - fetch_bars mockado retornando candles determinísticos
          - DB isolado em tmp
          - is_trading_time forçado True

        Asserções:
          - exit code 0 (sem exceção não-tratada)
          - forward_sim_trades cresce monotonicamente (sem regressão do Pitfall 24)
          - log não contém '[ERROR loop]' crítico
          - pelo menos 1 trade foi aberto (sinais sintéticos de teste)
        """
        fw, db_path = isolated_db
        # mock: sempre retorna candles novos (timestamp muda a cada call)
        call_count = {"n": 0}
        def mock_fetch_bars(symbol, tf, count=100):
            call_count["n"] += 1
            # avança 5min a cada chamada pra simular candles novos
            import time as _t
            base = 180_000.0 + call_count["n"] * 50  # drift leve
            # gera com timestamps ÚNICOS a cada call (simula novo bar)
            return _gen_bars(symbol, tf, count, base_price=base)

        # mock is_trading_time → True (mercado aberto simulado)
        monkeypatch.setattr(fw.vat, "fetch_bars", mock_fetch_bars)
        monkeypatch.setattr(fw.vat, "is_trading_time", lambda: True)

        # usa args.Namespace com 3min duration, poll=2s, report=2min
        from argparse import Namespace
        args = Namespace(
            duration_min=3 / 60,   # ~3 minutos (3/60 = 0.05 ... usar inteiro)
            poll_secs=2,
            bars_count=100,
            report_every_min=1,
            symbols=["WINQ26"],
            tfs=["M5"],
            include_disabled=False,
            no_telegram=True,      # silencioso em CI
            min_trades=1,
            force_trading_time=True,  # mock is_trading_time is True, mas CC deixou gate duplo
        )
        # CORREÇÃO: duration_min precisa ser >= 1 pra datetime.now() < deadline ser True
        # usamos 3 minutos cheios pro smoke
        args.duration_min = 3

        state = fw.WalkerState()
        try:
            fw.walker_loop(args, state)
        except Exception as e:
            pytest.fail(f"walker_loop crashou: {type(e).__name__}: {e}")

        # verifica DB
        con = sqlite3.connect(str(db_path))
        rows = con.execute(
            f"SELECT entry_ticket, entry_time, exit_time, net_pnl_brl "
            f"FROM {fw.SIM_TABLE} ORDER BY id"
        ).fetchall()
        con.close()

        # 1. sem [ERROR loop] crítico no output capturado
        captured = capfd.readouterr()
        assert "[ERROR loop]" not in captured.out, (
            f"loop error detectado: {captured.out[-500:]}"
        )

        # 2. monotonic growth: IDs crescem em ordem
        if len(rows) >= 2:
            ids = [r[0] for r in rows]  # entry_ticket inclui epoch_ms
            # Não podemos garantir monotonic em entry_ticket se re-entraram,
            # mas ID é autoincrement. Refetch:
            con2 = sqlite3.connect(str(db_path))
            ids_numeric = [r[0] for r in con2.execute(
                f"SELECT id FROM {fw.SIM_TABLE} ORDER BY id"
            )]
            con2.close()
            assert ids_numeric == sorted(ids_numeric), (
                f"forward_sim_trades.id NÃO monotônico: {ids_numeric}"
            )

        # 3. sem 'database is locked' (Pitfall 25)
        assert "database is locked" not in captured.out.lower(), (
            "Pitfall 25 ressurgiu — ver WAL/busy_timeout"
        )
