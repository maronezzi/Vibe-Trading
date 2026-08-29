STRATEGY_NAME = "AGI4_BIT_121102"

TUNABLE_PARAMS = {
    "bb_period": (int, 14, 30),
    "bb_std": (float, 1.5, 3.0),
    "bw_lookback": (int, 10, 40),
    "bw_expand_ratio": (float, 1.05, 2.5),
    "rsi_period": (int, 7, 21),
}


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Avalia sinal de reversao Bollinger em BIT_M5 com gate de regime por bandwidth.

    Retorna None ou dict com direction/sl_pts/info.
    """
    # --- Helpers (contrato dos indicadores: retorno escalar/tupla, uso direto) ---
    utils["calculate_ema"]
    calculate_rsi = utils["calculate_rsi"]
    utils["calculate_adx"]
    calculate_bollinger = utils["calculate_bollinger"]
    calc_sl = utils["calc_sl"]

    # --- Parametros ---
    bb_period = params.get("bb_period", 20)
    bb_std = params.get("bb_std", 2.0)
    bw_lookback = params.get("bw_lookback", 20)
    bw_expand_ratio = params.get("bw_expand_ratio", 1.3)
    bw_squeeze_ratio = params.get("bw_squeeze_ratio", 0.7)
    rsi_period = params.get("rsi_period", 14)
    rsi_oversold = params.get("rsi_oversold", 30)
    rsi_overbought = params.get("rsi_overbought", 70)
    touch_tol_pct = params.get("touch_tol_pct", 0.15)  # tolerancia de toque (% do preco)

    # --- Guards basicos ---
    min_bars = bb_period + bw_lookback + 5
    if not bars or len(bars) < min_bars:
        return None
    if atr is None or atr <= 0:
        return None

    # --- Indicadores (retornos diretos, sem indexacao) ---
    upper, mid, lower = calculate_bollinger(bars, bb_period, bb_std)
    rsi = calculate_rsi(bars, rsi_period)

    if upper == 0 or mid == 0 or lower == 0:
        return None
    if mid <= 0:
        return None
    if rsi is None or rsi == 0:
        return None

    # --- Bandwidth atual (largura normalizada pela banda central) ---
    current_bw = (upper - lower) / mid

    # --- Historico de bandwidth (janela rolante de bb_period barras) ---
    widths = []
    start = max(bb_period - 1, len(bars) - bw_lookback)
    for i in range(start, len(bars)):
        window = bars[i - bb_period + 1 : i + 1]
        u, m, lo = calculate_bollinger(window, bb_period, bb_std)
        if m is not None and m > 0 and u > lo:
            widths.append((u - lo) / m)
    if len(widths) < 5:
        return None

    avg_bw = sum(widths) / len(widths)
    if avg_bw <= 0:
        return None

    # --- Regime das bandas ---
    bw_ratio = current_bw / avg_bw
    if bw_ratio > bw_expand_ratio:
        # Bandas expandindo = tendencia forte / expansao de volatilidade.
        # Reversao nao reverte aqui (facada em queda) — ignora sinal.
        return None
    if bw_ratio < bw_squeeze_ratio:
        regime = "squeeze"
    else:
        regime = "stable"

    # --- Toque de banda (sobrevenda/sobrecompra) ---
    tol = 1.0 + (touch_tol_pct / 100.0)
    touch_lower = price <= lower * tol
    touch_upper = price >= upper / tol

    direction = None
    if touch_lower and rsi < rsi_oversold:
        direction = "BUY"
    elif touch_upper and rsi > rsi_overbought:
        direction = "SELL"

    if direction is None:
        return None

    # --- LEI 3: SL obrigatorio via calc_sl ---
    sl_pts = calc_sl(symbol, atr, params)
    if sl_pts is None or sl_pts <= 0:
        return None

    return {
        "direction": direction,
        "sl_pts": int(sl_pts),
        "info": {
            "strategy": STRATEGY_NAME,
            "pair": "BIT_M5",
            "logic": "bollinger_reversion_bandwidth_regime",
            "trigger": "lower_band_touch" if direction == "BUY" else "upper_band_touch",
            "bb_upper": round(upper, 2),
            "bb_mid": round(mid, 2),
            "bb_lower": round(lower, 2),
            "bw": round(current_bw, 6),
            "bw_avg": round(avg_bw, 6),
            "bw_ratio": round(bw_ratio, 4),
            "regime": regime,
            "rsi": round(rsi, 2),
            "atr": round(atr, 2),
            "bar_ts": bar_ts,
        },
    }
