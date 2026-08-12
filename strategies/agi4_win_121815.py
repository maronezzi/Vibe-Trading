"""
AGI4_WIN_121815 — Reclaim da banda média de Bollinger com polaridade DI para WIN_M5.

Tese:
  WIN é futuro do Ibovespa (Fato 1) — índice que forma tendências persistentes
  e direcionalmente fortes (Fato 2). Em M5, as estratégias clássicas de
  reversão (RSI_REVERSION, MEAN_REVERSION_ZSCORE), breakout (VOLATILITY_BREAKOUT,
  SQUEEZE_BREAKOUT) e pullback de EMA (EMA_PULLBACK, TRIPLE_EMA) já foram
  tentadas sem lucro no par. A literatura de trading intraday de índice sugere
  que, em timeframe curto, o edge está em continuar o movimento dominante após
  um retorno do preço à "linha de equilíbrio" dinâmica — não em antecipar
  reversão nem em perseguir extensão.

  Esta estratégia é estruturalmente diferente das 48 testadas:

    - Não usa EMA como gatilho (usa a banda MÉDIA de Bollinger = SMA(20)
      como linha de equilíbrio dinâmica — o preço reclamando o meio do
      envelope após um desvio).
    - Não usa o valor de ADX como gatilho de entrada; usa a POLARIDADE do
      sistema direcional (+DI vs -DI) como gate de direção de tendência.
    - Exige que o preço esteja DENTRO do envelope (entre as bandas) no
      momento do reclaim — não entra em extensão além da banda externa.
    - RSI confirma momentum a favor da polaridade (acima/abaixo de 50).

  Regras:
    - BUY: +DI > -DI (polaridade de alta) E preço reclama a banda média por
      cima (price >= mid) E price <= upper (ainda dentro do envelope) E
      RSI > 50 (momentum comprador) E ADX em faixa saudável de continuidade
      (adx_min <= ADX <= adx_max — fraco demais = ruído, forte demais =
      exaustão/clímax).
    - SELL: espelho (-DI > +DI, price <= mid, price >= lower, RSI < 50).

Contrato:
  - utils retorna escalares/tuplas fixas — NUNCA indexar com [-1], len(), ou
    .get(). Usar o resultado diretamente.
  - Lei 3: TODO sinal retorna sl_pts via calc_sl(symbol, atr, params).
  - SANDBOX: sem imports (sem os, subprocess, mt5). Tudo via utils e params.
"""

STRATEGY_NAME = "AGI4_WIN_121815"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Verifica sinal AGI4_WIN_121815.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calculate_ema = utils["calculate_ema"]
    calculate_rsi = utils["calculate_rsi"]
    calculate_adx = utils["calculate_adx"]
    calculate_bollinger = utils["calculate_bollinger"]
    calc_sl = utils["calc_sl"]

    # --- Params (sempre com default defensivo) ---
    bb_period = params.get("bb_period", 20)
    bb_std = params.get("bb_std", 2.0)
    adx_period = params.get("adx_period", 14)
    rsi_period = params.get("rsi_period", 14)
    adx_min = params.get("adx_min", 18)    # abaixo disso = sem tendência para continuar
    adx_max = params.get("adx_max", 45)    # acima disso = exaustão/clímax, não entrar
    rsi_mid = params.get("rsi_mid", 50.0)  # polo de momentum (50 = linha neutra RSI)

    # --- Guardas de warmup ---
    min_bars = max(bb_period, adx_period * 2, rsi_period) + 5
    if not bars or len(bars) < min_bars:
        return None
    if atr <= 0 or price <= 0:
        return None

    # --- Indicadores (retornos escalares/tuplas — uso direto) ---
    upper, mid, lower = calculate_bollinger(bars, bb_period, bb_std)
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    rsi = calculate_rsi(bars, rsi_period)

    if mid == 0 or adx_val == 0:
        return None

    # --- Faixa saudável de ADX (continuidade sem exaustão) ---
    if adx_val < adx_min or adx_val > adx_max:
        return None

    direction = None
    logic = None
    polarity = "neutra"

    if plus_di > minus_di:
        polarity = "alta"
        # Reclaim da banda média por cima, ainda DENTRO do envelope (sem extensão)
        if price >= mid and price <= upper and rsi > rsi_mid:
            direction = "BUY"
            logic = "mid_band_reclaim_bull_polarity"
    elif minus_di > plus_di:
        polarity = "baixa"
        # Reclaim da banda média por baixo, ainda DENTRO do envelope (sem extensão)
        if price <= mid and price >= lower and rsi < rsi_mid:
            direction = "SELL"
            logic = "mid_band_reclaim_bear_polarity"

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
            "polarity": polarity,
            "rsi": round(rsi, 2),
            "adx": round(adx_val, 2),
            "plus_di": round(plus_di, 2),
            "minus_di": round(minus_di, 2),
            "bb_upper": round(upper, 2),
            "bb_mid": round(mid, 2),
            "bb_lower": round(lower, 2),
            "atr": round(atr, 2),
        },
    }