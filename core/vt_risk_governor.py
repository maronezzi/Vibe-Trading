# -*- coding: utf-8 -*-
"""
vt_risk_governor — governador de risco por símbolo-root (Wave 880.II, 26/08/2026)

Incidente 26/08 (WDOU26): 3 pares (M15/M30/H1) entraram SELL no mesmo contrato
em 9 minutos; a conta é NETTING, então virou UMA posição de 4 contratos e o SL
fechou tudo de uma vez: -R$285 num único stop, estourando o stop diário de
WDO (-R$250) no primeiro trade do dia. Cada par "achava" que arriscava ~R$50.

Este módulo fecha o buraco: antes de enviar uma nova entrada, soma o pior caso
de risco em aberto do símbolo-root (todas as posições do bot naquele contrato)
com o risco da nova entrada. Se a soma passar do orçamento diário do símbolo
(|max_daily_loss_by_symbol[root]|, com buffer de slippage), a entrada é
BLOQUEADA.

Regras:
- Só conta posições do bot (magic 555501 + comment VibeTrading) do mesmo root.
- Entrada na direção OPOSTA à exposição líquida REDUZ risco — não é bloqueada.
- Posição sem SL conta como orçamento inteiro consumido (conservador).
- Fail-open: qualquer erro interno NÃO bloqueia a entrada (o governador é
  defesa extra, não caminho crítico). Erros são reportados no retorno.
- Kill-switch de emergência: env VT_RISK_GOVERNOR=0 desativa (ou
  execution_guards.risk_budget_enabled=false no config).

Módulo PURO (sem MT5, sem DB) — testável hermeticamente. O daemon passa o
snapshot de posições (broker truth) e o config.
"""

from __future__ import annotations

import os

# Fallbacks espelho de contract_specs / _point_map do autotrader (só usados
# se o config não tiver a spec do ativo)
_MULT_FALLBACK = {"WIN": 0.2, "WDO": 10.0, "BIT": 0.01, "WSP": 0.01,
                  "DOL": 1.0, "IND": 1.0}
_POINT_MAP = {"WIN": 1.0, "WDO": 0.001, "BIT": 0.01, "WSP": 0.01,
              "DOL": 0.001, "IND": 1.0}

VT_BOT_MAGIC = 555501

# Buffer de segurança sobre o risco teórico (slippage de execução do SL —
# o stop nem sempre executa exatamente no preço). Default 25%.
DEFAULT_RISK_BUFFER = 0.25


def symbol_root(symbol: str) -> str:
    """Extrai o root do contrato resolvido (ex: 'WDOU26' → 'WDO')."""
    for r in ("WIN", "WDO", "BIT", "WSP", "DOL", "IND"):
        if symbol and symbol.upper().startswith(r):
            return r
    return (symbol or "")[:3].upper()


def _mult_for(root: str, config: dict) -> float:
    specs = (config or {}).get("contract_specs", {}) or {}
    spec = specs.get(f"{root}$") or specs.get(root) or {}
    try:
        m = float(spec.get("mult", 0) or 0)
        if m > 0:
            return m
    except (TypeError, ValueError):
        pass
    return _MULT_FALLBACK.get(root, 1.0)


def _point_for(root: str) -> float:
    return _POINT_MAP.get(root, 1.0)


def _is_bot_position(p: dict) -> bool:
    try:
        if int(p.get("magic", 0) or 0) != VT_BOT_MAGIC:
            return False
    except (TypeError, ValueError):
        return False
    return (p.get("comment") or "").strip() == "VibeTrading"


def net_exposure(open_positions: list, root: str) -> float:
    """Exposição líquida do root em contratos (BUY positivo, SELL negativo).

    Sob netting, é o volume real da posição consolidada — é ela que o broker
    fecha no SL.
    """
    net = 0.0
    for p in open_positions or []:
        if not isinstance(p, dict) or not _is_bot_position(p):
            continue
        if symbol_root(p.get("symbol", "")) != root:
            continue
        vol = float(p.get("volume", 0) or 0)
        ptype = p.get("type", p.get("type_time", 0))
        # MT5: position type 0 = BUY, 1 = SELL (daemon normaliza em 'type')
        sign = 1.0 if ptype in (0, "BUY", "buy") else -1.0
        net += sign * vol
    return net


def open_worst_case_risk(open_positions: list, root: str, config: dict,
                         budget: float) -> float:
    """Pior caso de perda (R$) das posições abertas do root se todos os SL
    executarem. Posição sem SL = orçamento inteiro (conservador).
    """
    mult = _mult_for(root, config)
    total = 0.0
    for p in open_positions or []:
        if not isinstance(p, dict) or not _is_bot_position(p):
            continue
        if symbol_root(p.get("symbol", "")) != root:
            continue
        vol = float(p.get("volume", 0) or 0)
        entry = float(p.get("price_open", p.get("price", 0)) or 0)
        sl = float(p.get("sl", 0) or 0)
        if vol <= 0:
            continue
        if entry <= 0 or sl <= 0:
            total += budget
            continue
        total += abs(entry - sl) * mult * vol
    return total


