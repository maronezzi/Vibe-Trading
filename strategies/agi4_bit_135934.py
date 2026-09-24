"""
Estratégia: Trend Following com Filtro de Regime
Par: BIT_M30
Lógica:
1. Gatilho: Cruzamento de EMA 9 e EMA 21.
2. Filtro de Tendência: Preço em relação à EMA 50.
3. Gate de Regime: ADX > 20 (tendência forte) e ATR dentro de faixas médias (evitando picos extremos).
4. Direção: Long se EMA9 > EMA21 e Preço > EMA50; Short se contrário.
"""

STRATEGY_NAME = "AGI4_BIT_135934"

TUNABLE_PARAMS = {
    "ema_fast": (int, 5, 20),
    "ema_slow": (int, 10, 50),
    "ema_trend": (int, 30, 100),
    "adx_min": (float, 15.0, 30.0),
    "atr_pct_min": (float, 0.001, 0.01),
    "atr_pct_max": (float, 0.01, 0.05)
}

def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    # Extrair helpers do utils
    calculate_ema = utils["calculate_ema"]
    calculate_adx = utils["calculate_adx"]
    calc_sl = utils["calc_sl"]

    # Parâmetros com defaults defensivos
    ema_fast_period = params.get("ema_fast", 9)
    ema_slow_period = params.get("ema_slow", 21)
    ema_trend_period = params.get("ema_trend", 50)
    adx_period = params.get("adx_period", 14)
    adx_min = params.get("adx_min", 20.0)
    atr_pct_min = params.get("atr_pct_min", 0.002)
    atr_pct_max = params.get("atr_pct_max", 0.03)

    # Guards básicos
    if not bars or len(bars) < max(ema_trend_period, adx_period * 2) + 5:
        return None
    
    if price <= 0 or atr <= 0:
        return None

    # Calcular indicadores
    ema_fast = calculate_ema(bars, ema_fast_period)
    ema_slow = calculate_ema(bars, ema_slow_period)
    ema_trend = calculate_ema(bars, ema_trend_period)
    
    if ema_fast == 0 or ema_slow == 0 or ema_trend == 0:
        return None

    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    
    if adx_val == 0:
        return None

    # Gate de Regime: ADX deve ser forte o suficiente
    if adx_val < adx_min:
        return None

    # Gate de Volatilidade: ATR relativo ao preço deve estar dentro da faixa saudável
    atr_ratio = atr / price
    if atr_ratio < atr_pct_min or atr_ratio > atr_pct_max:
        return None

    # Lógica de Entrada
    direction = None
    
    # Condição Long: EMA Fast > EMA Slow E Preço > EMA Trend
    if ema_fast > ema_slow and price > ema_trend:
        direction = "BUY"
        
    # Condição Short: EMA Fast < EMA Slow E Preço < EMA Trend
    elif ema_fast < ema_slow and price < ema_trend:
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
            "atr_ratio": atr_ratio,
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "ema_trend": ema_trend
        }
    }