"""
AGI4_WSP_123245 — Estratégia para WSP_M5.

Abordagem: mean-reversion em range com filtro de volatilidade relativa.
Ideia central: WSP_M5 tende a oscilar em bandas; só entra quando o RSI
mostra exaustão curta, a posição do preço dentro das Bollinger confirma
o stretched, e o ADX garante que o mercado não está em tendência forte
(não queremos pegar faca caindo). Saída sempre via SL fixo em pontos,
calculado pelo utils["calc_sl"] padrão do AGI (Lei 3).

Sinais:
  BUY  -> RSI < rsi_buy (sobrevendido) AND close <= banda inferior
          AND ADX < adx_max (sem tendência) AND EMA rápida > EMA lenta
          (filtro direcional opcional, ajuda a não comprar em queda livre)
  SELL -> RSI > rsi_sell (sobrecomprado) AND close >= banda superior
          AND ADX < adx_max AND EMA rápida < EMA lenta

Sem imports — tudo vem via utils e params (SANDBOX).
"""

STRATEGY_NAME = "AGI4_WSP_123245"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    # ---- guards básicos ----
    if not bars or len(bars) < 30:
        return None
    if atr is None or atr <= 0:
        return None

    # ---- parâmetros com defaults defensivos ----
    rsi_period = int(params.get("rsi_period", 7))
    rsi_buy = float(params.get("rsi_buy", 25.0))
    rsi_sell = float(params.get("rsi_sell", 75.0))

    bb_period = int(params.get("bb_period", 20))
    bb_std = float(params.get("bb_std", 2.0))

    ema_fast_period = int(params.get("ema_fast", 9))
    ema_slow_period = int(params.get("ema_slow", 21))

    adx_period = int(params.get("adx_period", 14))
    adx_max = float(params.get("adx_max", 25.0))

    # ---- indicadores via utils ----
    calculate_rsi = utils["calculate_rsi"]
    calculate_bollinger = utils["calculate_bollinger"]
    calculate_ema = utils["calculate_ema"]
    calculate_adx = utils["calculate_adx"]
    calc_sl = utils["calc_sl"]

    try:
        rsi_val = calculate_rsi(bars, rsi_period)
        bb = calculate_bollinger(bars, bb_period, bb_std)
        ema_fast = calculate_ema(bars, ema_fast_period)
        ema_slow = calculate_ema(bars, ema_slow_period)
        adx_val = calculate_adx(bars, adx_period)
    except Exception:
        return None

    if rsi_val is None or bb is None or ema_fast is None or ema_slow is None or adx_val is None:
        return None

    upper, middle, lower = bb
    if upper is None or middle is None or lower is None:
        return None

    # ---- filtro ADX: sem tendência forte ----
    if adx_val >= adx_max:
        return None

    # LEI 3 — calcula sl_pts obrigatório antes de qualquer retorno de sinal
    sl_pts = calc_sl(symbol, atr, params)
    if sl_pts is None or sl_pts <= 0:
        return None

    # ---- sinal BUY ----
    if rsi_val < rsi_buy and price <= lower and ema_fast > ema_slow:
        return {
            "direction": "BUY",
            "sl_pts": int(sl_pts),
            "info": {
                "rsi": rsi_val,
                "adx": adx_val,
                "bb_lower": lower,
                "bb_middle": middle,
                "bb_upper": upper,
                "ema_fast": ema_fast,
                "ema_slow": ema_slow,
                "atr": atr,
                "reason": "mean_reversion_buy",
            },
        }

    # ---- sinal SELL ----
    if rsi_val > rsi_sell and price >= upper and ema_fast < ema_slow:
        return {
            "direction": "SELL",
            "sl_pts": int(sl_pts),
            "info": {
                "rsi": rsi_val,
                "adx": adx_val,
                "bb_lower": lower,
                "bb_middle": middle,
                "bb_upper": upper,
                "ema_fast": ema_fast,
                "ema_slow": ema_slow,
                "atr": atr,
                "reason": "mean_reversion_sell",
            },
        }

    return None