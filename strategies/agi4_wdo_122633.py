"""
Estratégia de Day Trade com Controle de Risco baseada em Volatilidade para WDO_H1.
Gatilho: Breakout de Donchian (20 períodos).
Filtro: Preço vs EMA 50.
Gate: ADX saudável e Z-Score não esticado.
"""

STRATEGY_NAME = "AGI4_WDO_122633"

TUNABLE_PARAMS = {
    "donchian_period": (int, 10, 50),
    "ema_trend_period": (int, 20, 100),
    "adx_min": (float, 15.0, 30.0),
    "adx_max": (float, 35.0, 60.0),
    "zscore_max": (float, 1.5, 3.5)
}

def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    calculate_ema = utils["calculate_ema"]
    calculate_adx = utils["calculate_adx"]
    calculate_donchian = utils["calculate_donchian"]
    calculate_zscore = utils["calculate_zscore"]
    calc_sl = utils["calc_sl"]

    donchian_period = params.get("donchian_period", 20)
    ema_trend_period = params.get("ema_trend_period", 50)
    adx_period = params.get("adx_period", 14)
    adx_min = params.get("adx_min", 20.0)
    adx_max = params.get("adx_max", 45.0)
    zscore_period = params.get("zscore_period", 20)
    zscore_max = params.get("zscore_max", 2.5)

    min_bars = max(donchian_period, ema_trend_period, adx_period * 2, zscore_period) + 10
    if not bars or len(bars) < min_bars:
        return None
    
    if atr <= 0 or price <= 0:
        return None

    # Cálculo dos indicadores
    dc_upper, dc_mid, dc_lower = calculate_donchian(bars, donchian_period)
    ema_trend = calculate_ema(bars, ema_trend_period)
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    zscore = calculate_zscore(bars, zscore_period)

    # Guards contra valores inválidos
    if not all([dc_upper, dc_lower, ema_trend, adx_val]):
        return None
    
    if dc_upper == 0 or dc_lower == 0 or ema_trend == 0:
        return None

    # Gate de Regime: ADX em faixa saudável (nem ruído, nem exaustão)
    if adx_val < adx_min or adx_val > adx_max:
        return None

    # Gate de Esticamento: Evitar entradas quando o preço está muito esticado estatisticamente
    if abs(zscore) > zscore_max:
        return None

    direction = None
    info = {
        "adx": adx_val,
        "zscore": zscore,
        "dc_upper": dc_upper,
        "dc_lower": dc_lower,
        "ema": ema_trend
    }

    # Lógica de Entrada: Breakout + Filtro de Tendência
    
    # Compra: Preço quebra a máxima do Donchian E está acima da EMA de tendência
    if price > dc_upper and price > ema_trend:
        direction = "BUY"
        
    # Venda: Preço quebra a mínima do Donchian E está abaixo da EMA de tendência
    elif price < dc_lower and price < ema_trend:
        direction = "SELL"

    if direction is None:
        return None

    sl_pts = calc_sl(symbol, atr, params)
    
    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": info
    }