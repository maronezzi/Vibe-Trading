"""
Testes do filtro de logs do watchdog Vibe-Trading.

REGRA DE OURO (Bruno, 23/06/2026):
"informações que foram CORRIGIDAS e NÃO IMPACTAM na operação NÃO devem
aparecer no Telegram."

Estes testes cobrem o novo módulo `core.vt_notify_log_filter` que adiciona
3 níveis de notificação ao watchdog:

- notify_critical: sempre envia (vai pro Telegram)
- notify_sync_ok: envia SÓ se houver mudança real (deduplica por conteúdo)
- notify_silent: NUNCA envia pro Telegram (só log estruturado)

Aplica-se a:
- monitoring/vt_trade_watchdog.py (cron)
- core/vt_watchdog.py (reconcile)
"""
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, "/home/bruno/Projects/Vibe-Trading")

import core.vt_notify as nt
import core.vt_notify_log_filter as nlf


# ============================================================
# Setup / Teardown
# ============================================================

def setup_function(function):
    """Reset estado de dedup antes de cada teste."""
    nt.reset_cooldown()
    nlf.reset_sync_cache()


# ============================================================
# 1) notify_critical — sempre envia (Telegram)
# ============================================================

def test_critical_always_sends_to_telegram():
    """notify_critical deve SEMPRE chegar no Telegram (sem dedup por padrão)."""
    sent = []
    with patch.object(nlf, "_send", side_effect=lambda m: sent.append(m)):
        r1 = nlf.notify_critical("🚨 TRUE ORPHAN: ticket 123")
        r2 = nlf.notify_critical("🚨 TRUE ORPHAN: ticket 123")
    assert r1 is True
    assert r2 is True
    assert len(sent) == 2, "critical deve sempre enviar"


def test_critical_with_key_dedups():
    """notify_critical com key deve dedupar dentro do cooldown."""
    sent = []
    with patch.object(nlf, "_send", side_effect=lambda m: sent.append(m)):
        nlf.notify_critical("msg 1", key="WATCHDOG_ORPHAN:123", cooldown_min=60)
        nlf.notify_critical("msg 2", key="WATCHDOG_ORPHAN:123", cooldown_min=60)
    assert len(sent) == 1, "segunda chamada com mesma key dentro do cooldown deve suprimir"


# ============================================================
# 2) notify_sync_ok — só envia se mudou conteúdo
# ============================================================

def test_sync_ok_silent_when_content_unchanged():
    """sync_ok com MESMO conteúdo em chamadas repetidas → 0 envios."""
    sent = []
    with patch.object(nlf, "_send", side_effect=lambda m: sent.append(m)):
        for _ in range(5):
            nlf.notify_sync_ok("WATCHDOG", "✅ WATCHDOG: OK | 3 posicoes | Equity=R$1.002.981")
    assert len(sent) == 0, "sync_ok repetido com mesmo conteúdo NÃO deve enviar"


def test_sync_ok_sends_when_content_changes():
    """sync_ok com conteúdo DIFERENTE → envia (mudança real)."""
    sent = []
    with patch.object(nlf, "_send", side_effect=lambda m: sent.append(m)):
        nlf.notify_sync_ok("WATCHDOG", "✅ WATCHDOG: OK | 3 posicoes")
        nlf.notify_sync_ok("WATCHDOG", "✅ WATCHDOG: OK | 2 posicoes")  # mudou
    assert len(sent) == 1, "mudança de conteúdo deve gerar 1 envio"


