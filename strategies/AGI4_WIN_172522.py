"""
AGI4_WIN_172522 — Filtro de tendência na reversão RSI para WIN_H1.

Tese:
  WIN é futuro do Ibovespa (Fato 1) — índice que forma tendências persistentes
  e direcionalmente fortes (Fato 2). O RSI Mean Reversion é uma estratégia de
  mercado LATERAL (Fato 4) e a implementação de referência trata a seleção de
  regime como parte da lógica de entrada (Fato 5). A literatura de RSI mean
  reversion recomenda filtrar contra a tendência dominante para não 'pegar
  faca caindo' em H1, onde a reversão sem contexto de tendência perde para o
  momentum.

  Esta estratégia adiciona um GATE DE TENDÊNCIA à reversão RSI:

    - SELL (rsi > rsi_overbought): permitido, pois vender sobrecompra na
      tendência é contra-tendência e aqui mantemos apenas se o preço estiver
      LONG/CIMA do filtro — caso contrário bloqueado (não lutar contra o
      momentum de queda).
    - BUY  (rsi < rsi_oversold): só COMPRA reversion quando o preço estiver
      ACIMA da média de longo prazo (EMA lenta) OU em pullback dentro de
      tendência de alta (EMA fast > EMA slow + preço acima da EMA lenta).
      Evita comprar quedas em H1 de índice em tendência de baixa contínua.

  O gate usa EMA de longo prazo como proxy de tendência dominante: se o preço
  está abaixo da EMA lenta e a EMA rápida está abaixo da lenta (estrutura
  baixista persistente), NÃO compramos oversold — o filtro bloqueia a 'faca
  caindo'.

Contrato:
  - utils retorna escalares/tuplas fixas — NUNCA indexar com [-1], len(), ou
    .get(). Usar o resultado diretamente.
  - Lei 3: TODO sinal retorna sl_pts via calc_sl(symbol, atr, params).
  - SANDBOX: sem imports (sem os, subprocess, mt5). Tudo via utils e params.
"""

STRATEGY_NAME = "AGI4_WIN_172522"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Verifica sinal AGI4_WIN_172522.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calculate_ema = utils["calculate_ema"]
    calculate_rsi = utils["calculate_rsi"]
    calculate_adx = utils["calculate_adx"]
    calculate_bollinger = utils["calculate_bollinger"]
    calc_sl = utils["calc_sl"]

    # --- Params (sempre com default defensivo) ---
    ema_fast_period = params.get("ema_fast", 9)
    ema_slow_period = params.get("ema_slow", 50)  # média de longo prazo (proxy de tendência)
    adx_period = params.get("adx_period", 14)
    adx_threshold = params.get("adx_threshold", 25)
    rsi_period = params.get("rsi_period", 14)
    rsi_overbought = params.get("rsi_overbought", 70)
    rsi_oversold = params.get("rsi_oversold", 30)
    bb_period = params.get("bb_period", 20)
    bb_std = params.get("bb_std", 2.0)
    allow_pullback_buy = params.get("allow_pullback_buy", True)

    # --- Guardas de warmup ---
    min_bars = max(ema_slow_period, ema_fast_period, adx_period * 2, bb_period) + 5
    if not bars or len(bars) < min_bars:
        return None
    if atr <= 0 or price <= 0:
        return None

    # --- Indicadores (retornos escalares — uso direto) ---
    ema_fast_val = calculate_ema(bars, ema_fast_period)
    ema_slow_val = calculate_ema(bars, ema_slow_period)
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    rsi = calculate_rsi(bars, rsi_period)

    if ema_fast_val == 0 or ema_slow_val == 0 or adx_val == 0:
        return None

    # --- Gate de tendência dominante (média de longo prazo) ---
    uptrend = price > ema_slow_val          # preço acima da média de longo prazo
    bullish_structure = ema_fast_val > ema_slow_val  # EMA rápida acima da lenta
    downtrend_persistent = (not uptrend) and (not bullish_structure)

    direction = None
    logic = None
    filter_state = "lateral"

    if uptrend:
        filter_state = "uptrend"
    elif downtrend_persistent:
        filter_state = "downtrend"

    # --- Reversão RSI com filtro de tendência ---
    if rsi > rsi_overbought:
        # SELL sobrecompra: só em contexto de alta (não comprar/operar contra queda contínua)
        if uptrend and bullish_structure:
            direction = "SELL"
            logic = "rsi_overbought_reversion_uptrend"
    elif rsi < rsi_oversold:
        # BUY oversold: NUNCA comprar queda em tendência de baixa contínua.
        # Compra apenas se preço acima da média de longo prazo (pullback na alta)
        # ou estrutura ainda bullish (EMA fast > slow) com preço pullback.
        if uptrend or (allow_pullback_buy and bullish_structure):
            direction = "BUY"
            logic = "rsi_oversold_reversion_pullback"
        else:
            filter_state = "downtrend_blocked"
            return None  # bloqueia 'pegar faca caindo' em H1

    if direction is None or logic is None:
        return None

    # --- Filtro de força (ADX): não reverter em momentum extremo contra o gate ---
    if adx_val > adx_threshold:
        if direction == "BUY" and minus_di > plus_di and not uptrend:
            return None  # momentum de queda + ADX forte + preço abaixo da média -> não comprar
        if direction == "SELL" and plus_di > minus_di and not bullish_structure:
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
            "trend_filter": filter_state,
            "rsi": round(rsi, 2),
            "ema_fast": round(ema_fast_val, 2),
            "ema_slow": round(ema_slow_val, 2),
            "adx": round(adx_val, 2),
            "plus_di": round(plus_di, 2),
            "minus_di": round(minus_di, 2),
            "atr": round(atr, 2),
        },
    }