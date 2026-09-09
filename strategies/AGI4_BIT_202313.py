"""AGI4_BIT_202313 — Keltner breakout híbrido com filtro de momentum + expansão de ATR."""

STRATEGY_NAME = "AGI4_BIT_202313"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Keltner breakout híbrido com filtro de momentum (RSI) + expansão de ATR.

    Par: BIT_M5
    Lógica:
      1. Calcula EMA central (Keltner midline) e bandas superior/inferior
         via Keltner channel simplificado (EMA + ATR * mult).
      2. Confirma expansão de volatilidade: ATR_atual > 1.2 * ATR_medio(20).
      3. Filtra momentum: RSI(14) > 55 para long, < 45 para short.
      4. Breakout de banda: close > upper (long) ou close < lower (short).
      5. SL: 1.5x ATR (sl_atr_mult=1.5).

    Parâmetros:
        symbol:       str  - par (ex.: "BIT", "BIT_M5", "BITM26").
        tf:           str  - timeframe (ex.: "M5").
        price:        float - preço atual (não usado, usa bars[-1]).
        atr:          float - ATR atual já calculado pelo caller.
        bar_ts:       datetime - timestamp da barra atual.
        bars:         list[dict] - OHLCV, cada dict com chaves open/high/low/close/volume.
        params:       dict - config da estratégia (keltner_period, keltner_mult, etc.).
        utils:        dict - indicadores injetados (calculate_rsi, calculate_ema,
                       calculate_bollinger, calculate_adx, calc_sl).

    Retorno:
        None se sem setup;
        dict {"direction": "BUY"|"SELL", "sl_pts": int, "info": {...}} em caso de sinal.
    """
    # --- guards básicos ---
    if not bars or len(bars) < 30:
        return None

    # --- parâmetros com defaults seguros ---
    keltner_period = int(params.get("keltner_period", 20))
    keltner_mult = float(params.get("keltner_mult", 2.0))
    rsi_period = int(params.get("rsi_period", 14))
    rsi_long_threshold = float(params.get("rsi_long_threshold", 55.0))
    rsi_short_threshold = float(params.get("rsi_short_threshold", 45.0))
    atr_expansion_period = int(params.get("atr_expansion_period", 20))
    atr_expansion_mult = float(params.get("atr_expansion_mult", 1.2))
    float(params.get("sl_atr_mult", 1.5))
    cooldown_seconds = int(params.get("cooldown_seconds", 400))
    max_daily_trades = int(params.get("max_daily_trades", 5))

    # --- resolve indicadores injetados (não importar nada) ---
    calculate_rsi = utils["calculate_rsi"]
    calculate_ema = utils["calculate_ema"]
    calc_sl = utils["calc_sl"]

    # --- fecha da barra atual ---
    close = float(bars[-1].get("close", price))

    # --- Keltner midline + bandas ---
    ema_mid = calculate_ema(bars, keltner_period)
    if ema_mid is None or atr is None or atr <= 0:
        return None

    upper_band = ema_mid + (keltner_mult * atr)
    lower_band = ema_mid - (keltner_mult * atr)

    # --- filtro de expansão de ATR (ATR_atual > 1.2x média[20]) ---
    # Wave 893 fix: calculate_atr do utils devolve ESCALAR (nunca série) —
    # len() em float lançava TypeError em TODO combo da simulação (o flood
    # "BUG DE CÓDIGO" no sandbox). Média de True Range puro sobre a janela,
    # mesma fórmula do calculate_atr; bars newest-first (prev close de
    # bars[k] é bars[k+1]).
    trs = []
    for k in range(min(atr_expansion_period, len(bars) - 1)):
        b_now, b_prev = bars[k], bars[k + 1]
        h = float(b_now.get("high", 0) or 0)
        lo = float(b_now.get("low", 0) or 0)
        c_prev = float(b_prev.get("close", 0) or 0)
        if h <= 0 or lo <= 0 or c_prev <= 0:
            continue
        trs.append(max(h - lo, abs(h - c_prev), abs(lo - c_prev)))
    if len(trs) < atr_expansion_period:
        return None
    atr_avg = sum(trs) / float(len(trs))
    if atr_avg <= 0:
        return None
    atr_expansion_ratio = atr / atr_avg
    if atr_expansion_ratio < atr_expansion_mult:
        return None

    # --- filtro de momentum (RSI) ---
    rsi_val = calculate_rsi(bars, rsi_period)
    if rsi_val is None:
        return None

    direction = None
    if close > upper_band and rsi_val > rsi_long_threshold:
        direction = "BUY"
    elif close < lower_band and rsi_val < rsi_short_threshold:
        direction = "SELL"
    else:
        return None

    # --- LEI 3: sl_pts OBRIGATÓRIO via utils["calc_sl"] ---
    sl_pts = int(calc_sl(symbol, atr, params))

    info = {
        "strategy": STRATEGY_NAME,
        "symbol": symbol,
        "tf": tf,
        "bar_ts": str(bar_ts),
        "close": close,
        "ema_mid": float(ema_mid),
        "upper_band": float(upper_band),
        "lower_band": float(lower_band),
        "atr": float(atr),
        "atr_avg": float(atr_avg),
        "atr_expansion_ratio": float(atr_expansion_ratio),
        "rsi": float(rsi_val),
        "keltner_period": keltner_period,
        "keltner_mult": keltner_mult,
        "cooldown_seconds": cooldown_seconds,
        "max_daily_trades": max_daily_trades,
    }

    return {"direction": direction, "sl_pts": sl_pts, "info": info}
