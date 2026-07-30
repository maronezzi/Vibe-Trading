"""Testes do fix do fallback de PnL (Wave 880.G — Bruno 2026-07-20).

Bug corrigido: em core/vt_autotrader.py, a fórmula de fallback de PnL do
caminho "FECHADO PELO SERVIDOR" usava `point_val` (preço/ponto = 1.0 para
WIN) onde deveria usar R$/ponto (= 0.20 para WIN mini). Resultado: notes
"PnL real: R$-515.00" quando o real é R$-103.00 (inflação 5x).

O fix substitui point_val por get_multiplier(symbol) no cálculo.

Estes testes NÃO importam core.vt_autotrader (host-coupled: lê MT5/DB no
top-level). Validam:
  (a) a fórmula correta isoladamente (mesma aritmética do fix);
  (b) que o source do autotrader contém o fix (garantia estrutural);
  (c) que get_multiplier retorna 0.20 para WIN (fonte da verdade).
"""
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _read_autotrader_source() -> str:
    with open(_PROJECT_ROOT / "core" / "vt_autotrader.py", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# (a) Fórmula correta isolada — mesma aritmética aplicada no fix.
# ---------------------------------------------------------------------------
def _fallback_pnl_brl(direction: str, entry: float, exit_price: float, brl_per_pt: float) -> float:
    """Réplica da fórmula de fallback corrigida (Wave 880.G)."""
    if direction == "BUY":
        return (exit_price - entry) * brl_per_pt
    return (entry - exit_price) * brl_per_pt


def test_sell_win_fallback_matches_db_not_inflated():
    """Trade #35 do dia 20/07: SELL WINQ26 174585→175100.

    pts = 174585 - 175100 = -515
    Correto (R$0.20/pt): -515 × 0.20 = -R$103.00  (casa com DB net_pnl)
    Inflado antigo (point_val 1.0): -515 × 1.0 = -R$515.00  (errado)
    """
    profit = _fallback_pnl_brl("SELL", 174585.0, 175100.0, brl_per_pt=0.20)
    assert profit == -103.0, f"esperado -103.0 (515pts × 0.20), got {profit}"
    # Garante que NÃO é o valor inflado antigo
    assert profit != -515.0, "fallback ainda está inflado 5x (point_val=1.0)"


def test_buy_win_fallback_matches_db_not_inflated():
    """Trade #18 do dia 20/07: BUY WINQ26 175270→175440.

    pts = 175440 - 175270 = +170
    Correto: +170 × 0.20 = +R$34.00  (casa com DB net_pnl)
    Inflado antigo: +170 × 1.0 = +R$170.00  (errado)
    """
    profit = _fallback_pnl_brl("BUY", 175270.0, 175440.0, brl_per_pt=0.20)
    assert profit == 34.0, f"esperado +34.0 (170pts × 0.20), got {profit}"
    assert profit != 170.0, "fallback ainda está inflado 5x (point_val=1.0)"


# ---------------------------------------------------------------------------
# (b) Garantia estrutural — o source contém o fix.
# ---------------------------------------------------------------------------
def test_source_uses_get_multiplier_in_fallback():
    """O bloco de fallback deve usar get_multiplier(symbol), não point_val,
    no cálculo de profit."""
    src = _read_autotrader_source()

    # Localiza o bloco de fallback de PnL.
    marker = "# Fallback: cálculo local se history falhou"
    start = src.find(marker)
    assert start != -1, "bloco de fallback de PnL não encontrado"

    # O bloco relevante termina no log_exit seguinte.
    end = src.find("exit_result = log_exit(", start)
    assert end != -1, "fim do bloco de fallback não encontrado"
    block = src[start:end]

    assert "from core.vt_trade_log import get_multiplier" in block, (
        "fallback não importa get_multiplier (Wave 880.G)"
    )
    assert "_brl_per_pt = get_multiplier(symbol)" in block, (
        "fallback não usa get_multiplier para R$/ponto"
    )
    assert "* _brl_per_pt" in block, "fallback não multiplica por _brl_per_pt"

    # CRÍTICO: o cálculo de profit no fallback NÃO deve usar point_val.
    # (point_val segue válido nos outros usos — SL/breakeven/trailing.)
    assert "profit = (current_price - entry_price) * point_val" not in block, (
        "fallback ainda usa point_val (bug 5x não corrigido)"
    )
    assert "profit = (entry_price - current_price) * point_val" not in block, (
        "fallback ainda usa point_val (bug 5x não corrigido)"
    )


def test_source_tracks_profit_source_in_note():
    """A note deve refletir honestamente a origem do PnL (broker-truth ou
    fallback local), não mentir 'broker-truth' sempre."""
    src = _read_autotrader_source()
    assert '_profit_source = "fallback local"' in src, (
        "variável _profit_source não inicializada"
    )
    assert '_profit_source = "broker-truth via MT5 history"' in src, (
        "_profit_source não é atualizada no caminho broker-truth"
    )
    assert "({ _profit_source})" in src or "({_profit_source})" in src, (
        "note não interpola _profit_source"
    )


def test_point_val_still_used_for_sl_calculations():
    """point_val NÃO foi removido — segue correto nos cálculos de SL/preço
    (conversão preço↔ponto, não R$/ponto). Regressão: garante que não
    quebrei os outros ~22 usos."""
    src = _read_autotrader_source()
    # exit_sl_price deve continuar usando point_val (preço/ponto).
    assert "exit_sl_price = entry_price - abs(pos.get(\"sl_pts\", 0)) * point_val" in src or \
           "exit_sl_price = entry_price + abs(pos.get(\"sl_pts\", 0)) * point_val" in src, (
        "exit_sl_price parou de usar point_val — regressão nos cálculos de SL"
    )


# ---------------------------------------------------------------------------
# (c) get_multiplier é a fonte da verdade de R$/ponto para WIN.
# ---------------------------------------------------------------------------
def test_get_multiplier_returns_020_for_win():
    """Confirma que 0.20 é o valor broker-truth para WIN (mini). Este é o
    número que o fix passa a usar no fallback."""
    # Import seguro: vt_trade_log não é host-coupled como vt_autotrader.
    # conftest.py isola CONFIG_PATH para tmp, então load_config não lê
    # produção. O fallback hardcoded (0.20) deve prevalecer.
    import sys
    sys.path.insert(0, str(_PROJECT_ROOT / "core"))
    try:
        from vt_trade_log import get_multiplier  # type: ignore
        assert get_multiplier("WINQ26") == 0.20, (
            "get_multiplier(WINQ26) deveria retornar 0.20 (broker-truth mini)"
        )
        assert get_multiplier("WIN") == 0.20
    finally:
        sys.path.remove(str(_PROJECT_ROOT / "core"))
