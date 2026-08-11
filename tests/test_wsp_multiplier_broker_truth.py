"""test_wsp_multiplier_broker_truth.py
=====================================
Regressão: trava o multiplicador (R$/ponto) do WSP em 2.5 nos TRÊS locais
que já tiveram o bug, impedindo que volte a 0.01 (ou qualquer outro palpite).

BUG CORRIGIDO (Bruno 11/08/2026):
  WSP = Micro S&P 500 (B3). O broker-truth (MT5 symbol_info WSPU26) é:
      trade_tick_value = 0.625 BRL   (valor de 1 tick em R$)
      trade_tick_size  = 0.25        (incremento de preço por tick)
      → mult = tick_value / tick_size = 0.625 / 0.25 = 2.5 R$/ponto

  Histórico de bugs (3 locais independentes, todos assumiam WSP ≈ BIT):
    1. backtest/backtest_v944.py        CONTRACT_SPECS["WSP$"].mult = 0.01
    2. optimization/vt_forward_backtest _CONTRACT_SPECS["WSP$"/"WSPU26"].mult = 0.50
    3. core/vt_trade_log.py             get_multiplier _mults["WSP"] = 0.01

  Impacto do bug no backtest (locus 1, o que o AGI usa):
    PnL por trade = (move_pts) × mult − fee_r − slip_r
    Com mult=0.01 e fee_r=7.0: break-even exige 700pts favoráveis só p/ cobrir
    a taxa. Nenhuma trade WSP real chega lá antes do SL → PF=0.00 em TODAS
    as estratégias/params testadas pelo AGI. Resultado: os 4 pares WSP
    (M5/M15/M30/H1) ficavam perpetuamente na lista de "failing pairs" e o
    AGI jamais aprovava uma estratégia WSP — não por falta de edge, mas por
    um bug de config de contrato que fazia toda vitória aparecer como perda.

  Por que o palpite "R$13.50/pt" também estava errado: confundiu o multiplicador
  do full S&P (USD 50/pt) com o Micro (USD 0.50/pt). O broker-truth é a fonte
  autoritativa, não hardcode — por isso este teste trava 2.5 (validado em MT5).

Nota: análise/backtest usam o perpétuo WSP$ (série costurada de 30d); operação
ao vivo usa o contrato resolvido WSPU26. Ambos têm o MESMO mult (2.5) — o
R$/ponto é propriedade do contrato, não da forma de cotação.
"""
import re
import sys
from pathlib import Path

_PROJECT = Path(__file__).resolve().parent.parent


def _read(path: str) -> str:
    return (_PROJECT / path).read_text(encoding="utf-8")


# ─── Derivação broker-truth (documentada, não contatada em runtime) ───────────
# MT5 symbol_info WSPU26 (Bruno 11/08/2026):
WSP_TICK_VALUE = 0.625   # BRL por tick
WSP_TICK_SIZE = 0.25     # pts de preço por tick
WSP_MULT_TRUTH = WSP_TICK_VALUE / WSP_TICK_SIZE   # = 2.5 R$/ponto


def test_broker_truth_derivation():
    """A derivação tick_value/tick_size deve dar 2.5 (sinal de que entendemos
    o contrato, não um número mágico)."""
    assert WSP_MULT_TRUTH == 2.5, (
        f"Esperado 2.5 R$/pt (0.625/0.25), recebi {WSP_MULT_TRUTH}"
    )


def test_backtest_v944_wsp_mult_is_truth():
    """Locus 1 (canônico AGI): backtest_v944.py WSP$ mult deve ser 2.5.

    Este é o arquivo que o AGI importa — o bug aqui cega o AGI para WSP.
    """
    src = _read("backtest/backtest_v944.py")
    m = re.search(r'"WSP\$"\s*:\s*\{[^}]*"mult"\s*:\s*([\d.]+)', src)
    assert m, "não achei CONTRACT_SPECS WSP$ em backtest_v944.py"
    mult = float(m.group(1))
    assert mult == WSP_MULT_TRUTH, (
        f"backtest_v944 WSP$ mult={mult} deve ser {WSP_MULT_TRUTH} "
        f"(broker-truth MT5). Era 0.01 (cópia do BIT) — causava PF=0 no AGI."
    )


def test_backtest_v944_wsp_not_bit_copy():
    """Sinalização: o valor errado 0.01 NÃO deve voltar para WSP$ em backtest_v944."""
    src = _read("backtest/backtest_v944.py")
    bad = re.search(r'"WSP\$"\s*:\s*\{[^}]*"mult"\s*:\s*0\.01', src)
    assert not bad, (
        "regressão: WSP$ mult voltou a 0.01 (cópia do BIT). Isso fez o AGI "
        "reportar PF=0 em todos os WSP — break-even exigia 700pts favoráveis."
    )


def test_forward_backtest_wsp_mult_is_truth():
    """Locus 2 (forward backtest / monitoramento): WSP$ e WSPU26 mult = 2.5."""
    src = _read("optimization/vt_forward_backtest.py")
    for key in ('"WSP\\$"', '"WSPU26"'):
        m = re.search(key + r'\s*:\s*\{\s*"mult"\s*:\s*([\d.]+)', src)
        assert m, f"não achei {key} em vt_forward_backtest _CONTRACT_SPECS"
        mult = float(m.group(1))
        assert mult == WSP_MULT_TRUTH, (
            f"vt_forward_backtest {key} mult={mult} deve ser {WSP_MULT_TRUTH}"
        )


def test_trade_log_wsp_mult_is_truth():
    """Locus 3 (report de PnL ao vivo): get_multiplier _mults WSP = 2.5.

    Este valor escala o PnL WLP reportado de trades reais — o bug subestimava
    o PnL WSP em 250× (2.5/0.01).
    """
    src = _read("core/vt_trade_log.py")
    m = re.search(r'"WSP"\s*:\s*([\d.]+)', src)
    assert m, "não achei _mults['WSP'] em vt_trade_log.py"
    mult = float(m.group(1))
    assert mult == WSP_MULT_TRUTH, (
        f"vt_trade_log _mults WSP={mult} deve ser {WSP_MULT_TRUTH} "
        f"(antes 0.01 → PnL WSP reportado subestimado 250×)"
    )


def test_get_multiplier_wsp_functional():
    """Validação end-to-end: get_multiplier('WSPU26') retorna 2.5."""
    sys.path.insert(0, str(_PROJECT / "core"))
    try:
        from vt_trade_log import get_multiplier  # type: ignore
        assert get_multiplier("WSPU26") == WSP_MULT_TRUTH, (
            f"get_multiplier('WSPU26') deve ser {WSP_MULT_TRUTH}"
        )
        # WSP$ perpétuo (análise) também bate no mesmo mult:
        assert get_multiplier("WSP$") == WSP_MULT_TRUTH
    finally:
        sys.path.remove(str(_PROJECT / "core"))


def test_wsp_break_even_is_tradable():
    """Sanity econômica: com mult=2.5 e fee R$7, break-even ≈ 3.3pts (tratável).

    Com o bug (mult=0.01) o break-even era 700pts — intraday impossível. Este
    teste documenta POR QUE o fix destrava o AGI: WSP volta a ser avaliável.
    """
    fee_r = 7.0
    slip_r = 1.25
    be_buggy = (fee_r + 0.0002) / 0.01      # ~700 pts (impossível intraday)
    be_fixed = (fee_r + slip_r) / 2.5        # ~3.3 pts (tratável)
    assert be_buggy > 500, "bug break-even deveria ser >500pts (PF=0 garantido)"
    assert be_fixed < 10, "break-even corrigido deveria ser <10pts (tratável)"
