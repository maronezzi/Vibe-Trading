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

────────────────────────────────────────────────────────────────────────────────
2026-06-19 — Bruno Maronezzi decidiu tirar IND (Índice Cheio) e DOL (Dólar
Cheio) de circulação. Apenas os 4 minicontratos são operados a partir de hoje.

Este arquivo continua documentando a regra B3 (cheio = 5, mini = 1) como
referência viva, mas o ASSERT mudou: o que era "DOL/IND deve ter volume 5"
agora é "DOL/IND não devem estar na config ativa" (fora de circulação).
────────────────────────────────────────────────────────────────────────────────
"""

import json
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / "vt_config.json"

# Mapeamento symbol_root -> tipo de contrato (referência, não assertado)
FULL_CONTRACTS = {"DOL", "IND"}    # contratos cheios (lote mínimo 5)  — fora de circulação desde 19/06/2026
MINI_CONTRACTS = {"WIN", "WDO", "BIT", "WSP"}  # minicontratos (lote mínimo 1)
MIN_FULL_VOLUME = 5
MIN_MINI_VOLUME = 1


def load_config():
    """Carrega vt_config.json."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ─── IND e DOL: agora asserts "fora de circulação" ────────────────────────────

def test_dol_out_of_circulation():
    """DOL (dólar cheio) foi removido por decisão do Bruno em 2026-06-19.

    Mantemos o teste vivo para garantir que ninguém reintroduza DOL sem
    passar pela revisão explícita. Se DOL voltar, basta mudar o config
    e este teste falhará — alerta intencional.
    """
    cfg = load_config()
    assert "DOL" not in cfg.get("symbols", []), (
        "DOL foi removido por decisão do Bruno em 19/06/2026. "
        "Para reativar, confirme com ele e ajuste este teste."
    )
    assert "DOL" not in cfg.get("volume_by_symbol", {}), (
        "DOL fora de circulação — não deve ter volume configurado."
    )


def test_ind_out_of_circulation():
    """IND (índice cheio) foi removido por decisão do Bruno em 2026-06-19."""
    cfg = load_config()
    assert "IND" not in cfg.get("symbols", []), (
        "IND foi removido por decisão do Bruno em 19/06/2026. "
        "Para reativar, confirme com ele e ajuste este teste."
    )
    assert "IND" not in cfg.get("volume_by_symbol", {}), (
        "IND fora de circulação — não deve ter volume configurado."
    )


# ─── Minis: regras B3 continuam valendo ───────────────────────────────────────

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


# ─── Cobertura total ──────────────────────────────────────────────────────────

def test_all_active_symbols_have_volume_configured():
    """Todos os symbols ativos (4 minis) devem ter volume_by_symbol explícito."""
    cfg = load_config()
    vol_by_sym = cfg.get("volume_by_symbol", {})
    active = set(cfg.get("symbols", []))
    expected = {"WIN", "WDO", "BIT", "WSP"}

    assert active == expected, (
        f"símbolos ativos esperados: {expected}, atual: {active}"
    )
    assert set(vol_by_sym.keys()) == expected, (
        f"volume_by_symbol deve cobrir apenas os 4 minis ativos. "
        f"Esperado: {expected}, Atual: {set(vol_by_sym.keys())}"
    )


def test_resolved_symbols_only_minis():
    """Apenas minis devem ter resolved_symbols (cheios fora de circulação)."""
    cfg = load_config()
    resolved = cfg.get("resolved_symbols", {})

    mini_expected = {"WIN": "WINQ26", "WDO": "WDON26", "BIT": "BITM26", "WSP": "WSPM26"}

    for sym, expected in mini_expected.items():
        assert resolved.get(sym) == expected, (
            f"{sym} deve ser MINICONTRATO ({expected}), "
            f"atual: {resolved.get(sym)}"
        )

    # E nenhum contrato cheio presente
    assert "DOL" not in resolved, "DOL fora de circulação — não deve ter resolved_symbol"
    assert "IND" not in resolved, "IND fora de circulação — não deve ter resolved_symbol"