def test_sync_ok_different_categories_are_independent():
    """sync_ok de categorias diferentes (WATCHDOG vs RECONCILE) não compartilham cache."""
    sent = []
    with patch.object(nlf, "_send", side_effect=lambda m: sent.append(m)):
        # Primeira chamada de cada categoria = baseline silencioso
        nlf.notify_sync_ok("WATCHDOG", "msg A")
        nlf.notify_sync_ok("RECONCILE", "msg B")
        # Repetição = silencioso
        nlf.notify_sync_ok("WATCHDOG", "msg A")
        nlf.notify_sync_ok("RECONCILE", "msg B")
        # Mudança em uma categoria = envia 1
        nlf.notify_sync_ok("WATCHDOG", "msg A2")
        # Mudança na outra = envia +1
        nlf.notify_sync_ok("RECONCILE", "msg B2")
    assert len(sent) == 2, f"esperado 2 envios (1 por categoria que mudou), got {len(sent)}"


# ============================================================
# 3) notify_silent — NUNCA Telegram
# ============================================================

def test_silent_never_sends_to_telegram():
    """notify_silent NUNCA deve chegar no Telegram, independente de conteúdo."""
    sent = []
    with patch.object(nlf, "_send", side_effect=lambda m: sent.append(m)):
        for i in range(10):
            nlf.notify_silent(f"[DEBUG] msg {i}")
    assert len(sent) == 0, "silent NUNCA deve enviar pro Telegram"


def test_silent_writes_to_logger():
    """notify_silent deve escrever no logger (log estruturado)."""
    logger = MagicMock()
    with patch.object(nlf, "_logger", logger):
        nlf.notify_silent("[INFO] state file sync: 2462062917")
    # logger.info ou logger.debug deve ter sido chamado
    assert logger.info.called or logger.debug.called, "silent deve logar"


# ============================================================
# 4) Integração com reconcile_with_mt5()
# ============================================================

def test_reconcile_zero_divergences_is_silent():
    """Reconciliação com 0 divergências não deve gerar Telegram."""
    sent = []
    empty_result = {
        "inserted": [], "closed": [], "divergences": [], "skipped": [], "errors": []
    }
    with patch.object(nlf, "_send", side_effect=lambda m: sent.append(m)):
        nlf.notify_reconcile_drift(empty_result, key="RECONCILE:DRIFT")
    assert len(sent) == 0, "reconciliação limpa NÃO deve notificar"


def test_reconcile_with_inserted_sends_critical():
    """Reconciliação que INSERIU posições deve notificar (drift real corrigido)."""
    sent = []
    result = {
        "inserted": [{"ticket": "300", "symbol": "BITM26", "direction": "BUY",
                      "entry_price": 323080.0, "tf": "M5"}],
        "closed": [], "divergences": [], "skipped": [], "errors": [],
    }
    with patch.object(nlf, "_send", side_effect=lambda m: sent.append(m)):
        nlf.notify_reconcile_drift(result, key="RECONCILE:DRIFT:300")
    assert len(sent) == 1
    assert "[RECONCILIADO]" in sent[0]
    assert "BITM26" in sent[0]


def test_reconcile_with_closed_sends_critical():
    """Reconciliação que FECHOU posições sumidas do MT5 deve notificar."""
    sent = []
    result = {
        "inserted": [], "closed": [{"ticket": "200", "symbol": "WDOQ26"}],
        "divergences": [], "skipped": [], "errors": [],
    }
    with patch.object(nlf, "_send", side_effect=lambda m: sent.append(m)):
        nlf.notify_reconcile_drift(result, key="RECONCILE:DRIFT:200")
    assert len(sent) == 1
    assert "fechada" in sent[0].lower()


def test_reconcile_with_divergences_sends_critical():
    """Divergência de preço DEVE notificar (inconsistência entre DB e MT5)."""
    sent = []
    result = {
        "inserted": [], "closed": [],
        "divergences": [{"ticket": "111", "symbol": "WINQ26",
                         "db_entry": 120000, "mt5_entry": 120050, "diff": 50}],
        "skipped": [], "errors": [],
    }
    with patch.object(nlf, "_send", side_effect=lambda m: sent.append(m)):
        nlf.notify_reconcile_drift(result, key="RECONCILE:DRIFT:111")
    assert len(sent) == 1
    assert "divergência" in sent[0].lower() or "divergencia" in sent[0].lower()


