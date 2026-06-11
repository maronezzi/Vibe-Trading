"""
Estratégia VWAP — Mean-reversion com filtro de tendência.
Para WDO (mercado trending).

Parâmetros (via vt_config.json → wdo):
  vwap_period, vwap_buy_threshold, vwap_sell_threshold
  ema_fast, ema_slow, rsi_period, rsi_overbought, rsi_oversold
  sl_atr_mult, trail_activate, trail_distance
"""

STRATEGY_NAME = "VWAP"

# Imports são feitos via autotrader (funções utilitárias passadas como contexto)
# Este plugin usa funções do vt_autotrader via import direto


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada VWAP.

    Args:
        symbol: símbolo (ex: "WDOQ26")
        tf: timeframe (ex: "M5")
        price: preço atual
        atr: ATR atual
        bar_ts: timestamp da barra
        bars: lista de barras (mais recente primeiro)
        params: parâmetros do símbolo do vt_config.json
        utils: dict com funções utilitárias do autotrader

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calculate_vwap = utils["calculate_vwap"]
    calculate_ema = utils["calculate_ema"]
    calculate_rsi = utils["calculate_rsi"]
    get_market_regime = utils["get_market_regime"]
    calc_sl = utils["calc_sl"]

    # Market regime
    regime = "UNKNOWN"
    ema_slow_val_cfg = params.get("ema_slow", 21)
    if bars and len(bars) >= ema_slow_val_cfg + 5:
        regime = get_market_regime(bars, params)
        if regime == "CHOPPY":
            return None

    # VWAP
    vwap = calculate_vwap(bars, params.get("vwap_period", 20))
    if vwap == 0:
        return None

    # Trend direction
    ema_fast = ema_slow_val = 0
    if bars and len(bars) >= ema_slow_val_cfg + 5:
        ema_fast = calculate_ema(bars, params.get("ema_fast", 9))
        ema_slow_val = calculate_ema(bars, ema_slow_val_cfg)

    # Threshold adaptativo
    atr_pct = (atr / price) if price > 0 else 0
    if atr_pct < 0.0015:
        buy_mult = 1.0005
        sell_mult = 0.9995
    elif atr_pct < 0.003:
        buy_mult = 1.0015
        sell_mult = 0.9985
    else:
        buy_mult = params.get("vwap_buy_threshold", 1.003)
        sell_mult = params.get("vwap_sell_threshold", 0.997)

    buy_thresh = vwap * buy_mult
    sell_thresh = vwap * sell_mult

    direction = None
    if price > buy_thresh:
        direction = "BUY"
    elif price < sell_thresh:
        direction = "SELL"

    if not direction:
        return None

    # Trend filter
    if ema_fast > 0 and ema_slow_val > 0:
        if direction == "BUY" and ema_fast < ema_slow_val:
            return None
        if direction == "SELL" and ema_fast > ema_slow_val:
            return None

    # RSI filter
    rsi = 50
    rsi_period = params.get("rsi_period", 14)
    if bars and len(bars) >= rsi_period + 2:
        rsi = calculate_rsi(bars, rsi_period)
        if direction == "BUY" and rsi > params.get("rsi_overbought", 70):
            return None
        if direction == "SELL" and rsi < params.get("rsi_oversold", 30):
            return None

    # SL
    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "VWAP",
            "vwap": vwap,
            "rsi": rsi,
            "regime": regime,
            "ema_fast": ema_fast,
            "ema_slow": ema_slow_val,
            "buy_thresh": buy_thresh,
            "sell_thresh": sell_thresh,
        },
    }
