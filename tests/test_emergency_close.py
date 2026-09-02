"""
test_emergency_close.py
========================
TDD: cobre o safety-first "EMERGENCY CLOSE" quando modify_sl falha definitivamente.

CONTEXTO DO BUG (23/06/2026 13:24-13:25):
- Autotrader em loop infinito tentando modificar SL da posição WSPU26 SELL.
- safe_modify_sl em mt5_error_recovery.py tem guard anti-loop (MAX_FIX_ATTEMPTS=3)
  que ABORTA sem fechar a posição.
- manage_position() re-chama safe_modify_sl a cada ciclo (30s) → loop infinito.
- Posição fica aberta sem SL funcional → prejuízo acumulado (-R$30 WSPU26).
- Regra Bruno: "se o SL não está sendo possível alterar e a operação está indo
  contra, deve-se fechar imediatamente a operação para não aumentar a despesa".

SOLUÇÃO:
- Wrapper safe_modify_sl_with_emergency_close() em core/vt_emergency.py
- Se safe_modify_sl falha definitivamente (status != "ok") E posição contra
  (PnL < 0 OR price contra entry): fecha IMEDIATAMENTE + notifica critical +
  grava close_source='EMERGENCY_CLOSE' no DB.

ESTE TESTE:
- Cobre 7 cenários: sucesso 1ª tentativa, sucesso no retry, falha contra,
  falha a favor, tentativas exatas, DB, Telegram.
"""
import sys
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, "/home/bruno/Projects/Vibe-Trading")


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────

