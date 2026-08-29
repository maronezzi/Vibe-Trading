"""
Estratégia AGI4_WDO_223241 — Breakout de range com filtro ADX para WDO H1.

Substitui lógica de RSI reversion por trend-following direcional:
- Rompimento da máxima/mínima das últimas N barras (N=12 para H1)
- Filtro ADX > 30 confirma tendência forte antes de entrar
- Stop ATR 2.0 via calc_sl; trailing stop ATR na gestão de posição

Justificativa: WDO rastreia câmbio BRL/USD com viés direcional estrutural
(carry trade + fluxo cambial). Em H1, movimentos direcionais persistentes
favorecem trend-following; mean reversion opera sistematicamente contra a
tendência estrutural do dólar.

Parâmetros (via vt_config.json):
  range_lookback (12), adx_period (14), adx_threshold (30)
  sl_atr_mult (2.0), trail_atr_mult (1.5)
"""

STRATEGY_NAME = "AGI4_WDO_223241"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de breakout de range com filtro ADX.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calculate_adx = utils["calculate_adx"]
    calc_sl = utils["calc_sl"]

    range_lookback = params.get("range_lookback", 12)
    adx_period = params.get("adx_period", 14)
    adx_threshold = params.get("adx_threshold", 30)

    min_bars = max(range_lookback, adx_period * 2) + 5
    if not bars or len(bars) < min_bars:
        return None
    if atr <= 0:
        return None

    # ADX — filtro de tendência forte
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    if adx_val == 0 or adx_val < adx_threshold:
        return None  # Sem tendência forte — não entrar

    # Range das últimas N barras (exclui barra atual, índice 0)
    lookback_bars = bars[1:range_lookback + 1]
    if len(lookback_bars) < range_lookback:
        return None

    range_high = max(b.get("high", 0) for b in lookback_bars)
    range_low = min(b.get("low", float("inf")) for b in lookback_bars)

    if range_high == 0 or range_low == float("inf") or range_high <= range_low:
        return None

    direction = None

    # BUY: preço rompeu a máxima do range + DI+ > DI- (tendência de alta confirmada)
    if price > range_high and plus_di > minus_di:
        direction = "BUY"
    # SELL: preço rompeu a mínima do range + DI- > DI+ (tendência de baixa confirmada)
    elif price < range_low and minus_di > plus_di:
        direction = "SELL"

    if not direction:
        return None

    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": STRATEGY_NAME,
            "atr": round(atr, 2),
            "adx": round(adx_val, 2),
            "plus_di": round(plus_di, 2),
            "minus_di": round(minus_di, 2),
            "range_high": round(range_high, 2),
            "range_low": round(range_low, 2),
            "range_lookback": range_lookback,
        },
    }