def check_entry_risk_budget(symbol: str, direction: str, sl_pts: float,
                            volume: float, config: dict,
                            open_positions: list) -> dict:
    """Decide se a nova entrada cabe no orçamento de risco diário do root.

    Args:
        symbol: contrato MT5 resolvido (ex: "WDOU26").
        direction: "BUY" | "SELL".
        sl_pts: distância do SL em pontos MT5 (unidade do order_send).
        volume: contratos da nova entrada.
        config: CONFIG do daemon (lê max_daily_loss_by_symbol,
            contract_specs, execution_guards).
        open_positions: snapshot broker-truth de posições (lista de dicts
            do status() do orchestrator).

    Returns:
        dict {ok: bool, reason: str, detail: str, budget, open_risk,
        new_risk, net_exp, positions: [...]} — `positions` são as posições
        do bot no MESMO contrato (o caller reusa p/ restaurar SL apertado).
    """
    out = {"ok": True, "reason": "", "detail": "",
           "budget": 0.0, "open_risk": 0.0, "new_risk": 0.0,
           "net_exp": 0.0, "positions": []}
    try:
        root = symbol_root(symbol)
        if os.environ.get("VT_RISK_GOVERNOR", "1") != "1":
            out["detail"] = "governador desativado por env"
            return out
        guards = (config or {}).get("execution_guards", {}) or {}
        if guards.get("risk_budget_enabled") is False:
            out["detail"] = "governador desativado por config"
            return out

        limits = (config or {}).get("max_daily_loss_by_symbol", {}) or {}
        try:
            budget = abs(float(limits.get(root, 0) or 0))
        except (TypeError, ValueError):
            budget = 0.0
        if budget <= 0:
            out["detail"] = f"sem max_daily_loss_by_symbol p/ {root} — guard off"
            return out

        mine = [p for p in (open_positions or [])
                if isinstance(p, dict) and _is_bot_position(p)
                and symbol_root(p.get("symbol", "")) == root]
        out["positions"] = mine

        net = net_exposure(open_positions, root)
        out["net_exp"] = net
        dir_sign = 1.0 if str(direction).upper() == "BUY" else -1.0
        # Entrada que REDUZ a exposição líquida é hedge sob netting — libera.
        if mine and net != 0 and (dir_sign * net) < 0:
            out["detail"] = (f"entrada {direction} reduz exposição líquida "
                             f"{net:+.1f} contratos — liberada")
            return out

        mult = _mult_for(root, config)
        point = _point_for(root)
        try:
            buffer = float(guards.get("risk_buffer", DEFAULT_RISK_BUFFER))
        except (TypeError, ValueError):
            buffer = DEFAULT_RISK_BUFFER

        open_risk = open_worst_case_risk(open_positions, root, config, budget)
        new_risk = abs(float(sl_pts)) * point * mult * max(float(volume), 0.0)
        budget_eff = budget / (1.0 + max(buffer, 0.0))
        out["budget"] = budget
        out["open_risk"] = round(open_risk, 2)
        out["new_risk"] = round(new_risk, 2)

        if open_risk + new_risk > budget_eff:
            out["ok"] = False
            out["reason"] = "RISK_BUDGET"
            out["detail"] = (
                f"risco em aberto R${open_risk:.0f} + novo R${new_risk:.0f} "
                f"> orçamento efetivo R${budget_eff:.0f} "
                f"(stop diário {root} -R${budget:.0f}, buffer {buffer:.0%})"
            )
        return out
    except Exception as e:  # fail-open: governador nunca segura entrada
        out["ok"] = True
        out["detail"] = f"fail-open ({type(e).__name__}: {e})"
        return out


def should_restore_prev_sl(direction: str, prev_sl: float, new_sl: float) -> bool:
    """Política tightest-SL-wins: se a entrada nova LARGOU o SL da posição
    consolidada (last-writer-wins do netting), o SL anterior mais apertado
    deve ser restaurado.

    SELL: SL fica ACIMA do preço — mais apertado é o MENOR.
    BUY:  SL fica ABAIXO do preço — mais apertado é o MAIOR.
    """
    try:
        prev_sl = float(prev_sl or 0)
        new_sl = float(new_sl or 0)
        if prev_sl <= 0 or new_sl <= 0:
            return False
        if str(direction).upper() == "SELL":
            return prev_sl < new_sl
        return prev_sl > new_sl
    except (TypeError, ValueError):
        return False