def make_tmp_db():
    """Cria DB temporário com schema completo de trades (com close_source + daily_summary)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript("""
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_ticket TEXT,
    exit_ticket TEXT,
    magic_number INTEGER DEFAULT 555501,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('BUY', 'SELL')),
    volume REAL NOT NULL,
    timeframe TEXT DEFAULT 'M5',
    entry_time TEXT NOT NULL,
    entry_price REAL NOT NULL,
    entry_sl REAL,
    exit_time TEXT,
    exit_price REAL,
    exit_reason TEXT,
    exit_sl_price REAL,
    close_source TEXT DEFAULT NULL,
    gross_pnl REAL DEFAULT 0,
    fees REAL DEFAULT 0,
    swap REAL DEFAULT 0,
    net_pnl REAL DEFAULT 0,
    is_day_trade INTEGER DEFAULT 1,
    asset_type TEXT DEFAULT 'FUTURE',
    multiplier REAL DEFAULT 0.20,
    strategy TEXT DEFAULT 'VWAP',
    signal_detail TEXT,
    raw_entry_json TEXT,
    raw_exit_json TEXT,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE daily_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    n_trades INTEGER DEFAULT 0,
    n_winners INTEGER DEFAULT 0,
    n_losers INTEGER DEFAULT 0,
    gross_pnl REAL DEFAULT 0,
    fees REAL DEFAULT 0,
    net_pnl REAL DEFAULT 0,
    max_win REAL DEFAULT 0,
    max_loss REAL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(date, symbol)
);
""")
    conn.commit()
    conn.close()
    return path


def seed_open_trade(db_path, symbol="WSPU26", direction="SELL",
                     entry_price=7470.0, volume=1.0, ticket="2462125180"):
    """Insere trade aberta no DB (igual ao WSPU26 do bug)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("""
        INSERT INTO trades (symbol, direction, volume, entry_time, entry_price,
                           entry_sl, entry_ticket, multiplier)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (symbol, direction, volume, "2026-06-23 13:00:33",
          entry_price, entry_price + 1.0, ticket, 1.0))
    trade_id = cur.lastrowid
    conn.commit()
    conn.close()
    return trade_id


# ────────────────────────────────────────────────────────────────────
# Testes
# ────────────────────────────────────────────────────────────────────

class TestEmergencyClose:
    """Cobre safe_modify_sl_with_emergency_close() — safety-first do WSPU26."""

    def _import_module(self, monkeypatch_db_path=None):
        """Importa vt_emergency sob demanda (depois de patchar DB)."""
        import importlib
        import core.vt_emergency as em
        importlib.reload(em)
        return em

    # ─── Cenário 1: modify OK na 1ª → SEM emergency ───
    def test_no_emergency_close_when_modify_succeeds(self):
        """Modify OK na 1ª tentativa → não fecha, não notifica critical."""
        from core import vt_emergency as em

        # Mock do safe_modify_sl retornando OK direto
        with patch.object(em, "safe_modify_sl") as mock_mod, \
             patch.object(em, "_emergency_close_position") as mock_close, \
             patch.object(em, "_notify_critical_emergency") as mock_notify:
            mock_mod.return_value = {"status": "ok", "new_sl": 15}

            result = em.safe_modify_sl_with_emergency_close(
                symbol="WSPU26",
                ticket="2462125180",
                sl_pts=15,
                entry_price=7470.0,
                direction="SELL",
                trade_log_id=42,
            )

            assert result["status"] == "ok"
            assert result.get("emergency_closed") is False, \
                "modify OK NÃO pode disparar emergency close"
            mock_close.assert_not_called()
            mock_notify.assert_not_called()

    # ─── Cenário 2: 3 tentativas falhadas + PnL negativo → FECHA ───
    def test_emergency_close_when_modify_fails_and_position_against_us(self):
        """3 falhas + PnL negativo → emergency close IMEDIATO."""
        from core import vt_emergency as em

        # Mock: safe_modify_sl falha definitivamente (status != "ok")
        # Mock: PnL negativo (-30.0) → posição contra
        with patch.object(em, "safe_modify_sl") as mock_mod, \
             patch.object(em, "_get_current_pnl") as mock_pnl, \
             patch.object(em, "_emergency_close_position") as mock_close, \
             patch.object(em, "_notify_critical_emergency") as mock_notify:
            mock_mod.return_value = {
                "status": "aborted",
                "error": "max fix attempts (3) atingido",
                "attempts": 3,
            }
            mock_pnl.return_value = -30.0  # contra!
            mock_close.return_value = {"status": "ok", "exit_price": 7472.5}

            result = em.safe_modify_sl_with_emergency_close(
                symbol="WSPU26",
                ticket="2462125180",
                sl_pts=14,
                entry_price=7470.0,
                direction="SELL",
                trade_log_id=42,
            )

            assert result["status"] == "emergency_closed", \
                f"esperava emergency_closed, recebi {result}"
            assert result.get("emergency_closed") is True
            mock_close.assert_called_once()
            mock_notify.assert_called_once()
            # Args do close: symbol, ticket, trade_log_id (todos keyword ou posicional)
            args, kwargs = mock_close.call_args
            # Aceita tanto posicional quanto keyword
            symbol_arg = args[0] if len(args) > 0 else kwargs.get("symbol")
            ticket_arg = args[1] if len(args) > 1 else kwargs.get("ticket")
            assert symbol_arg == "WSPU26", f"symbol errado: {symbol_arg}"
            assert str(ticket_arg) == "2462125180", f"ticket errado: {ticket_arg}"

    # ─── Cenário 3: falha mas posição a favor → NÃO fecha ───
    def test_no_emergency_close_when_position_in_profit(self):
        """Modify falha mas PnL positivo → NÃO fecha (apenas loga warning)."""
        from core import vt_emergency as em

        with patch.object(em, "safe_modify_sl") as mock_mod, \
             patch.object(em, "_get_current_pnl") as mock_pnl, \
             patch.object(em, "_emergency_close_position") as mock_close, \
             patch.object(em, "_notify_silent") as mock_silent:
            mock_mod.return_value = {
                "status": "aborted",
                "error": "max fix attempts (3) atingido",
                "attempts": 3,
            }
            mock_pnl.return_value = 50.0  # a favor!
            mock_close.return_value = None

            result = em.safe_modify_sl_with_emergency_close(
                symbol="WSPU26",
                ticket="2462125180",
                sl_pts=14,
                entry_price=7470.0,
                direction="SELL",
                trade_log_id=42,
            )

            assert result["status"] == "aborted"
            assert result.get("emergency_closed") is False, \
                "PnL positivo NÃO pode disparar emergency close"
            mock_close.assert_not_called()
            # Deve logar warning silencioso
            mock_silent.assert_called()

    # ─── Cenário 4: 3 falhas exatas = 1 close ───
    def test_emergency_close_only_after_max_attempts(self):
        """3 falhas exatas = 1 close (não 2, não 0)."""
        from core import vt_emergency as em

        with patch.object(em, "safe_modify_sl") as mock_mod, \
             patch.object(em, "_get_current_pnl") as mock_pnl, \
             patch.object(em, "_emergency_close_position") as mock_close:
            # Falha 3 vezes — safe_modify_sl retorna status != "ok" mas attempts=3
            mock_mod.return_value = {
                "status": "aborted",
                "attempts": 3,
                "error": "Invalid stops",
            }
            mock_pnl.return_value = -10.0
            mock_close.return_value = {"status": "ok"}

            em.safe_modify_sl_with_emergency_close(
                symbol="BITM26", ticket="123", sl_pts=100,
                entry_price=330000.0, direction="BUY", trade_log_id=1,
            )

            assert mock_close.call_count == 1, \
                f"esperava 1 close, recebi {mock_close.call_count}"

    # ─── Cenário 5: DB grava close_source='EMERGENCY_CLOSE' ───
    def test_emergency_close_records_emergency_in_db(self):
        """DB deve ter close_source='EMERGENCY_CLOSE' + exit_reason='EMERGENCY_CLOSE_SL_FAILED'."""
        db_path = make_tmp_db()
        trade_id = seed_open_trade(db_path, symbol="WSPU26", direction="SELL",
                                    entry_price=7470.0, ticket="2462125180")

        # Importar vt_trade_log e patchar DB_PATH
        import core.vt_trade_log as tl
        tl.DB_PATH = Path(db_path)

        from core import vt_emergency as em
        # Mockar mt5_orchestrator.close pra não chamar wine
        with patch.object(em, "safe_close") as mock_close_mt5:
            mock_close_mt5.return_value = {"status": "ok", "exit_price": 7472.5}

            em._emergency_close_position(
                symbol="WSPU26",
                ticket="2462125180",
                trade_log_id=trade_id,
                pnl=-30.0,
                attempts=3,
                last_error="Invalid stops",
            )

        # Verificar DB
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
        conn.close()

        assert row is not None, "trade deveria existir"
        assert row["exit_reason"] == "EMERGENCY_CLOSE_SL_FAILED", \
            f"exit_reason errado: {row['exit_reason']}"
        assert row["close_source"] == "EMERGENCY_CLOSE", \
            f"close_source errado: {row['close_source']}"
        assert row["exit_time"] is not None
        # PnL deve estar negativo (SELL: entry=7470, exit=7472.5 → loss)
        assert row["gross_pnl"] < 0, f"gross_pnl deveria ser negativo: {row['gross_pnl']}"

        # Cleanup
        os.unlink(db_path)

    # ─── Cenário 6: Telegram critical com detalhes ───
    def test_emergency_close_sends_critical_telegram(self):
        """Telegram critical com: symbol, attempts, pnl, error."""
        from core import vt_emergency as em

        sent = []
        def fake_send(msg):
            sent.append(msg)

        # Mockar notify_critical do notify_log_filter
        with patch("core.vt_notify_log_filter.notify_critical") as mock_crit:
            mock_crit.side_effect = lambda msg, **kw: fake_send(msg) or True

            em._notify_critical_emergency(
                symbol="WSPU26",
                ticket="2462125180",
                attempts=3,
                pnl=-30.0,
                last_error="Invalid stops",
                exit_price=7472.5,
            )

            assert mock_crit.called, "notify_critical deveria ter sido chamado"
            args = mock_crit.call_args
            msg = args[0][0]  # primeiro arg posicional é msg
            assert "WSPU26" in msg, f"msg deveria mencionar WSPU26: {msg}"
            assert "EMERGENCY" in msg.upper() or "EMERGÊNCIA" in msg.upper(), \
                f"msg deveria mencionar EMERGENCY: {msg}"
            assert "-30" in msg or "30.00" in msg, \
                f"msg deveria mencionar PnL -30: {msg}"
            assert "3" in msg, f"msg deveria mencionar tentativas=3: {msg}"

    # ─── Cenário 7: sucesso no retry (2 falhas, 3ª sucesso) → SEM emergency ───
    def test_safe_modify_sl_succeeds_on_retry(self):
        """safe_modify_sl() faz 2 fix attempts que falham, 3ª sucede."""
        # Esse teste é mais interno ao mt5_error_recovery.safe_modify_sl
        # Vamos garantir que safe_modify_sl_with_emergency_close respeita
        # o status=="ok" mesmo após retries.

        from core import vt_emergency as em

        # safe_modify_sl já trata retries internamente. O wrapper
        # só age se o status retornado for != "ok".
        # Aqui simulamos que safe_modify_sl teve sucesso após retries internos
        # e retorna status="ok".
        with patch.object(em, "safe_modify_sl") as mock_mod, \
             patch.object(em, "_emergency_close_position") as mock_close:
            # success após 3 retries (3 fix attempts, 3ª OK)
            mock_mod.return_value = {
                "status": "ok",
                "new_sl": 33,
                "attempts": 3,
            }

            result = em.safe_modify_sl_with_emergency_close(
                symbol="BITM26", ticket="123", sl_pts=33,
                entry_price=330000.0, direction="BUY", trade_log_id=1,
            )

            assert result["status"] == "ok"
            assert result.get("emergency_closed") is False
            mock_close.assert_not_called()


# ────────────────────────────────────────────────────────────────────
# Testes da constante MAX_SL_MODIFY_ATTEMPTS
# ────────────────────────────────────────────────────────────────────

class TestConstants:
    """A constante MAX_SL_MODIFY_ATTEMPTS deve existir e ser 3."""

    def test_max_sl_modify_attempts_exists(self):
        from core import vt_emergency as em
        assert hasattr(em, "MAX_SL_MODIFY_ATTEMPTS"), \
            "MAX_SL_MODIFY_ATTEMPTS deve existir"

    def test_max_sl_modify_attempts_value(self):
        from core import vt_emergency as em
        assert em.MAX_SL_MODIFY_ATTEMPTS == 3, \
            f"MAX_SL_MODIFY_ATTEMPTS deveria ser 3, é {em.MAX_SL_MODIFY_ATTEMPTS}"


class TestNettingAdoptionWave891:
    """Wave 891 (incidente 02/09 10:19): filho consolidado em PAI netting.

    - POSITION_NOT_FOUND no modify do filho → adota o PAI e reaplica o SL
      (tightest-SL-wins) convertendo sl_pts via price_open de cada um.
    - PnL do filho é None → usa o PnL netted do PAI como verdade (não fecha
      exposição de OUTRA estratégia que está no lucro).
    - Sem container netted → safety-first original preservado.
    """

    def _mod(self):
        from core import vt_emergency as em
        return em

    def test_adopt_parent_reapplies_sl(self):
        from core import vt_emergency as em
        with patch.object(em, "safe_modify_sl") as mock_mod, \
             patch.object(em, "_get_current_pnl", return_value=None), \
             patch.object(em, "_netting_container") as mock_cont, \
             patch("mt5.mt5_error_recovery._get_point_val", return_value=1.0):
            # 1a chamada: filho POSITION_NOT_FOUND; 2a: pai recebe o SL
            mock_mod.side_effect = [
                {"status": "failed", "attempts": 3,
                 "error": "POSITION_NOT_FOUND: ticket 111 não encontrado em WSPU26"},
                {"status": "ok"},
            ]
            mock_cont.return_value = (222, 50.0, 95.0)  # pai: ticket 222, +50, open 95
            r = em.safe_modify_sl_with_emergency_close(
                symbol="WSPU26", ticket=111, sl_pts=20, entry_price=100.0,
                direction="BUY",
            )
            # SL absoluto do filho = 100 - 20 = 80; no pai (open 95): (95-80)/1 = 15
            assert mock_mod.call_count == 2
            assert mock_mod.call_args_list[1].kwargs["ticket"] == 222
            assert mock_mod.call_args_list[1].kwargs["sl_pts"] == 15
            assert r["status"] == "ok" and r.get("adopted_parent") == 222
            # sem emergency: pai no lucro (+50) jamais deveria fechar
            assert r.get("emergency_closed") is False

    def test_netting_parent_losing_closes_with_named_cause(self):
        from core import vt_emergency as em
        with patch.object(em, "safe_modify_sl") as mock_mod, \
             patch.object(em, "_get_current_pnl", return_value=None), \
             patch.object(em, "_netting_container") as mock_cont, \
             patch.object(em, "_emergency_close_position") as mock_close, \
             patch("mt5.mt5_error_recovery._get_point_val", return_value=1.0), \
             patch.object(em, "_notify_critical_emergency"):
            mock_mod.side_effect = [
                {"status": "failed", "attempts": 3,
                 "error": "POSITION_NOT_FOUND: ticket 111 não encontrado em WSPU26"},
                {"status": "failed", "attempts": 1, "error": "no changes"},
            ]
            mock_cont.return_value = (222, -30.0, 95.0)  # pai no PREJUÍZO
            mock_close.return_value = {"status": "closed", "exit_price": 80.0}
            r = em.safe_modify_sl_with_emergency_close(
                symbol="WSPU26", ticket=111, sl_pts=20, entry_price=100.0,
                direction="BUY",
            )
            assert r.get("emergency_closed") is True
            # causa nomeada: last_error carrega a nota netting
            assert "netting" in r.get("emergency_reason", "") or \
                "netting" in str(mock_close.call_args.kwargs.get("last_error", "")) or \
                "netting" in str(r.get("underlying_result", {}).get("error", ""))

    def test_no_container_keeps_safety_first(self):
        from core import vt_emergency as em
        with patch.object(em, "safe_modify_sl") as mock_mod, \
             patch.object(em, "_get_current_pnl", return_value=None), \
             patch.object(em, "_netting_container", return_value=None), \
             patch.object(em, "_emergency_close_position") as mock_close, \
             patch.object(em, "_notify_critical_emergency"):
            mock_mod.return_value = {"status": "failed", "attempts": 3,
                                     "error": "POSITION_NOT_FOUND: não encontrado"}
            mock_close.return_value = {"status": "already_closed"}
            r = em.safe_modify_sl_with_emergency_close(
                symbol="WSPU26", ticket=111, sl_pts=20, entry_price=100.0,
                direction="BUY",
            )
            # sem pai netting: safety-first original (None → contra → fechar)
            assert r.get("emergency_closed") is True
            assert mock_mod.call_count == 1  # sem adoção, sem retry no pai
