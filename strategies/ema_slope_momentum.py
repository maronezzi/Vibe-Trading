"""
EMA_SLOPE_MOMENTUM — Momentum pela inclinação da EMA.

Tese: WDO_H1 tem poucos trades (5 em 30d) porque cruzamentos de EMA são raros
no H1 do dólar. Esta estratégia não depende de cruzamento — mede a INCLINAÇÃO
da EMA (comparação com N barras atrás) para detectar momentum persistente:

  - Inclinação: EMA atual vs EMA de `slope_lookback` barras atrás.
    Se EMA subiu > slope_threshold × ATR, momentum é altista.
  - Confirmação: ADX > adx_min e RSI do lado certo (não exausto).
  - Trigger: preço acima da EMA fast (comprando força, não contra).

Diferencial vs catálogo: EMA_CROSSOVER/EMA_PULLBACK dependem de cruzamento
(raro em H1). TRIPLE_EMA usa 3 EMAs (ainda mais lento). Esta é a primeira
que mede a taxa de variação (slope) da EMA — capta momentum que ainda não
cruzou mas já está direcionado, gerando mais sinais no H1 onde o movimento
é gradual e persistente.

Params: ema_period=21, slope_lookback=8, slope_threshold=0.3, adx_period=14,
        adx_min=20, rsi_period=14, rsi_max_buy=70, rsi_min_sell=30, sl_atr_mult=1.6
"""
STRATEGY_NAME = "EMA_SLOPE_MOMENTUM"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Verifica sinal de entrada EMA_SLOPE_MOMENTUM.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    if not bars or len(bars) < 50:
        return None
    if atr is None or atr <= 0:
        return None

    calculate_ema = utils["calculate_ema"]
    calculate_rsi = utils["calculate_rsi"]
    calculate_adx = utils["calculate_adx"]
    calc_sl = utils["calc_sl"]

    ema_period = params.get("ema_period", 21)
    slope_lookback = params.get("slope_lookback", 8)
    slope_threshold = params.get("slope_threshold", 0.3)
    adx_period = params.get("adx_period", 14)
    adx_min = params.get("adx_min", 20)
    rsi_period = params.get("rsi_period", 14)
    rsi_max_buy = params.get("rsi_max_buy", 70)
    rsi_min_sell = params.get("rsi_min_sell", 30)

    # EMA atual
    ema_now = calculate_ema(bars, ema_period)
    if ema_now == 0:
        return None

    # EMA de slope_lookback barras atrás (fatia o histórico)
    bars_ago = bars[slope_lookback:]
    if len(bars_ago) < ema_period:
        return None
    ema_past = calculate_ema(bars_ago, ema_period)
    if ema_past == 0:
        return None

    # Inclinação normalizada pelo ATR (movimento relativo à volatilidade)
    slope = (ema_now - ema_past) / atr if atr > 0 else 0

    # Força e direção da tendência
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    if adx_val < adx_min:
        return None

    rsi = calculate_rsi(bars, rsi_period)

    direction = None

    # Momentum altista: EMA subindo forte + preço acima da EMA + não exausto
    if slope > slope_threshold and plus_di > minus_di and price > ema_now:
        if rsi < rsi_max_buy:  # ainda não sobrecomprado
            direction = "BUY"

    # Momentum baixista: EMA caindo forte + preço abaixo da EMA
    if slope < -slope_threshold and minus_di > plus_di and price < ema_now:
        if rsi > rsi_min_sell:  # ainda não sobrevendido
            direction = "SELL"

    if direction is None:
        return None

    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": STRATEGY_NAME,
            "ema_now": round(ema_now, 1),
            "ema_past": round(ema_past, 1),
            "slope": round(slope, 3),
            "adx": round(adx_val, 1),
            "rsi": round(rsi, 1),
            "atr": atr,
        },
    }
