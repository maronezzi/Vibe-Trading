"""
Estratégia de Breakout de Volatilidade (Bollinger Bands) com Filtro de Regime ADX.
Foco: WIN_H1 (Ibovespa).
Lógica: Captura expansões de volatilidade quando o preço rompe as bandas de Bollinger,
confirmado por ADX em faixa saudável (tendência estabelecida mas não exausta).
"""

STRATEGY_NAME = "AGI4_WIN_123055"

TUNABLE_PARAMS = {
    "bb_period": (int, 10, 30),
    "bb_std": (float, 1.5, 3.0),
    "adx_period": (int, 10, 20),
    "adx_min": (float, 20.0, 35.0),
    "adx_max": (float, 40.0, 60.0),
    "rsi_period": (int, 7, 21)
}


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    # Extrair helpers
    calculate_bollinger = utils["calculate_bollinger"]
    calculate_adx = utils["calculate_adx"]
    calculate_rsi = utils["calculate_rsi"]
    calc_sl = utils["calc_sl"]

    # Parâmetros com defaults defensivos
    bb_period = params.get("bb_period", 20)
    bb_std = params.get("bb_std", 2.0)
    adx_period = params.get("adx_period", 14)
    adx_min = params.get("adx_min", 25.0)
    adx_max = params.get("adx_max", 50.0)
    rsi_period = params.get("rsi_period", 14)

    # Warmup e Guards
    min_bars = max(bb_period, adx_period * 2, rsi_period) + 5
    if not bars or len(bars) < min_bars:
        return None
    
    if atr <= 0 or price <= 0:
        return None

    # Cálculo dos Indicadores
    # Bollinger Bands: retorna (upper, mid, lower)
    upper, mid, lower = calculate_bollinger(bars, bb_period, bb_std)
    
    # ADX: retorna (adx, plus_di, minus_di)
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    
    # RSI: retorna float
    rsi = calculate_rsi(bars, rsi_period)

    # Guards adicionais contra valores inválidos
    if upper == 0 or lower == 0 or mid == 0:
        return None
    if adx_val is None or adx_val == 0:
        return None

    # Gate de Regime (ADX)
    # ADX fraco demais = ruído/lateralização (não entrar)
    # ADX forte demais = exaustão/clímax (evitar entrar no topo/fundo)
    if adx_val < adx_min or adx_val > adx_max:
        return None

    # Lógica de Entrada
    direction = None
    
    # Breakout para Alta (Long)
    # Preço fecha acima da banda superior
    if price > upper:
        # Filtro de polaridade/tendência adicional: 
        # Em breakout de alta, preferimos que o momentum não esteja esticado demais (RSI < 80)
        # E que a tendência seja confirmada (+DI > -DI é ideal, mas o ADX já filtra força)
        if rsi < 80:
            direction = "BUY"
            
    # Breakout para Baixa (Short)
    # Preço fecha abaixo da banda inferior
    elif price < lower:
        # Filtro de polaridade/tendência adicional:
        # Em breakout de baixa, preferimos que o momentum não esteja esticado demais (RSI > 20)
        if rsi > 20:
            direction = "SELL"

    if direction is None:
        return None

    # Calcular Stop Loss
    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "adx": adx_val,
            "rsi": rsi,
            "bb_upper": upper,
            "bb_lower": lower,
            "price": price
        }
    }