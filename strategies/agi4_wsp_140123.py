"""
Estratégia: AGI4_WSP_140123
Par: WSP_H1
Lógica: Trend Following com Filtro de Regime baseada em EMA Pullback.
Gatilho: Preço toca/cruza EMA curta na direção da tendência (EMA longa).
Filtro: Preço > EMA longa para BUY, < para SELL.
Gate: ADX > adx_min para confirmar tendência.
SL: Calculado via utils['calc_sl'].
"""

STRATEGY_NAME = "AGI4_WSP_140123"

TUNABLE_PARAMS = {
    "ema_fast": (int, 5, 30),
    "ema_slow": (int, 30, 100),
    "adx_min": (float, 15.0, 35.0),
    "adx_max": (float, 35.0, 60.0),
    "rsi_period": (int, 7, 21),
}


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    calculate_ema = utils["calculate_ema"]
    calculate_adx = utils["calculate_adx"]
    calculate_rsi = utils["calculate_rsi"]
    calc_sl = utils["calc_sl"]

    # Parâmetros com defaults defensivos
    ema_fast_p = params.get("ema_fast", 20)
    ema_slow_p = params.get("ema_slow", 50)
    adx_period = params.get("adx_period", 14)
    adx_min = params.get("adx_min", 20.0)
    adx_max = params.get("adx_max", 45.0)
    rsi_period = params.get("rsi_period", 14)
    
    # Guardas de warmup e dados válidos
    min_bars = max(ema_slow_p, adx_period * 2, rsi_period) + 5
    if not bars or len(bars) < min_bars:
        return None
    
    if atr <= 0 or price <= 0:
        return None

    # Cálculo dos indicadores
    # Nota: calculate_ema retorna float (valor atual)
    ema_fast_val = calculate_ema(bars, ema_fast_p)
    ema_slow_val = calculate_ema(bars, ema_slow_p)
    
    # Nota: calculate_adx retorna tupla (adx, plus_di, minus_di)
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    
    # Nota: calculate_rsi retorna float
    rsi_val = calculate_rsi(bars, rsi_period)

    # Guards contra valores nulos/zero dos indicadores
    if ema_fast_val == 0 or ema_slow_val == 0 or adx_val == 0:
        return None

    # Gate de Regime/Volatilidade (Lei B: Faixa saudável de ADX)
    # ADX fraco demais = ruído; ADX forte demais = exaustão/clímax
    if adx_val < adx_min or adx_val > adx_max:
        return None

    # Filtro de Tendência (Polaridade)
    # BUY: Preço acima da EMA lenta
    # SELL: Preço abaixo da EMA lenta
    direction = None
    
    if price > ema_slow_val:
        # Verificação de Pullback para BUY
        # O preço deve estar perto ou ter tocado a EMA rápida recentemente
        # Usamos uma tolerância pequena baseada no ATR para definir "toca"
        tolerance = atr * 0.5 
        if price >= ema_fast_val - tolerance and price < ema_fast_val + tolerance * 2:
            # Confirmação adicional: RSI não sobrecomprado excessivamente
            if rsi_val < 70:
                direction = "BUY"
                
    elif price < ema_slow_val:
        # Verificação de Pullback para SELL
        tolerance = atr * 0.5
        if price <= ema_fast_val + tolerance and price > ema_fast_val - tolerance * 2:
            # Confirmação adicional: RSI não sobrevendido excessivamente
            if rsi_val > 30:
                direction = "SELL"

    if direction is None:
        return None

    # Cálculo do Stop Loss obrigatório
    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "adx": adx_val,
            "rsi": rsi_val,
            "ema_fast": ema_fast_val,
            "ema_slow": ema_slow_val
        }
    }