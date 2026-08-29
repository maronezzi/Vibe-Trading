"""
AGI4_WDO_061536 — Pullback de tendência com aceleração ADX (fast vs slow) para WDO_H1.

Tese:
  A varredura das 30+ estratégias existentes não achou edge em WDO_H1. As
  abordagens testadas se dividem em: seguidoras de tendência puras
  (ADX_TREND, STRONG_TREND, EMA_CROSSOVER, TRIPLE_EMA), reversões
  (RSI_REVERSION, MEAN_REVERSION_ZSCORE, ENHANCED_RSI_REVERSION) e
  breakouts (DONCHIAN_BREAKOUT, VOLATILITY_BREAKOUT, MOMENTUM_BREAKOUT).
  Nenhuma combina os TRÊS elementos desta estratégia:

  1. Aceleração de tendência via ADX fast vs slow: ADX(7) > ADX(14) mede
     se a força de tendência RECENTE supera a força de janela longa —
     proxy de momentum de tendência. Como utils retorna escalar por
     chamada, comparar períodos diferentes é o único jeito de capturar a
     DINÂMICA da força sem indexar série.
  2. Pullback na banda média das Bollinger: entrada só quando o preço
     RETESTA a média (mid) na direção da tendência — melhor relação
     risco/retorno do que perseguir preço longe da média.
  3. Zona RSI de confirmação (não exausto): RSI 45-68 no BUY (32-55 no
     SELL) — momentum presente sem sobrecompra/sobrevenda que precede
     reversão.

  Gestão: stop via calc_sl (Lei 3) e a vencedora corre sob trailing do
  engine — adequado a WDO_H1, onde o impulso intraday é forte e as
  tendências duram barras H1.

Contrato:
  - utils retorna escalares/tuplas fixas — NUNCA indexar com [-1], len(),
    ou .get() no RESULTADO. Usar o retorno diretamente. Cortes/slices são
    aplicados na ENTRADA (lista de barras), nunca no retorno.
  - Lei 3: TODO sinal retorna sl_pts via calc_sl(symbol, atr, params).
  - SANDBOX: sem imports (sem os, subprocess, mt5). Tudo via utils e params.
"""

STRATEGY_NAME = "AGI4_WDO_061536"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Verifica sinal AGI4_WDO_061536.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calculate_ema = utils["calculate_ema"]
    calculate_rsi = utils["calculate_rsi"]
    calculate_adx = utils["calculate_adx"]
    calculate_bollinger = utils["calculate_bollinger"]
    calc_sl = utils["calc_sl"]

    # --- Params (sempre com default defensivo) ---
    adx_fast_period = params.get("adx_fast_period", 7)   # ADX janela curta (força recente)
    adx_slow_period = params.get("adx_slow_period", 14)  # ADX janela longa (força base)
    adx_min = params.get("adx_min", 22)                  # gate de regime trending
    rsi_period = params.get("rsi_period", 14)
    rsi_min = params.get("rsi_min", 45)                  # piso zona momentum (BUY)
    rsi_max = params.get("rsi_max", 68)                  # teto zona momentum (BUY)
    bb_period = params.get("bb_period", 20)
    bb_std = params.get("bb_std", 2.0)
    ema_period = params.get("ema_period", 50)            # tendência de fundo (H1 ≈ 2 dias)
    pullback_pct = params.get("pullback_pct", 0.004)     # faixa de reteste da banda média (0.4%)

    # --- Guardas de warmup ---
    min_bars = max(adx_slow_period * 2, bb_period, ema_period) + 5
    if not bars or len(bars) < min_bars:
        return None
    if atr <= 0 or price <= 0:
        return None

    # --- Indicadores (retornos escalares/tuplas — uso direto) ---
    adx_fast, plus_di_fast, minus_di_fast = calculate_adx(bars, adx_fast_period)
    adx_slow, plus_di, minus_di = calculate_adx(bars, adx_slow_period)
    rsi = calculate_rsi(bars, rsi_period)
    upper, mid, lower = calculate_bollinger(bars, bb_period, bb_std)
    ema_trend = calculate_ema(bars, ema_period)

    if adx_slow == 0 or adx_fast == 0 or ema_trend == 0:
        return None
    if upper == 0 or mid == 0 or lower == 0:
        return None
    if upper <= mid or lower >= mid:
        return None

    direction = None
    logic = None

    # --- BUY: tendência + aceleração ADX + DI alinhado + reteste da banda média ---
    if adx_slow >= adx_min and adx_fast > adx_slow and plus_di > minus_di:
        if price > ema_trend and mid < price <= mid * (1.0 + pullback_pct):
            if rsi_min <= rsi <= rsi_max:
                direction = "BUY"
                logic = "adx_accel_mid_pullback"

    # --- SELL: espelho ---
    if direction is None:
        if adx_slow >= adx_min and adx_fast > adx_slow and minus_di > plus_di:
            rsi_low_floor = 100.0 - rsi_max
            rsi_low_ceil = 100.0 - rsi_min
            if price < ema_trend and mid * (1.0 - pullback_pct) <= price < mid:
                if rsi_low_floor <= rsi <= rsi_low_ceil:
                    direction = "SELL"
                    logic = "adx_accel_mid_pullback"

    if direction is None or logic is None:
        return None

    # --- Lei 3: SL obrigatório via calc_sl ---
    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": STRATEGY_NAME,
            "entry_price": price,
            "logic": logic,
            "adx_fast": round(adx_fast, 2),
            "adx_slow": round(adx_slow, 2),
            "plus_di": round(plus_di, 2),
            "minus_di": round(minus_di, 2),
            "rsi": round(rsi, 2),
            "bb_mid": round(mid, 2),
            "ema_trend": round(ema_trend, 2),
            "atr": round(atr, 2),
            "sl_pts": sl_pts,
        },
    }