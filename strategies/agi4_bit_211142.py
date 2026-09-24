"""
Estratégia: Trend Following with Volatility Filter
Par: BIT_M5
Baseada no setup vencedor WSP_M15 adaptado para M5.
Gatilho: Cruzamento EMA 9/21. Filtro: Preço vs EMA 50. Gate: BB Width > Média BB Width.
"""

STRATEGY_NAME = "AGI4_BIT_211142"

TUNABLE_PARAMS = {
    "ema_fast": (int, 5, 15),
    "ema_slow": (int, 15, 30),
    "ema_trend": (int, 30, 60),
    "bb_period": (int, 15, 30),
    "bb_std": (float, 1.5, 2.5),
    "adx_min": (float, 15.0, 25.0)
}

def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    # Extrair helpers
    calculate_ema = utils["calculate_ema"]
    calculate_bollinger = utils["calculate_bollinger"]
    calculate_adx = utils["calculate_adx"]
    calc_sl = utils["calc_sl"]

    # Parâmetros com defaults defensivos
    ema_fast_p = params.get("ema_fast", 9)
    ema_slow_p = params.get("ema_slow", 21)
    ema_trend_p = params.get("ema_trend", 50)
    bb_period = params.get("bb_period", 20)
    bb_std = params.get("bb_std", 2.0)
    adx_period = params.get("adx_period", 14)
    adx_min = params.get("adx_min", 18.0)
    adx_max = params.get("adx_max", 45.0)
    
    # Calcular min_bars necessário
    # Precisamos de barras suficientes para calcular as EMAs e a média da largura das bandas
    # A média da largura das bandas usa o mesmo período das bandas (bb_period)
    min_bars = max(ema_trend_p, bb_period * 2, adx_period * 2) + 10
    
    if not bars or len(bars) < min_bars:
        return None
        
    if atr <= 0 or price <= 0:
        return None

    # Calcular Indicadores
    ema_fast_val = calculate_ema(bars, ema_fast_p)
    ema_slow_val = calculate_ema(bars, ema_slow_p)
    ema_trend_val = calculate_ema(bars, ema_trend_p)
    
    upper, mid, lower = calculate_bollinger(bars, bb_period, bb_std)
    
    # Calcular ADX para gate de regime
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)

    # Guards contra valores inválidos
    if not all([ema_fast_val, ema_slow_val, ema_trend_val, mid]):
        return None
    if mid == 0:
        return None
    if adx_val == 0:
        return None

    # Gate de Regime/Volatilidade (ADX)
    if adx_val < adx_min or adx_val > adx_max:
        return None

    # Cálculo da Largura das Bandas de Bollinger (BB Width)
    # Width = (Upper - Lower) / Mid
    current_bb_width = (upper - lower) / mid
    
    # Calcular a média histórica da BB Width dos últimos 'bb_period' períodos
    # Para isso, precisamos iterar pelas barras anteriores e calcular a width em cada uma
    # Como não podemos importar nada e as funções retornam escalares para a barra atual,
    # precisamos recalcular ou usar uma aproximação. 
    # No entanto, o contrato diz que as funções retornam valores para a barra ATUAL.
    # Para obter a "média histórica", teríamos que chamar calculate_bollinger N vezes?
    # Não, isso é ineficiente e possivelmente não suportado se as funções não aceitarem offset.
    # Alternativa comum em sandbox: Usar a própria banda atual como proxy ou assumir que 
    # a volatilidade está alta se a width atual for maior que uma fração do ATR ou similar.
    # Mas a descrição pede: "largura ... acima da média histórica de 20 períodos".
    
    # Vamos implementar um loop simples para calcular a média da width nas últimas N barras.
    # Note: As funções de utilitário geralmente operam na lista `bars` inteira e retornam o valor da ÚLTIMA barra.
    # Para obter valores passados, precisaríamos fatiar a lista `bars`.
    # Exemplo: bars[:-1] para a barra anterior.
    
    lookback_widths = []
    for i in range(1, bb_period + 1):
        past_bars = bars[:-i]
        if len(past_bars) < min_bars - i:
            continue
        try:
            u, m, l = calculate_bollinger(past_bars, bb_period, bb_std)
            if m != 0:
                w = (u - l) / m
                lookback_widths.append(w)
        except Exception:
            continue
            
    if not lookback_widths:
        return None
        
    avg_bb_width = sum(lookback_widths) / len(lookback_widths)
    
    # Gate de Volatilidade: A width atual deve ser maior que a média histórica
    if current_bb_width <= avg_bb_width:
        return None

    # Lógica de Entrada
    direction = None
    
    # Condição Long:
    # 1. EMA Fast > EMA Slow (Cruzamento/Tendência curta)
    # 2. Preço > EMA Trend (Filtro de tendência de médio prazo)
    if ema_fast_val > ema_slow_val and price > ema_trend_val:
        direction = "BUY"
        
    # Condição Short:
    # 1. EMA Fast < EMA Slow
    # 2. Preço < EMA Trend
    elif ema_fast_val < ema_slow_val and price < ema_trend_val:
        direction = "SELL"

    if direction is None:
        return None

    sl_pts = calc_sl(symbol, atr, params)
    
    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "adx": adx_val,
            "bb_width_current": current_bb_width,
            "bb_width_avg": avg_bb_width,
            "ema_fast": ema_fast_val,
            "ema_slow": ema_slow_val,
            "ema_trend": ema_trend_val
        }
    }