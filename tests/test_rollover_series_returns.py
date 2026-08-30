# -*- coding: utf-8 -*-
"""Tests Wave 883.B5 (30/08) — series_divergence por MOVIMENTO (retorno diário).

Regressão do bloqueio eterno do WIN: a perpétua costurada carrega basis de
rolagem estrutural (~2,2% no nível) — o gate antigo comparava NÍVEL e
bloqueava todo o símbolo para sempre. Agora compara retorno diário mediano:
basis constante some da conta; divergência real de movimento continua
bloqueando. Freeze/grace intocados.

Hermético: DataFrames sintéticos em memória. Nenhum MT5/Wine/DB.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from optimization.agi_v4 import rollover_guard as rg  # noqa: E402


def _df(closes_by_day: dict) -> pd.DataFrame:
    """DataFrame M15-like: 2 barras/dia, index datetime, coluna close.

    closes_by_day: {"2026-08-25": [c1, c2], ...}
    """
    rows = []
    for day, closes in closes_by_day.items():
        d = datetime.strptime(day, "%Y-%m-%d")
        for k, c in enumerate(closes):
            rows.append((d.replace(hour=10 + k, minute=0), c))
    rows.sort(key=lambda r: r[0])
    return pd.DataFrame({"close": [c for _, c in rows]},
                        index=pd.DatetimeIndex([t for t, _ in rows]))


def test_daily_returns_basico():
    df = _df({"2026-08-25": [100.0, 101.0], "2026-08-26": [103.0, 102.96]})
    r = rg._daily_returns(df)
    # último close de cada dia: 101.0 → 102.96 = +1,94%
    assert r[pd.Timestamp("2026-08-26").date()] == pytest.approx(0.0194, abs=1e-4)
    assert pd.Timestamp("2026-08-25").date() not in r  # 1º dia não tem retorno


def test_basis_constante_nao_diverge():
    # mesmo mercado, níveis com basis de ~2,19% (caso real WIN$ vs WINZ26)
    base = {"2026-08-25": [100.0, 101.0], "2026-08-26": [103.0, 102.0],
            "2026-08-27": [104.0, 106.0], "2026-08-28": [105.0, 107.0]}
    dperp = _df(base)
    dlive = _df({d: [c * 1.0219 for c in cs] for d, cs in base.items()})
    e = rg._series_comparison(dperp, dlive, "WINZ26")
    assert e["divergent"] is False
    assert e["diff_pct"] == pytest.approx(2.19, abs=0.05)  # nível: informativo
    assert e["n_days"] == 3


def test_movimento_divergente_bloqueia():
    # perpétua sobe 2%/dia, contrato CAI 2%/dia → mediana >> 1%
    dperp = _df({"2026-08-25": [100.0, 100.0], "2026-08-26": [100.0, 102.0],
                 "2026-08-27": [102.0, 104.04], "2026-08-28": [104.04, 106.12]})
    dlive = _df({"2026-08-25": [5000.0, 5000.0], "2026-08-26": [5000.0, 4900.0],
                 "2026-08-27": [4900.0, 4802.0], "2026-08-28": [4802.0, 4705.96]})
    e = rg._series_comparison(dperp, dlive, "WRONG")
    assert e["divergent"] is True
    assert e["ret_diff_med_pct"] > 1.0


def test_poucos_dias_sem_opiniao():
    base = {"2026-08-25": [100.0, 101.0], "2026-08-26": [103.0, 105.0]}
    dperp = _df(base)
    dlive = _df({"2026-08-25": [50.0, 10.0], "2026-08-26": [200.0, 20.0]})
    e = rg._series_comparison(dperp, dlive, "X")
    assert e["divergent"] is False  # 1 dia comum < mínimo → fail-open
    assert e["n_days"] is None


def test_df_none_nao_diverge():
    e = rg._series_comparison(None, _df({"2026-08-25": [1.0, 1.0]}), "X")
    assert e["divergent"] is False and e["perp_last"] is None
