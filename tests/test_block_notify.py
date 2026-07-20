"""
Testes do notificador centralizado de BLOQUEIOS DE OPERAÇÃO (Wave N+block_notify).

Cobre:
- notify_block_activated() roteamento por severidade (info / warning / critical)
- dedup via notify_once (vt_notify) com cooldown_min custom
- defaults por severidade (warning=60, critical=30)
- categories constants (CAT_*) exportadas e strings válidas
- formatação PT-BR com emoji + símbolo + TF
- integração com hermes_send via mock (não envia Telegram real em teste)
- reset_for_tests() limpa dedup
- severidade inválida cai pra warning sem crash
"""
import sys
from unittest.mock import patch

sys.path.insert(0, "/home/bruno/Projects/Vibe-Trading")

import core.vt_block_notify as bn
import core.vt_notify as nt


def _captured_send():
    """Retorna (send_fn, sent_list) — send_fn acumula mensagens em sent_list."""
    sent = []
    def _fn(msg):
        sent.append(msg)
    return _fn, sent


# ============================================================
# Roteamento por severidade
# ============================================================

def test_info_severity_does_not_call_telegram():
    """severity='info' deve só logar, nunca chamar hermes_send."""
    bn.reset_for_tests()
    sent = []
    with patch.object(bn, "_send_telegram", side_effect=lambda m: sent.append(m)) as mock_send:
        result = bn.notify_block_activated(
            "TEST_INFO", symbol="WIN", tf="M5",
            reason="info-only event", severity="info",
        )
    assert result is True
    assert mock_send.call_count == 0
    assert sent == []


def test_warning_calls_telegram_with_dedup():
    """severity='warning' deve chamar hermes_send na primeira vez."""
    bn.reset_for_tests()
    sent = []
    with patch.object(bn, "_send_telegram", side_effect=lambda m: sent.append(m)) as mock_send:
        r1 = bn.notify_block_activated(
            "TEST_WARN", symbol="WIN", tf="M5",
            reason="warn event", severity="warning", cooldown_min=60,
        )
        r2 = bn.notify_block_activated(
            "TEST_WARN", symbol="WIN", tf="M5",
            reason="warn event 2", severity="warning", cooldown_min=60,
        )
    assert r1 is True
    assert r2 is False  # dedup
    assert mock_send.call_count == 1
    assert len(sent) == 1


def test_critical_default_cooldown_is_30min():
    """severity='critical' default cooldown = 30min (mais apertado que warning)."""
    bn.reset_for_tests()
    with patch.object(bn, "notify_once", wraps=nt.notify_once) as mock_notify:
        bn.notify_block_activated(
            "TEST_CRIT", reason="crit event", severity="critical",
        )
    # notify_once foi chamado com cooldown_min=30
    args, kwargs = mock_notify.call_args
    assert kwargs.get("cooldown_min") == 30


def test_warning_default_cooldown_is_60min():
    """severity='warning' default cooldown = 60min."""
    bn.reset_for_tests()
    with patch.object(bn, "notify_once", wraps=nt.notify_once) as mock_notify:
        bn.notify_block_activated(
            "TEST_WARN2", reason="warn event", severity="warning",
        )
    args, kwargs = mock_notify.call_args
    assert kwargs.get("cooldown_min") == 60


def test_explicit_cooldown_overrides_default():
    """cooldown_min explícito sobrescreve o default da severidade."""
    bn.reset_for_tests()
    with patch.object(bn, "notify_once", wraps=nt.notify_once) as mock_notify:
        bn.notify_block_activated(
            "TEST_OVR", reason="x", severity="critical", cooldown_min=1440,
        )
    args, kwargs = mock_notify.call_args
    assert kwargs.get("cooldown_min") == 1440


# ============================================================
# Dedup key
# ============================================================

def test_dedup_key_includes_category_symbol_tf():
    """Dedup key = f'BLOCK:{category}:{symbol}:{tf}'."""
    bn.reset_for_tests()
    with patch.object(bn, "notify_once", wraps=nt.notify_once) as mock_notify:
        bn.notify_block_activated(
            "CAT_X", symbol="WIN", tf="M5",
            reason="r", severity="warning", cooldown_min=60,
        )
    args, kwargs = mock_notify.call_args
    assert kwargs.get("key") == "BLOCK:CAT_X:WIN:M5"


def test_different_symbols_have_independent_dedup():
    """(WIN, M5) e (WDO, M5) devem ter cooldown independente."""
    bn.reset_for_tests()
    sent = []
    with patch.object(bn, "_send_telegram", side_effect=lambda m: sent.append(m)):
        bn.notify_block_activated(
            "AGGREGATE_BLACKOUT", symbol="WIN", tf="M5",
            reason="r", severity="warning", cooldown_min=60,
        )
        bn.notify_block_activated(
            "AGGREGATE_BLACKOUT", symbol="WDO", tf="M5",
            reason="r", severity="warning", cooldown_min=60,
        )
    assert len(sent) == 2


def test_empty_symbol_and_tf_dedup():
    """Bloqueios cross-symbol (symbol='') devem dedupar por categoria."""
    bn.reset_for_tests()
    sent = []
    with patch.object(bn, "_send_telegram", side_effect=lambda m: sent.append(m)):
        bn.notify_block_activated(
            "HALT_TRADING", reason="r", severity="critical", cooldown_min=1440,
        )
        bn.notify_block_activated(
            "HALT_TRADING", reason="r2", severity="critical", cooldown_min=1440,
        )
    assert len(sent) == 1


# ============================================================
# Formatação PT-BR
# ============================================================

