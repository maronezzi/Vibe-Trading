# -*- coding: utf-8 -*-
"""
vt_netting — atribuição de PnL de posição netting consolidada (Wave 880.II)

A B3 é NETTING: entradas de vários pares (ex: WDO_M15 + WDO_M30 + WDO_H1)
no mesmo contrato viram UMA posição com UM SL. Incidente 26/08: o reconcile
marcava as sub-entradas como GHOST (PnL 0) e lançava a perda inteira da
posição consolidada numa única linha — estatística por par distorcida para
todo consumidor (AGI live_reality, risk_calibrator, relatórios).

Este módulo tem a matemática PURA do split: dado o deal OUT do broker
(preço/profit totais) e as sub-entradas, cada linha recebe o PnL exato da
sua parcela (preço de saída × distância da sua entrada), e a linha "pai"
(a cujo ticket pertence a posição) recebe o resíduo para que a SOMA das
linhas seja igual ao broker-truth (slippage/comissões ficam no pai).

Módulo PURO (sem MT5, sem DB) — o daemon (vt_autotrader.py) aplica os
SQLs. Idempotente por construção: só settling de linhas ainda abertas.
"""

from __future__ import annotations

# Fallback espelho de contract_specs (usado só se a linha não tiver
# multiplier gravado)
_MULT_FALLBACK = {"WIN": 0.2, "WDO": 10.0, "BIT": 0.01, "WSP": 0.01,
                  "DOL": 1.0, "IND": 1.0}


def symbol_root(symbol: str) -> str:
    for r in ("WIN", "WDO", "BIT", "WSP", "DOL", "IND"):
        if symbol and symbol.upper().startswith(r):
            return r
    return (symbol or "")[:3].upper()


def _dir_sign(direction) -> float:
    return 1.0 if str(direction or "").upper() == "BUY" else -1.0


def settle_netting_group(members: list[dict], out_deal: dict) -> list[dict]:
    """Reparte o PnL do deal OUT entre as sub-entradas da posição netting.

    Args:
        members: sub-entradas — cada dict com:
            trade_log_id (int|None), ticket (str), direction, entry_price,
            volume, multiplier (fallback: _MULT_FALLBACK pelo symbol),
            symbol, fees (opcional, default 0).
        out_deal: broker truth do fechamento — dict com:
            price (exit), profit, commission, swap, fee (totais da posição),
            position_ticket (ticket da posição consolidada), time.

    Returns:
        Lista de updates no formato:
            {trade_log_id, exit_price, gross_pnl, net_pnl, is_parent, note}
        O pai (ticket == position_ticket, ou o primeiro membro) recebe o
        resíduo: net_pai = broker_net − Σ(net_filhos). Linhas sem
        trade_log_id não geram update (mas contam no resíduo do pai como
        filhos "sem linha" — PnL delas vai pro pai, conservador).

    Raise:
        ValueError se out_deal sem preço válido ou members vazio — caller
        cai no caminho legado (fail-safe).
    """
    exit_price = float(out_deal.get("price", 0) or 0)
    if exit_price <= 0 or not members:
        raise ValueError("out_deal sem preço válido ou members vazio")

    broker_net = (float(out_deal.get("profit", 0) or 0)
                  + float(out_deal.get("commission", 0) or 0)
                  + float(out_deal.get("swap", 0) or 0)
                  + float(out_deal.get("fee", 0) or 0))
    pos_ticket = str(out_deal.get("position_ticket", "") or "")

    updates = []
    parent_idx = None
    for i, m in enumerate(members):
        entry = float(m.get("entry_price", 0) or 0)
        vol = float(m.get("volume", 0) or 0)
        if entry <= 0 or vol <= 0:
            continue
        mult = float(m.get("multiplier", 0) or 0)
        if mult <= 0:
            mult = _MULT_FALLBACK.get(symbol_root(m.get("symbol", "")), 1.0)
        gross = (exit_price - entry) * vol * mult * _dir_sign(m.get("direction"))
        fees = float(m.get("fees", 0) or 0)
        net = gross - fees
        is_parent = str(m.get("ticket", "")) == pos_ticket
        if is_parent:
            parent_idx = i
        updates.append({
            "trade_log_id": m.get("trade_log_id"),
            "ticket": str(m.get("ticket", "")),
            "exit_price": exit_price,
            "gross_pnl": round(gross, 2),
            "net_pnl": round(net, 2),
            "is_parent": is_parent,
            "note": "",
        })
    if not updates:
        raise ValueError("nenhum membro com dados válidos")

    # Pai = linha cujo ticket é o da posição; senão, o primeiro (a posição
    # consolidada nasce no ticket da primeira entrada).
    if parent_idx is None:
        parent_idx = 0
        updates[0]["is_parent"] = True

    # Filhos SEM trade_log_id: PnL deles não tem linha própria — soma no
    # pai (conservador, mantém Σ linhas == broker).
    orphan_net = sum(u["net_pnl"] for u in updates if not u["trade_log_id"])
    children_net = sum(u["net_pnl"] for i, u in enumerate(updates)
                       if i != parent_idx and u["trade_log_id"])
    parent_net = broker_net - children_net - orphan_net
    parent = updates[parent_idx]
    parent["net_pnl"] = round(parent_net, 2)
    parent["gross_pnl"] = round(parent_net
                                + float(members[parent_idx].get("fees", 0) or 0), 2)
    parent["note"] = (f"NETTING_PARENT | broker_net={broker_net:.2f} "
                      f"residual após {len(updates) - 1} filho(s)")
    for i, u in enumerate(updates):
        if i != parent_idx:
            u["note"] = "NETTING_CHILD_SETTLED | split por preço de saída"
    return updates
