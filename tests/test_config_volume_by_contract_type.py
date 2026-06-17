"""
TDD: Volume mínimo por tipo de contrato (cheio vs mini).

Regras B3 (confirmado via TinyFish em 17/06/2026):
- Contratos CHEIOS (IND, DOL): lote mínimo 5 contratos
- Minicontratos (WIN, WDO, BIT, WSP): lote mínimo 1 contrato

Referência:
- https://www.b3.com.br/pt_br/produtos-e-servicos/negociacao/renda-variavel/mini-contrato-ibovespa-indices.htm
- https://www.investidor.b3.com.br/mini-contratos
- https://blog.rico.com.br/mini-contratos
- https://ajuda.nelogica.com.br/portal/pt-br/kb/articles/contratos-101
- https://conteudos.xpi.com.br/mini-contratos/
"""

import json
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / "vt_config.json"

# Mapeamento symbol_root -> tipo de contrato
FULL_CONTRACTS = {"DOL", "IND"}  # contratos cheios (lote mínimo 5)
MINI_CONTRACTS = {"WIN", "WDO", "BIT", "WSP"}  # minicontratos (lote mínimo 1)
MIN_FULL_VOLUME = 5
MIN_MINI_VOLUME = 1


def load_config():
    """Carrega vt_config.json."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def test_dol_is_full_contract_with_min_volume_5():
    """DOL (dólar cheio DOLN26) deve ter volume 5 (lote mínimo B3)."""
    cfg = load_config()
    vol_by_sym = cfg.get("volume_by_symbol", {})

    assert "DOL" in vol_by_sym, "DOL deve estar em volume_by_symbol"
    assert vol_by_sym["DOL"] == 5, (
        f"DOL é contrato CHEIO, lote mínimo B3 = 5 contratos. "
        f"Config atual: {vol_by_sym['DOL']}. "
        f"Volume 1 viola regra B3 e pode causar rejeição de ordens."
    )


def test_ind_is_full_contract_with_min_volume_5():
    """IND (índice cheio INDM26) deve ter volume 5 (lote mínimo B3)."""
    cfg = load_config()
    vol_by_sym = cfg.get("volume_by_symbol", {})

    assert "IND" in vol_by_sym, "IND deve estar em volume_by_symbol"
    assert vol_by_sym["IND"] == 5, (
        f"IND é contrato CHEIO, lote mínimo B3 = 5 contratos. "
        f"Config atual: {vol_by_sym['IND']}. "
        f"Volume 1 viola regra B3 e pode causar rejeição de ordens."
    )


def test_win_is_mini_contract_with_volume_1():
    """WIN (minicontrato WINQ26) deve ter volume 1 (lote mínimo mini)."""
    cfg = load_config()
    vol_by_sym = cfg.get("volume_by_symbol", {})

    assert "WIN" in vol_by_sym, "WIN deve estar em volume_by_symbol"
    assert vol_by_sym["WIN"] == 1, (
        f"WIN é MINICONTRATO, lote mínimo B3 = 1 contrato. "
        f"Config atual: {vol_by_sym['WIN']}."
    )


def test_wdo_is_mini_contract_with_volume_1():
    """WDO (minicontrato WDON26) deve ter volume 1 (lote mínimo mini)."""
    cfg = load_config()
    vol_by_sym = cfg.get("volume_by_symbol", {})

    assert "WDO" in vol_by_sym, "WDO deve estar em volume_by_symbol"
    assert vol_by_sym["WDO"] == 1, (
        f"WDO é MINICONTRATO, lote mínimo B3 = 1 contrato. "
        f"Config atual: {vol_by_sym['WDO']}."
    )


def test_bit_is_mini_contract_with_volume_1():
    """BIT (minicontrato BITM26) deve ter volume 1 (lote mínimo mini)."""
    cfg = load_config()
    vol_by_sym = cfg.get("volume_by_symbol", {})

    assert "BIT" in vol_by_sym, "BIT deve estar em volume_by_symbol"
    assert vol_by_sym["BIT"] == 1, (
        f"BIT é MINICONTRATO, lote mínimo B3 = 1 contrato. "
        f"Config atual: {vol_by_sym['BIT']}."
    )


def test_wsp_is_mini_contract_with_volume_1():
    """WSP (minicontrato WSPM26) deve ter volume 1 (lote mínimo mini)."""
    cfg = load_config()
    vol_by_sym = cfg.get("volume_by_symbol", {})

    assert "WSP" in vol_by_sym, "WSP deve estar em volume_by_symbol"
    assert vol_by_sym["WSP"] == 1, (
        f"WSP é MINICONTRATO, lote mínimo B3 = 1 contrato. "
        f"Config atual: {vol_by_sym['WSP']}."
    )


def test_all_symbols_have_volume_configured():
    """Todos os 6 symbols devem ter volume_by_symbol explícito."""
    cfg = load_config()
    vol_by_sym = cfg.get("volume_by_symbol", {})
    expected_symbols = {"WIN", "WDO", "BIT", "DOL", "IND", "WSP"}

    assert set(vol_by_sym.keys()) == expected_symbols, (
        f"volume_by_symbol deve cobrir todos os 6 symbols. "
        f"Esperado: {expected_symbols}, Atual: {set(vol_by_sym.keys())}"
    )


def test_resolved_symbols_match_full_vs_mini():
    """Os resolved_symbols devem corresponder ao tipo (cheio vs mini)."""
    cfg = load_config()
    resolved = cfg.get("resolved_symbols", {})

    # DOLN26 = dólar cheio (6 letras = N26)
    # INDM26 = índice cheio
    # WINQ26 = mini índice
    # WDON26 = mini dólar
    # BITM26 = mini bitcoin
    # WSPM26 = mini S&P

    full_expected = {"DOL": "DOLN26", "IND": "INDM26"}
    mini_expected = {"WIN": "WINQ26", "WDO": "WDON26", "BIT": "BITM26", "WSP": "WSPM26"}

    for sym, expected in full_expected.items():
        assert resolved.get(sym) == expected, (
            f"{sym} deve ser contrato CHEIO ({expected}), "
            f"atual: {resolved.get(sym)}"
        )

    for sym, expected in mini_expected.items():
        assert resolved.get(sym) == expected, (
            f"{sym} deve ser MINICONTRATO ({expected}), "
            f"atual: {resolved.get(sym)}"
        )
