"""
Testes de deduplicação de mensagens Telegram.
"""
import sys
import time
sys.path.insert(0, "/home/bruno/Projects/Vibe-Trading")

import core.vt_notify as nt


def test_notify_once_sends_first_message():
    """Primeira chamada com chave deve enviar."""
    nt.reset_cooldown()
    sent = []
    result = nt.notify_once("TEST:KEY:1", "msg 1", lambda m: sent.append(m))
    assert result is True
    assert sent == ["msg 1"]


def test_notify_once_suppresses_within_cooldown():
    """Segunda chamada dentro do cooldown não envia."""
    nt.reset_cooldown()
    sent = []
    nt.notify_once("TEST:KEY:2", "msg 1", lambda m: sent.append(m), cooldown_min=60)
    result2 = nt.notify_once("TEST:KEY:2", "msg 2", lambda m: sent.append(m), cooldown_min=60)
    assert result2 is False
    assert sent == ["msg 1"]


def test_notify_once_resets_after_cooldown():
    """Após cooldown expirar, deve enviar de novo."""
    nt.reset_cooldown()
    sent = []
    nt.notify_once("TEST:KEY:3", "msg 1", lambda m: sent.append(m), cooldown_min=0.001)
    time.sleep(0.1)
    result2 = nt.notify_once("TEST:KEY:3", "msg 2", lambda m: sent.append(m), cooldown_min=0.001)
    assert result2 is True
    assert len(sent) == 2


def test_different_keys_are_independent():
    """Chaves diferentes não compartilham cooldown."""
    nt.reset_cooldown()
    sent = []
    nt.notify_once("A:1", "msg A1", lambda m: sent.append(m))
    nt.notify_once("B:1", "msg B1", lambda m: sent.append(m))
    assert sent == ["msg A1", "msg B1"]


def test_streak_loss_deduped_per_pair_tf():
    """Cenário real: 5 STREAK_LOSS WINQ26 M5 em 30s = 1 msg."""
    nt.reset_cooldown()
    sent = []
    for i in range(5):
        nt.notify_once("STREAK_LOSS:WIN:M5", f"perda {i}", lambda m: sent.append(m))
    assert len(sent) == 1, f"deveria enviar 1 msg, enviou {len(sent)}"


def test_force_sends_ignoring_cooldown():
    """force=True deve enviar mesmo dentro do cooldown."""
    nt.reset_cooldown()
    sent = []
    nt.notify_once("FORCE:1", "msg 1", lambda m: sent.append(m), cooldown_min=60)
    nt.notify_once("FORCE:1", "msg 2", lambda m: sent.append(m), cooldown_min=60, force=True)
    assert len(sent) == 2


def test_default_cooldown_for_streak_loss():
    """notify_once com prefixo STREAK_LOSS deve usar cooldown de 60min."""
    nt.reset_cooldown()
    sent = []
    # sem passar cooldown_min, deve usar DEFAULT_COOLDOWNS
    nt.notify_once("STREAK_LOSS:WDO:M15", "msg 1", lambda m: sent.append(m))
    nt.notify_once("STREAK_LOSS:WDO:M15", "msg 2", lambda m: sent.append(m))
    assert len(sent) == 1


def test_default_cooldown_for_trade_open_is_zero():
    """Abertura de trade deve sempre enviar (cooldown=0)."""
    nt.reset_cooldown()
    sent = []
    for i in range(3):
        nt.notify_once(f"TRADE_OPEN:WINQ26:M5:{i}", f"open {i}", lambda m: sent.append(m))
    assert len(sent) == 3


def test_reset_cooldown_clears_state():
    """reset_cooldown deve permitir reenvio imediato."""
    nt.reset_cooldown()
    sent = []
    nt.notify_once("RESET:1", "msg 1", lambda m: sent.append(m), cooldown_min=60)
    nt.reset_cooldown("RESET:1")
    nt.notify_once("RESET:1", "msg 2", lambda m: sent.append(m), cooldown_min=60)
    assert sent == ["msg 1", "msg 2"]


def test_fmt_trade_open_includes_equity_pt_br():
    """Mensagem de abertura deve incluir equity, fonte [MT5], em PT-BR."""
    msg = nt.fmt_trade_open(
        symbol="WINQ26", tf="M5", side="BUY", qty=1, price=120000.0,
        sl=119800.0, atr=200.0, strategy="PIVOT_POINTS", ticket=12345,
        equity=1002976.45, daily_pnl=2456.35
    )
    assert "WINQ26" in msg
    assert "PIVOT_POINTS" in msg
    assert "[MT5]" in msg
    assert "Equity" in msg
    assert "R$ 1.002.976,45" in msg
    assert "PnL" in msg
    # Não deve ter marcadores em inglês
    assert "[DEBUG]" not in msg
    assert "ERROR" not in msg


def test_fmt_trade_close_includes_source_and_pnl():
    msg = nt.fmt_trade_close(
        symbol="WDOQ26", tf="M15", side="SELL", qty=1,
        entry=5226.5, exit_price=5220.0, pnl=65.0,
        reason="SL_SERVIDOR", ticket=2461981649, equity=1003050.0,
        daily_pnl=2456.35, source="MT5"
    )
    assert "🟢" in msg or "🔴" in msg
    assert "Fechou" in msg
    assert "[MT5]" in msg
    assert "SL_SERVIDOR" in msg


def test_fmt_streak_loss_pt_br():
    msg = nt.fmt_streak_loss("WIN", "M5", 3, 3, 2456.35)
    assert "STREAK_LOSS" in msg
    assert "WIN" in msg
    assert "PnL Dia" in msg
    assert "HALT" in msg
    assert "limite" in msg.lower() or "Limite" in msg


def test_fmt_reconcile_shows_inserted_count():
    result = {
        "inserted": [
            {"ticket": "300", "symbol": "BITM26", "direction": "BUY",
             "entry_price": 323080.0, "tf": "M5"}
        ],
        "closed": [],
        "divergences": [],
        "errors": [],
    }
    msg = nt.fmt_reconcile(result)
    assert "[RECONCILIADO]" in msg
    assert "BITM26" in msg
    assert "1 posição" in msg


def test_fmt_reconcile_shows_ok_when_synced():
    result = {"inserted": [], "closed": [], "divergences": [], "errors": []}
    msg = nt.fmt_reconcile(result)
    assert "sincronia" in msg.lower()
