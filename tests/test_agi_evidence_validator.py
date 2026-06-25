"""
test_agi_evidence_validator.py
================================
TDD: garante que AGI valida mudanças contra DADOS REAIS (não só Pydantic schema).

Gap identificado 2026-06-25:
- optimization/agi_safety_validator.py valida TIPOS (Pydantic) e regras simples.
- MAS NÃO consulta vt_trades.db pra validar se a mudança MELHORA ou PIORA
  o histórico real do par.

Bug real: AGI pode propor BIT reabilitar (PF=26.58 sobre 1 trade outlier)
sem checar que BIT tem -R$7k em 30d. Hoje BIT j\u00e1 est\u00e1 desabilitado manualmente.

Este teste (RED) vai falhar at\u00e9 optimization/agi_evidence_validator.py existir.

EXPECTATIVA: validate_against_reality(symbol, new_params, db_path) retorna:
  - (True, "") se a mudan\u00e7a \u00e9 segura (WR>=35%, PnL>=0, sem streak>=5)
  - (False, "motivo") se a mudan\u00e7a \u00e9 perigosa (WR<35%, PnL<0, streak>=5)
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


def _create_temp_db_with_trades(trades):
    """Cria DB tempor\u00e1rio com schema correto + insere trades."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_time TEXT NOT NULL,
            exit_time TEXT,
            symbol TEXT NOT NULL,
            timeframe TEXT DEFAULT 'M5',
            direction TEXT NOT NULL,
            entry_price REAL NOT NULL,
            exit_price REAL,
            net_pnl REAL DEFAULT 0,
            exit_reason TEXT,
            strategy TEXT
        );
    """)
    for t in trades:
        conn.execute("""
            INSERT INTO trades (entry_time, exit_time, symbol, timeframe, direction,
                              entry_price, exit_price, net_pnl, exit_reason, strategy)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, t)
    conn.commit()
    conn.close()
    return path


def _make_trade(days_ago, pnl, symbol="WINQ26", tf="M5", strategy="STRONG_TREND"):
    """Helper: cria um trade fechado days_ago atr\u00e1s com pnl dado."""
    entry = (datetime.now() - timedelta(days=days_ago)).isoformat()
    exit_t = (datetime.now() - timedelta(days=days_ago, hours=-1)).isoformat()
    return (
        entry, exit_t, symbol, tf, "BUY",
        100.0, 101.0, pnl, "SL_SERVIDOR", strategy
    )


