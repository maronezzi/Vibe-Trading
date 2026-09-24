"""
Momentum Breakout com Filtro ADX Simplificado (WSP_H1)

Gatilho: rompimento de máximo/mínimo do range das últimas N barras.
Filtro: ADX acima do mínimo para garantir tendência direcional real.
Regime: Continuação em alta vol/tendência (ADX alto), Reversão em lateralidade (ADX baixo, extremidades de Bollinger).
SL: Calculado via utils["calc_sl"] baseado em ATR e params.
"""

STRATEGY_NAME = "AGI4_WSP_123521"

TUNABLE_PARAMS = {
    "range_period": (int, 10, 50),       # Tamanho do range para breakout
    "adx_min_break": (float, 15.0, 35.0), # ADX mínimo para sinais de BREAKOUT
    "adx_max_revert": (float, 15.0, 30.0),# ADX máximo para sinais de REVERSÃO
    "zscore_thres": (float, 1.0, 3.0),   # Limiar de Z-Score para confirmar momentum/extremidade
    "bb_period": (int, 15, 30)           # Período do indicador auxiliar de volatilidade/reversão
}

def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    # Extração de helpers
    calculate_donchian = utils["calculate_donchian"]
    calculate_adx = utils["calculate_adx"]
    calculate_zscore = utils["calculate_zscore"]
    calculate_bollinger = utils["calculate_bollinger"]
    calc_sl = utils["calc_sl"]

    # Params defensivos
    range_period = params.get("range_period", 20)
    adx_min_break = params.get("adx_min_break", 25.0)
    adx_max_revert = params.get("adx_max_revert", 20.0)
    zscore_thres = params.get("zscore_thres", 2.0)
    bb_period = params.get("bb_period", 20)
    bb_std = params.get("bb_std", 2.0) # Helper param, não tunable principal

    # Guards de Warmup e Dados Válidos
    min_bars = max(range_period, adx_min_break * 2 if adx_min_break > 0 else 0, bb_period) + 10
    if not bars or len(bars) < min_bars:
        return None
    
    if atr <= 0 or price <= 0:
        return None

    # 1. Calcular Range (Donchian Channel) - Exclui barra atual
    don_upper, don_mid, don_lower = calculate_donchian(bars, range_period)
    
    # 2. Calcular Tendência (ADX)
    adx_val, plus_di, minus_di = calculate_adx(bars, 14) # Usando período padrão para estrutura robusta

    # 3. Regime / Volatilidade Extremos (Z-Score & Bollinger)
    zsc = calculate_zscore(bars, 20)
    up_bb, mid_bb, lo_bb = calculate_bollinger(bars, bb_period, bb_std)

    # Validação dos indicadores
    if don_upper == 0 or don_lower == 0 or adx_val == 0 or mid_bb == 0:
        return None

    direction = None
    
    # --- LÓGICA HÍBRIDA DE REGIME ---

    if adx_val >= adx_min_break:
        # MODO TENDÊNCIA (BREAKOUT CONTINUATION)
        # Condição Long: Preço rompeu o topo do range recente + Momentum positivo confirmado por Z-Score alto
        if price > don_upper and zsc > zscore_thres:
            direction = "BUY"
        
        # Condição Short: Preço rompeu a base do range recente + Momentum negativo confirmado por Z-Score baixo
        elif price < don_lower and zsc < -zscore_thres:
            direction = "SELL"

    elif adx_val < adx_max_revert:
        # MODO LATERALIZADO (MEAN REVERSION NAS EXTREMIDADES)
        # Evita pegar topos/fundos prematuros usando filtros de estiramento extremo
        
        # Long Reversal: Esticado demais para cima (BB) + Esticado demais para baixo (Z-Score negativo forte? Não, se é reversão de topo... 
        # Para reversão LONG num mercado lateral: preço bate no fundo da BB ou Z-score extremamente negativo esperando pull-up?
        # Melhor definição clássica: Preço cai abaixo do Lower Band/Donchian Lower mas está em regime fraco -> Compra na sobrecarga vendedora exaurida.
        # Vamos usar Donchian Lower + Z-score muito negativo como gatilho de exaustão.
        if price < don_lower and zsc < -zscore_thres * 0.8: # Levemente mais permissivo que thres para reversão
             direction = "BUY"
             
        elif price > don_upper and zsc > zscore_thres * 0.8: # Venda na exaustão compradora
             direction = "SELL"
    else:
        # ADX em zona "cinza" entre reversão fraca e confirmação forte. Não opera.
        return None

    if not direction:
        return None

    # Cálculo OBRIGATÓRIO do Stop Loss
    sl_pts = calc_sl(symbol, atr, params)

    info_data = {
        "direction_type": "Breakout" if adx_val >= adx_min_break else "Reversion",
        "adx": round(adx_val, 2),
        "zscore": round(zsc, 2),
        "range_high": round(don_upper, 2),
        "range_low": round(don_lower, 2)
    }

    return {
        "direction": direction,
        "sl_pts": int(sl_pts),
        "info": info_data
    }