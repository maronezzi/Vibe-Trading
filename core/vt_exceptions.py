"""
core/vt_exceptions.py
=====================
Exceções de domínio e constantes nomeadas para o path de ordens (Fase 3).

Por que um módulo separado?
--------------------------
Antes da Fase 3 só existia UMA exceção customizada no repo (`ConfigLockError`
em vt_config_loader.py). Não havia `MissingStopLossError`, `OrderNotConfirmedError`
nem `OrderRejectedError`, e nenhuma constante nomeada para os retcodes do MT5
(10008/10009 apareciam como literais mágicos). Este módulo centraliza:

  1. As 3 exceções de ordem (Lei 3 — SL obrigatório; Lei 4 — Garantia MT5)
  2. Constantes nomeadas de retcode MT5 (fim dos magic numbers 10008/10009)
  3. Helper para construir o dict de erro no contrato existente do orchestrator

IMPORTANTE — decisão de design (defende o bot ao vivo)
-------------------------------------------------------
O handoff original pedia que `buy()/sell()` **levantassem** essas exceções.
Porém a auditoria do path de ordens (2026-07-01) mostrou que:
  - `safe_buy/safe_sell` NÃO envolvem a chamada buy/sell em try/except
  - `_execute_entry` NÃO envolve safe_buy/safe_sell
  - Os 4 callers de `_execute_entry` ignoram o retorno E não capturam exceções
→ propagar uma exceção de buy() derrubaria o tick do autotrader AO VIVO.

Solução adotada: as exceções EXISTEM (para testes, validações explícitas e
migração gradual futura), mas `buy()/sell()` devolvem o **dict de erro no
contrato existente** (`{"status": "BLOCKED", "reason": "MISSING_STOP_LOSS", ...}`)
via `error_dict()`, que safe_buy/safe_sell já sabem classificar. Assim a defesa
da Lei 3/Lei 4 é garantida SEM quebrar o contrato nem o bot.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


# ── Exceções de domínio (Lei 3 + Lei 4) ─────────────────────────────────────
class OrderError(RuntimeError):
    """Base para erros do path de ordens."""


class MissingStopLossError(OrderError):
    """Lei 3: toda ordem DEVE ter SL. Raised quando sl <= 0 ou None.

    O handoff exige validação DENTRO de buy/sell (não confiar no caller).
    No contrato dict (usado em produção), vira reason='MISSING_STOP_LOSS'.
    """


class OrderNotConfirmedError(OrderError):
    """Lei 4: MT5 não confirmou a ordem (sem ticket válido).

    O handoff exige que ordem só conte como "aberta" se MT5 retornar ticket.
    No contrato dict, vira reason='NOT_CONFIRMED'.
    """


class OrderRejectedError(OrderError):
    """Lei 4: MT5 rejeitou a ordem (retcode != DONE/PLACED).

    No contrato dict, vira reason='REJECTED_BY_RETCODE'.
    """


# ── Constantes de retcode MT5 (fim dos magic numbers) ───────────────────────
# Fonte: MetaTrader5 docs. Antes da Fase 3, 10009 aparecia só via
# mt5.TRADE_RETCODE_DONE no executor Wine-side, e 10008 não era tratado
# (bug latente: ordem PLACED caía no bucket REJECTED → orphan+duplicate).
TRADE_RETCODE_DONE = 10009       # request completed
TRADE_RETCODE_PLACED = 10008     # order placed
# Retcodes considerados "aceitos" pelo broker (ordem efetivamente registrada)
ACCEPTED_RETCODES = frozenset({TRADE_RETCODE_DONE, TRADE_RETCODE_PLACED})

# Magic number canônico do VibeTrading (555501).
# Antes definido em 2 lugares (vt_truth.py:73 MAGIC_VIBETRADING, vt_autotrader.py:1638
# VT_BOT_MAGIC) + 8 literais espalhados. Aqui fica a fonte única referenciável.
MAGIC_VIBETRADING = 555501


# ── Razões de bloqueio (reason no dict de erro) ─────────────────────────────
REASON_MISSING_STOP_LOSS = "MISSING_STOP_LOSS"
REASON_NOT_CONFIRMED = "NOT_CONFIRMED"
REASON_REJECTED_BY_RETCODE = "REJECTED_BY_RETCODE"


def error_dict(reason: str, detail: str = "",
               **extra: Any) -> Dict[str, Any]:
    """Constrói um dict de erro no contrato do orchestrator.

    O orchestrator (`buy`/`sell`) e safe_buy/safe_sell usam dicts com chave
    `status`. Um erro de validação de SL/ticket vira:
        {"status": "BLOCKED", "reason": "MISSING_STOP_LOSS", "detail": "..."}

    safe_buy/safe_sell já tratam status != "FILLED" via _classify_error, então
    BLOCKED entra no fluxo de retry/abort sem propagar exceção (não derruba o
    bot ao vivo). A exceção correspondente pode ser levantada por quem quiser
    validar explicitamente (testes, scripts de diagnóstico).
    """
    d: Dict[str, Any] = {"status": "BLOCKED", "reason": reason}
    if detail:
        d["detail"] = detail
    d["ticket"] = 0          # consistência: 0 = não aberto
    d.update(extra)
    return d


def from_exception(exc: OrderError, **extra: Any) -> Dict[str, Any]:
    """Converte uma OrderError no dict de erro correspondente.

    Útil para testes: permite validar tanto a exceção quanto o dict sem duplicar
    lógica. Mapeamento:
        MissingStopLossError → MISSING_STOP_LOSS
        OrderNotConfirmedError → NOT_CONFIRMED
        OrderRejectedError    → REJECTED_BY_RETCODE
    """
    mapping = {
        MissingStopLossError: REASON_MISSING_STOP_LOSS,
        OrderNotConfirmedError: REASON_NOT_CONFIRMED,
        OrderRejectedError: REASON_REJECTED_BY_RETCODE,
    }
    for cls, reason in mapping.items():
        if isinstance(exc, cls):
            return error_dict(reason, detail=str(exc), **extra)
    return error_dict("UNKNOWN_ERROR", detail=str(exc), **extra)
