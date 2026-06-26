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
    """IND M15 reativado Wave 9 (BOLLINGER edge, PnL HOJE +R$ 609, WR 80%).

    Volume deve estar configurado para IND.
    """
    cfg = load_config()
    assert "IND" in cfg.get("symbols", []), (
        "IND_M15 BOLLINGER reativado Wave 9 (deve estar em symbols)"
    )
    assert "IND" in cfg.get("volume_by_symbol", {}), (
        "IND reativado — deve ter volume configurado"
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
    """Cada symbol ativo deve ter volume_by_symbol configurado.

    Wave 9 (2026-06-26): IND reativado (M15 BOLLINGER edge de elite).
    """
    cfg = load_config()
    vol_by_sym = cfg.get("volume_by_symbol", {})
    active = set(cfg.get("symbols", []))
    expected = {"WIN", "WDO", "BIT", "WSP", "IND"}  # Wave 9: IND reativado

    assert active == expected, (
        f"símbolos ativos esperados: {expected}, atual: {active}"
    )
    assert set(vol_by_sym.keys()) == expected, (
        f"volume_by_symbol deve cobrir todos os símbolos ativos. "
        f"Esperado: {expected}, Atual: {set(vol_by_sym.keys())}"
    )


def test_resolved_symbols_only_minis():
    """Apenas WIN/WDO/BIT/WSP e IND devem ter resolved_symbols.

    Wave 9: IND reativado com M15 BOLLINGER.
    Não hardcode o contrato exato (WINQ26/INDM26/...) — ele muda a cada roll
    mensal. Checa apenas que os 5 symbols têm um resolved e cada um começa com
    a raiz correta.
    """
    cfg = load_config()
    resolved = cfg.get("resolved_symbols", {})

    # Wave 9: IND reativado (4 minis + IND)
    expected_roots = {"WIN", "WDO", "BIT", "WSP", "IND"}
    assert set(resolved.keys()) == expected_roots, (
        f"resolved_symbols deve conter os 4 minis + IND (Wave 9). "
        f"Esperado: {expected_roots}, Atual: {set(resolved.keys())}"
    )
    for root in expected_roots:
        contract = resolved.get(root)
        assert contract and contract.startswith(root), (
            f"{root} resolved deveria começar com '{root}', "
            f"atual: {contract!r}"
        )

    # E nenhum contrato cheio presente
    assert "DOL" not in resolved, "DOL fora de circulação — não deve ter resolved_symbol"
    # Wave 9: IND reativado (M15 BOLLINGER edge de elite, PnL HOJE +R$ 609)
    # IND AGORA é permitido em resolved_symbols (mas só IND, não DOL).
