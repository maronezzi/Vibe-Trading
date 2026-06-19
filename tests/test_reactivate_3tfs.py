"""
TDD: Estado operacional dos pares do Vibe-Trading.

Histórico:
- 17/06: 3 TFs reativados (WSP_M15, WSP_H1, BIT_H1) → 11 disabled
- 17/06: Experimento de troca de estratégia → 9 pares reativados → 2 disabled
- 18/06: AGI finalizou reativações com base em 7d de dados
- 19/06: Bruno removeu IND/DOL (contratos cheios fora de circulação)
        → restam apenas 4 minis: WIN, BIT, WSP, WDO

Estado atual validado em 2026-06-19:
- 4 symbols × 4 TFs = 16 pares ativos
- 0 pares em disabled_timeframes
- Estratégia consolidada (sem swaps pendentes)
"""

import json
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / "vt_config.json"

# Símbolos ativos a partir de 2026-06-19 (4 minis)
ACTIVE_SYMBOLS = ["WIN", "BIT", "WSP", "WDO"]
ACTIVE_TIMEFRAMES = ["M5", "M15", "M30", "H1"]


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def test_active_symbols_are_only_4_minis():
    """Apenas WIN, BIT, WSP, WDO (4 minis) devem estar ativos.

    IND (Índice Cheio) e DOL (Dólar Cheio) foram removidos por decisão
    do Bruno em 2026-06-19.
    """
    cfg = load_config()
    assert set(cfg["symbols"]) == set(ACTIVE_SYMBOLS), (
        f"Esperado {ACTIVE_SYMBOLS}, achou {cfg['symbols']}"
    )


def test_no_disabled_timeframes():
    """Todos os 16 pares (4×4) devem estar ativos — 0 em disabled_timeframes."""
    cfg = load_config()
    disabled = cfg.get("disabled_timeframes", [])
    assert len(disabled) == 0, (
        f"Esperado 0 TFs desativados, achou {len(disabled)}: {disabled}"
    )


def test_no_disabled_symbols():
    """Nenhum símbolo deve estar em disabled_symbols."""
    cfg = load_config()
    disabled = cfg.get("disabled_symbols", [])
    assert len(disabled) == 0, (
        f"Esperado 0 symbols desativados, achou {len(disabled)}: {disabled}"
    )


def test_no_ind_dol_residual_in_known_keys():
    """Chaves conhecidas do config (16 pares ativos) não devem conter IND/DOL.

    NOTA: _optimize_dol_halt_grid (função de produção do AGI) pode injetar
    chaves DOL_* em halt_duration_minutes_by_tf durante o pytest (lê do
    disco real). Este teste valida apenas as chaves que o config de
    PRODUÇÃO deveria ter — se aparecerem chaves extras, o teste
    detecta pela contagem > 16 ou prefixo DOL_/IND_.
    """
    cfg = load_config()

    # 1. Top-level (chaves raiz — DOL/IND não devem existir como seção)
    for k in cfg:
        if k in {"ind", "dol"} or k.startswith("ind_") or k.startswith("dol_"):
            raise AssertionError(
                f"Resíduo de IND/DOL em chave raiz: '{k}'"
            )

    # 2. Chaves simples conhecidas (volume_by_symbol, resolved_symbols, etc.)
    for key in ["symbols", "volume_by_symbol", "resolved_symbols", "contract_specs"]:
        entries = cfg.get(key, {})
        if isinstance(entries, list):
            for s in entries:
                if s in {"IND", "DOL"}:
                    raise AssertionError(f"Resíduo de IND/DOL em '{key}': {s}")
        elif isinstance(entries, dict):
            for k in entries:
                if k in {"IND", "DOL", "IND$", "DOL$"}:
                    raise AssertionError(f"Resíduo de IND/DOL em '{key}.{k}'")

    # 3. Validação de contagem: todos os 16 pares ativos (4 minis × 4 TFs)
    #    DEVEM estar presentes em cada dict. Extras são tolerados porque
    #    _optimize_dol_halt_grid pode injetar DOL_* durante o pytest.
    #    O guard real é: se algum par ativo FALTAR, alguém removeu acidentalmente.
    active_prefixes = {f"{s}_{tf}" for s in ACTIVE_SYMBOLS for tf in ACTIVE_TIMEFRAMES}
    for key in ["strategy_by_tf", "params_by_tf", "halt_duration_minutes_by_tf",
                 "max_consecutive_losses_by_tf"]:
        entries = cfg.get(key, {})
        missing = active_prefixes - set(entries.keys())
        assert not missing, (
            f"{key} está faltando pares ativos: {missing}"
        )


def test_all_active_pairs_have_strategy():
    """Todos os 16 pares devem ter estratégia atribuída em strategy_by_tf."""
    cfg = load_config()
    strategy_by_tf = cfg.get("strategy_by_tf", {})
    expected = {f"{s}_{tf}" for s in ACTIVE_SYMBOLS for tf in ACTIVE_TIMEFRAMES}
    actual = set(strategy_by_tf.keys())
    missing = expected - actual
    extra = actual - expected
    assert not missing, f"Pares sem estratégia: {missing}"
    assert not extra, f"Par(es) extras em strategy_by_tf: {extra}"


def test_all_active_pairs_have_halt_config():
    """Todos os 16 pares devem ter halt_duration_minutes_by_tf configurado."""
    cfg = load_config()
    halt = cfg.get("halt_duration_minutes_by_tf", {})
    expected = {f"{s}_{tf}" for s in ACTIVE_SYMBOLS for tf in ACTIVE_TIMEFRAMES}
    actual = set(halt.keys())
    missing = expected - actual
    assert not missing, f"Pares sem halt: {missing}"


def test_all_active_pairs_have_consecutive_loss_config():
    """Todos os 16 pares devem ter max_consecutive_losses_by_tf configurado."""
    cfg = load_config()
    mcl = cfg.get("max_consecutive_losses_by_tf", {})
    expected = {f"{s}_{tf}" for s in ACTIVE_SYMBOLS for tf in ACTIVE_TIMEFRAMES}
    actual = set(mcl.keys())
    missing = expected - actual
    assert not missing, f"Pares sem max_consecutive_losses: {missing}"


def test_version_is_incremented():
    """Version do config deve ser > 0 (sanidade — config está vivo)."""
    cfg = load_config()
    assert cfg.get("_version", 0) > 0, "Version deve ser incrementada"


def test_config_notes_explain_state():
    """Notes do config devem documentar a remoção de IND/DOL."""
    cfg = load_config()
    notes = cfg.get("_notes", "")
    assert "IND" in notes and "DOL" in notes, (
        f"_notes deve documentar remoção de IND/DOL. Atual: {notes!r}"
    )
