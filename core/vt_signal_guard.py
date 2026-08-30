# -*- coding: utf-8 -*-
"""
vt_signal_guard — guarda de sinal expirado em TFs altos (Wave 883.B4, 29/08).

Problema (auditoria 29/08/2026): o daemon recalcula indicadores com a barra
EM FORMAÇÃO, então em M30/H1 o estado costuma "acender" bem depois do
fechamento do candle-sinal — H1 p50 = 22min DENTRO da barra seguinte. Evidência
do custo: entradas no 1º quarto da barra fizeram +0,24R / 59% WR; depois
disso, -0,09R / 45% WR; e o SLIPPAGE-GUARD descartou 71 sinais em 15 dias
por preço andado >125pts contra o sinal ANTES da ordem.

A guarda corta a causa (entrar com sinal velho) antes de gastar ciclo:
se a fração decorrida da barra atual — a barra-sinal fechou quando a atual
abriu — passar do limite, a entrada é descartada com log [SINAL-EXPIRADO].

Fuso: os timestamps de barra do MT5 são alinhados a múltiplos do TF e o
offset servidor↔local da B3 é número inteiro de horas, logo
``(agora - abertura_da_barra_atual) % 3600`` = decorrido real para TFs <= H1,
sem precisar conhecer o offset. Limitação documentada: se algum broker
usar fuso com meia-hora, a guarda fica ±30min mais estrita para M30 —
direção fail-safe (pular entrada, nunca forçar).

Kill-switch: ``VT_SIGNAL_AGE_GUARD=0`` desliga sem restart do config.
Escopo (decisão Bruno 29/08 — "urgente"): apenas M30 e H1; M5/M15 seguem
como estão (a latência mediana de M15 ~2min já é intra-loop).

Módulo PURO (sem MT5/DB/config). O daemon só consulta.
"""
from __future__ import annotations

import os
import time

# TF → fração máxima da barra que pode ter decorrido quando o sinal é visto.
_SIGNAL_AGE_MAX_FRACTION = {"M30": 0.25, "H1": 0.25}
_TF_SECONDS = {"M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}


def enabled() -> bool:
    return os.environ.get("VT_SIGNAL_AGE_GUARD", "1") == "1"


def elapsed_in_current_bar(bars: list, now: float | None = None) -> float | None:
    """Segundos decorridos desde a abertura da barra em formação (bars[0]).

    Retorna None se não der para calcular (sem barras/sem time). O módulo
    3600 elimina o offset de fuso servidor↔local (inteiro em horas na B3).
    """
    if not bars:
        return None
    t0 = bars[0].get("time") if isinstance(bars[0], dict) else None
    if not t0:
        return None
    try:
        return ((now if now is not None else time.time()) - float(t0)) % 3600.0
    except (TypeError, ValueError):
        return None


def signal_age_ok(tf: str, bars: list, now: float | None = None) -> bool:
    """True se o sinal do TF ainda está fresco (ou TF fora do escopo/desligado)."""
    if not enabled():
        return True
    frac = _SIGNAL_AGE_MAX_FRACTION.get(tf)
    if frac is None:
        return True
    elapsed = elapsed_in_current_bar(bars, now=now)
    if elapsed is None:
        return True  # sem dado → não bloqueia (fail-open do filtro)
    return elapsed <= frac * _TF_SECONDS.get(tf, 3600)


def max_age_seconds(tf: str) -> float | None:
    """Limite em segundos do TF (para log); None se fora do escopo."""
    frac = _SIGNAL_AGE_MAX_FRACTION.get(tf)
    return frac * _TF_SECONDS.get(tf, 0) if frac else None