def test_message_includes_category_label():
    bn.reset_for_tests()
    sent = []
    with patch.object(bn, "_send_telegram", side_effect=lambda m: sent.append(m)):
        bn.notify_block_activated(
            "HALT_TRADING", reason="bot travado", severity="critical", cooldown_min=1440,
        )
    assert "HALT_TRADING" in sent[0]
    assert "bot travado" in sent[0]


def test_message_includes_symbol_and_tf_when_present():
    bn.reset_for_tests()
    sent = []
    with patch.object(bn, "_send_telegram", side_effect=lambda m: sent.append(m)):
        bn.notify_block_activated(
            "DISABLED_TF", symbol="WIN", tf="M5",
            reason="off", severity="warning", cooldown_min=3600,
        )
    msg = sent[0]
    assert "WIN" in msg
    assert "M5" in msg


def test_message_pt_br_with_emoji():
    bn.reset_for_tests()
    sent = []
    with patch.object(bn, "_send_telegram", side_effect=lambda m: sent.append(m)):
        bn.notify_block_activated(
            "HALT_TRADING", reason="r", severity="critical", cooldown_min=1440,
        )
        bn.notify_block_activated(
            "MAX_DAILY_LOSS", reason="r", severity="critical", cooldown_min=1440,
        )
        bn.notify_block_activated(
            "VALIDATOR_LLM_DOWN", reason="r", severity="critical", cooldown_min=30,
        )
        bn.notify_block_activated(
            "AGGREGATE_BLACKOUT", symbol="WIN", tf="M5", reason="r",
            severity="warning", cooldown_min=60,
        )
    emojis = [sent[i].split()[0] for i in range(4)]
    assert "🛑" in emojis[0]
    assert "🛑" in emojis[1]
    assert "🤖" in emojis[2]
    assert "⛔" in emojis[3]


# ============================================================
# Categorias constantes
# ============================================================

def test_all_cat_constants_are_strings():
    cats = [
        bn.CAT_HALT_TRADING,
        bn.CAT_HALT_NEW_TRADES,
        bn.CAT_DISABLED_SYMBOLS,
        bn.CAT_DISABLED_TF,
        bn.CAT_AGGREGATE_BLACKOUT,
        bn.CAT_MAX_DAILY_LOSS,
        bn.CAT_VALIDATOR_LLM_DOWN,
        bn.CAT_LEI3_MISSING_SL,
        bn.CAT_LEI4_RETCODE,
    ]
    for c in cats:
        assert isinstance(c, str)
        assert c == c.upper()  # SCREAMING_SNAKE
        assert " " not in c


def test_cat_constants_are_unique():
    cats = [
        bn.CAT_HALT_TRADING, bn.CAT_HALT_NEW_TRADES, bn.CAT_DISABLED_SYMBOLS,
        bn.CAT_DISABLED_TF, bn.CAT_AGGREGATE_BLACKOUT, bn.CAT_MAX_DAILY_LOSS,
        bn.CAT_VALIDATOR_LLM_DOWN, bn.CAT_LEI3_MISSING_SL, bn.CAT_LEI4_RETCODE,
    ]
    assert len(cats) == len(set(cats))


# ============================================================
# Resilience
# ============================================================

def test_invalid_severity_falls_back_to_warning():
    """severity desconhecida não deve crashar; cai pra warning."""
    bn.reset_for_tests()
    sent = []
    with patch.object(bn, "_send_telegram", side_effect=lambda m: sent.append(m)):
        # Não deve levantar exceção
        bn.notify_block_activated(
            "TEST_BAD", reason="r", severity="unknown_severity",
        )
    assert len(sent) == 1  # warning → Telegram


def test_send_failure_does_not_propagate():
    """Se _send_telegram falhar, função não propaga exceção (não derruba o bot)."""
    bn.reset_for_tests()
    with patch.object(bn, "_send_telegram", side_effect=RuntimeError("hermes offline")):
        # Não deve levantar exceção
        result = bn.notify_block_activated(
            "TEST_FAIL", reason="r", severity="warning", cooldown_min=60,
        )
    assert result is False


def test_reset_for_tests_clears_dedup():
    """reset_for_tests deve permitir reenvio imediato."""
    bn.reset_for_tests()
    sent = []
    with patch.object(bn, "_send_telegram", side_effect=lambda m: sent.append(m)):
        bn.notify_block_activated(
            "RESET_TEST", reason="first", severity="warning", cooldown_min=60,
        )
        bn.reset_for_tests()
        bn.notify_block_activated(
            "RESET_TEST", reason="second", severity="warning", cooldown_min=60,
        )
    assert len(sent) == 2


# ============================================================
# Integration com hermes_send (mockado)
# ============================================================

def test_default_target_includes_thread_id_suffix():
    """Default Telegram target deve ter ':1' (bypass anti-loop guard)."""
    import os
    os.environ.pop("VT_TELEGRAM_TARGET_BLOCK", None)
    assert bn.TELEGRAM_TARGET_DEFAULT == "telegram:-1004284773048:1"


def test_env_var_override_changes_target():
    """VT_TELEGRAM_TARGET_BLOCK sobrescreve default."""
    import os
    with patch.dict(os.environ, {"VT_TELEGRAM_TARGET_BLOCK": "telegram:-9999:1"}):
        with patch("core.vt_hermes_helper.hermes_send") as mock_hermes:
            mock_hermes.return_value = True
            bn.notify_block_activated(
                "ENV_TEST", reason="r", severity="warning", cooldown_min=60,
            )
    args, kwargs = mock_hermes.call_args
    # Primeiro arg posicional = target
    assert args[0] == "telegram:-9999:1"