class TestAGIEvidenceValidator(unittest.TestCase):
    """TDD: validate_against_reality precisa existir e bloquear mudan\u00e7as ruins."""

    def test_module_exists_and_imports(self):
        """O m\u00f3dulo optimization/agi_evidence_validator.py precisa existir."""
        try:
            from optimization.agi_evidence_validator import validate_against_reality
        except ImportError as e:
            self.fail(
                f"optimization/agi_evidence_validator.py n\u00e3o existe ou n\u00e3o "
                f"importa: {e}. Crie o m\u00f3dulo com validate_against_reality()."
            )

    def test_validator_function_exists(self):
        """validate_against_reality(symbol, new_params, db_path) -> tuple[bool, str]."""
        from optimization.agi_evidence_validator import validate_against_reality
        # Cria DB tempor\u00e1rio vazio
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            result = validate_against_reality("WINQ26", {"sl_atr_mult": 1.5}, db_path)
            self.assertIsInstance(result, tuple, f"deve retornar tuple, retornou {type(result)}")
            self.assertEqual(len(result), 2, f"tuple deve ter 2 elementos, tem {len(result)}")
        finally:
            os.unlink(db_path)

    def test_blocks_symbol_with_below_35_wr(self):
        """Se símbolo tem WR<35% em >=10 trades é BLOQUEADO."""
        from optimization.agi_evidence_validator import validate_against_reality

        # WINQ26 com 10 trades: 2W/8L = 20% WR (W nas posições ímpares de 1..10)
        pnls = [50, -50, -50, -50, -50, -50, -50, -50, -50, 50]  # 2W, 8L
        trades = [_make_trade(d, pnl=p) for d, p in enumerate(pnls, 1)]
        db_path = _create_temp_db_with_trades(trades)
        try:
            ok, reason = validate_against_reality(
                "WINQ26", {"sl_atr_mult": 1.5}, db_path
            )
            self.assertFalse(
                ok,
                f"WINQ26 com 20% WR deve ser BLOQUEADO. ok={ok}, reason={reason!r}"
            )
            self.assertIn("WR", reason.upper(),
                         f"reason deve mencionar WR. Actual: {reason!r}")
        finally:
            os.unlink(db_path)

    def test_allows_symbol_with_above_50_wr(self):
        """Se símbolo tem WR>=50% em >=10 trades é PERMITIDO."""
        from optimization.agi_evidence_validator import validate_against_reality

        # WINQ26 com 10 trades: 7W/3L = 70% WR (W nas posições múltiplas de 3)
        pnls = [50, 50, 50, -50, 50, 50, 50, -50, 50, -50]  # 7W, 3L
        trades = [_make_trade(d, pnl=p) for d, p in enumerate(pnls, 1)]
        db_path = _create_temp_db_with_trades(trades)
        try:
            ok, reason = validate_against_reality(
                "WINQ26", {"sl_atr_mult": 1.5}, db_path
            )
            self.assertTrue(
                ok,
                f"WINQ26 com 70% WR deve ser PERMITIDO. ok={ok}, reason={reason!r}"
            )
        finally:
            os.unlink(db_path)

    def test_blocks_symbol_with_streak_5_losses(self):
        """Se símbolo tem streak >=5 losses consecutivos recentes é BLOQUEADO."""
        from optimization.agi_evidence_validator import validate_against_reality

        # WINQ26 com 13 trades: 8W (antigos) + 5L (recentes).
        # Recentes devem ser os losses pra detectar streak.
        # d=1 (mais recente) = loss, d=2..13 = wins antigos.
        # Para isso, _make_trade com d=1 é "ontem" (mais recente).
        # Precisamos: trade recente = loss. Logo _make_trade(d=1, pnl=-50).
        pnls_recent = [-50, -50, -50, -50, -50]  # últimos 5 trades
        pnls_old = [50, 50, 50, 50, 50, 50, 50, 50]   # 8 wins antigos
        # d=1 = mais recente; então -50 para d=1..5
        pnls = pnls_old + pnls_recent  # mas _make_trade com d=N: entry = now - N days
        # Logo se d=1: now-1day (mais recente); d=5: now-5day (mais antigo entre recentes)
        # pnls[0] = d=1 (mais recente) = -50 (primeiro loss recente)
        pnls = pnls_old[::-1] + pnls_recent  # d=13 primeiro (antigo), depois recentes
        # Quando iteramos pnls com enumerate(d=1..13), pnls[0]=d=1=WIN antigo
        # então inverto: para que d=1 = -50, pnls[0]=-50, pnls[12]=+50 (antigo)
        pnls = pnls_recent + pnls_old  # d=1..5 = LOSS, d=6..13 = WIN
        # → ORDER BY DESC: [-50, -50, -50, -50, -50, 50, 50, ..., 50] (5 losses consecutivos)
        trades = [_make_trade(d, pnl=p) for d, p in enumerate(pnls, 1)]
        db_path = _create_temp_db_with_trades(trades)
        try:
            ok, reason = validate_against_reality(
                "WINQ26", {"sl_atr_mult": 1.5}, db_path
            )
            self.assertFalse(
                ok,
                f"5 losses consecutivos recentes deve BLOQUEAR mesmo com WR boa. ok={ok}, reason={reason!r}"
            )
            self.assertTrue(
                "STREAK" in reason.upper() or "LOSS" in reason.upper() or "CONSEC" in reason.upper() or "MOMENTUM" in reason.upper(),
                f"reason deve mencionar streak/loss. Actual: {reason!r}"
            )
        finally:
            os.unlink(db_path)

    def test_permits_symbol_with_few_trades(self):
        """Se s\u00edmbolo tem <10 trades (evid\u00eancia insuficiente), PERMITIR."""
        from optimization.agi_evidence_validator import validate_against_reality

        # WINQ26 com s\u00f3 3 trades (WR=33%, evid\u00eancia insuficiente)
        trades = [_make_trade(d, pnl=-50) for d in range(1, 4)]
        db_path = _create_temp_db_with_trades(trades)
        try:
            ok, reason = validate_against_reality(
                "WINQ26", {"sl_atr_mult": 1.5}, db_path
            )
            self.assertTrue(
                ok,
                f"<10 trades deve PERMITIR (evid\u00eancia insuficiente). ok={ok}, reason={reason!r}"
            )
        finally:
            os.unlink(db_path)

    def test_does_not_modify_db(self):
        """O validator \u00e9 read-only: NUNCA deve modificar vt_trades.db."""
        from optimization.agi_evidence_validator import validate_against_reality

        trades = [_make_trade(d, pnl=50) for d in range(1, 6)]
        db_path = _create_temp_db_with_trades(trades)
        # Snapshot mtime + tamanho
        try:
            mtime_before = os.path.getmtime(db_path)
            size_before = os.path.getsize(db_path)
            validate_against_reality("WINQ26", {"sl_atr_mult": 1.5}, db_path)
            validate_against_reality("WINQ26", {"cooldown_seconds": 600}, db_path)
            mtime_after = os.path.getmtime(db_path)
            size_after = os.path.getsize(db_path)
            self.assertEqual(mtime_before, mtime_after,
                             "validate_against_reality n\u00e3o deve modificar mtime do DB")
            self.assertEqual(size_before, size_after,
                             "validate_against_reality n\u00e3o deve modificar tamanho do DB")
        finally:
            os.unlink(db_path)


if __name__ == "__main__":
    unittest.main()
