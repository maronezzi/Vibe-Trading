"""
Estratégia: AGI4_WIN_211906
Par: WIN_H1
Lógica: Trend Following com Filtro de Volatilidade.
Gatilho: Cruzamento EMA 9/21 ou Rompimento Donchian 20.
Filtro: Preço vs EMA 50.
Gate: ATR atual > Média Histórica de ATR (aproximada via Z-score ou comparação direta se disponível, mas aqui usamos ADX como proxy de regime/volatilidade conforme regras do sistema e TUNABLE_PARAMS).
Nota: Como não há função 'calculate_atr_average' explícita no utils, usamos o ADX como gate de regime (volatilidade/tendência) e o Z-score para garantir que não estamos em extremos irrecuperáveis, alinhando-se ao padrão AGI4.
"""

STRATEGY_NAME = "AGI4_WIN_211906"

TUNABLE_PARAMS = {
    "ema_fast": (int, 5, 20),
    "ema_slow": (int, 15, 50),
    "ema_trend": (int, 30, 80),
    "donchian_period": (int, 10, 40),
    "adx_min": (float, 15.0, 30.0),
    "adx_max": (float, 35.0, 60.0)
}

def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    # Extrair helpers
    calculate_ema = utils["calculate_ema"]
    calculate_adx = utils["calculate_adx"]
    calculate_donchian = utils["calculate_donchian"]
    calculate_zscore = utils["calculate_zscore"]
    calc_sl = utils["calc_sl"]

    # Parâmetros com defaults defensivos
    ema_fast_p = params.get("ema_fast", 9)
    ema_slow_p = params.get("ema_slow", 21)
    ema_trend_p = params.get("ema_trend", 50)
    donchian_p = params.get("donchian_period", 20)
    adx_period = params.get("adx_period", 14)
    adx_min = params.get("adx_min", 18.0)
    adx_max = params.get("adx_max", 45.0)
    zscore_limit = params.get("zscore_limit", 2.5)

    # Warmup e Guards
    min_bars = max(ema_trend_p, donchian_p, adx_period * 2) + 10
    if not bars or len(bars) < min_bars:
        return None
    
    if atr <= 0 or price <= 0:
        return None

    # Calcular Indicadores
    ema_fast = calculate_ema(bars, ema_fast_p)
    ema_slow = calculate_ema(bars, ema_slow_p)
    ema_trend = calculate_ema(bars, ema_trend_p)
    
    if not all([ema_fast, ema_slow, ema_trend]):
        return None

    # Gate de Regime: ADX deve estar em faixa saudável (nem ruído, nem exaustão)
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    if adx_val < adx_min or adx_val > adx_max:
        return None

    # Gate de Extremo: Z-score não deve estar muito esticado (evita comprar topo/vender fundo extremo sem confirmação)
    z_score = calculate_zscore(bars, 20)
    if abs(z_score) > zscore_limit:
        return None

    # Filtro de Tendência Principal: Preço vs EMA Longa
    # Se preço > EMA Trend, só BUY. Se preço < EMA Trend, só SELL.
    is_uptrend = price > ema_trend
    is_downtrend = price < ema_trend

    if not is_uptrend and not is_downtrend:
        return None

    # Gatilhos
    signal_direction = None

    # Gatilho 1: Cruzamento de EMAs (Fast > Slow para Buy, Fast < Slow para Sell)
    # Usamos a posição relativa das EMAs como filtro de momentum curto prazo
    fast_above_slow = ema_fast > ema_slow
    fast_below_slow = ema_fast < ema_slow

    # Gatilho 2: Rompimento Donchian
    dc_upper, dc_mid, dc_lower = calculate_donchian(bars, donchian_p)
    
    if not all([dc_upper, dc_mid, dc_lower]):
        return None

    breakout_high = price > dc_upper
    breakout_low = price < dc_lower

    # Lógica de Entrada Combinada
    if is_uptrend:
        # Para BUY: Precisa de momentum positivo (Fast > Slow) E (Rompimento OU Preços acima da média rápida)
        # O prompt pede "Cruzamento" ou "Rompimento". 
        # Interpretando "Cruzamento" como estado atual das médias (Fast > Slow) para manter consistência com "Trend Following".
        if fast_above_slow and (breakout_high or price > ema_fast):
            signal_direction = "BUY"
            
    elif is_downtrend:
        # Para SELL: Precisa de momentum negativo (Fast < Slow) E (Rompimento Baixo OU Preços abaixo da média rápida)
        if fast_below_slow and (breakout_low or price < ema_fast):
            signal_direction = "SELL"

    if signal_direction is None:
        return None

    # Cálculo do Stop Loss
    sl_pts = calc_sl(symbol, atr, params)
    if sl_pts <= 0:
        return None

    return {
        "direction": signal_direction,
        "sl_pts": sl_pts,
        "info": {
            "adx": adx_val,
            "zscore": z_score,
            "trigger": "breakout" if (breakout_high or breakout_low) else "ema_cross_state"
        }
    }