# ============================================================
# 5) Cenários integrados do watchdog real
# ============================================================

def test_watchdog_ok_heartbeat_is_silent():
    """Heartbeat 'OK' do watchdog NÃO deve notificar (operação normal)."""
    sent = []
    with patch.object(nlf, "_send", side_effect=lambda m: sent.append(m)):
        # Simula o `format_ok()` que o watchdog emite quando tudo OK
        msg = "✅ WATCHDOG: OK | 3 posicoes | Equity=R$1.002.981"
        nlf.notify_sync_ok("WATCHDOG_HEARTBEAT", msg)
        nlf.notify_sync_ok("WATCHDOG_HEARTBEAT", msg)  # repetido
    assert len(sent) == 0, "heartbeat OK não deve poluir Telegram"


def test_watchdog_state_file_sync_info_is_silent():
    """[INFO] State file sync: X ... NÃO deve notificar (já é o comportamento atual)."""
    sent = []
    with patch.object(nlf, "_send", side_effect=lambda m: sent.append(m)):
        for _ in range(10):
            nlf.notify_silent(
                "[INFO] State file sync: 2462062917 (BITM26) DB trade #1403"
            )
    assert len(sent) == 0


def test_watchdog_true_orphan_is_critical():
    """TRUE ORPHAN (ticket sem DB e sem state) DEVE notificar."""
    sent = []
    with patch.object(nlf, "_send", side_effect=lambda m: sent.append(m)):
        nlf.notify_critical(
            "⚠️ ORFAO: BITM26 BUY 1.0 lots | PnL=R$-1.60",
            key="WATCHDOG_ORPHAN:2462062917",
            cooldown_min=30,
        )
    assert len(sent) == 1
    assert "ORFAO" in sent[0] or "ORFÃO" in sent[0]


# ============================================================
# 6) Smoke test do cenário REAL do Bruno (12:57/12:58)
# ============================================================

def test_bruno_real_scenario_no_spam():
    """
    Cenário EXATO que Bruno reportou às 12:57:58 / 12:58:00:
    - 5 watchdog runs em sequência
    - Cada um detecta 1 sync_fix (BITM26 #1403 missing from state, found in DB)
    - Estado já está OK, sem divergência real

    ESPERADO: 0 mensagens no Telegram (start silent + sync_ok silencioso).
    """
    sent = []
    with patch.object(nlf, "_send", side_effect=lambda m: sent.append(m)):
        for run in range(5):
            # O sync_fix é log de manutenção → silent
            nlf.notify_silent(
                "[SYNC FIX] Ticket 2462062917 (BITM26) missing from state file "
                "but found in DB trade #1403 — not flagging as orphan"
            )
            # O [INFO] State file sync: ... também é silent
            nlf.notify_silent(
                "[INFO] State file sync: 2462062917 (BITM26) DB trade #1403"
            )
            # Heartbeat: start silent, depois suprime repetidos
            nlf.notify_sync_ok(
                "WATCHDOG_HEARTBEAT",
                "✅ WATCHDOG: OK | 3 posicoes | Equity=R$1.002.981"
            )
    assert len(sent) == 0, (
        f"5 runs do watchdog não devem enviar NENHUMA msg pro Telegram "
        f"quando estado está OK. Recebeu {len(sent)}."
    )


def test_real_reconcile_no_drift_does_not_pollute():
    """
    reconcile_with_mt5() com 0 divergências NÃO deve ir pro Telegram.
    Cenário: watchdog rodando a cada 5min, MT5 e DB em sincronia.
    """
    sent = []
    with patch.object(nlf, "_send", side_effect=lambda m: sent.append(m)):
        # 10 reconciliações, todas com resultado vazio
        for _ in range(10):
            result = {
                "inserted": [], "closed": [], "divergences": [],
                "skipped": [], "errors": []
            }
            nlf.notify_reconcile_drift(result, key="RECONCILE:DRIFT")
    assert len(sent) == 0
