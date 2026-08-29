"""
AGI4_WIN_061355 — Reversão de extremo com filtro de tendência (EMA 50 do H1)
para WIN_H1.

Tese:
  Reversões compradas no oversold durante downtrend tendem a ser 'pegar faca
  caindo' em um contrato de índice futuro alavancado e com forte participação
  de day traders (fato web 1, B3). Médias móveis estão entre as ferramentas de
  análise técnica padrão para Mini Bovespa Futures (fato web 3, TradingView).
  O filtro de EMA 50 alinha a reversão à tendência dominante do H1:

    1. BUY (reversão de oversold) só é aceita com preço ACIMA da EMA 50 —
       oversold dentro de estrutura bullish = pullback comprável.
    2. SELL (reversão de overbought) só é aceita com preço ABAIXO da EMA 50 —
       overbought dentro de estrutura bearish = pullback vendável.
    3. Reversões CONTRA a tendência dominante do timeframe são descartadas:
       a assimetria 'faca caindo / rally contra' é exatamente o que o filtro
       ataca.

Contrato:
  - utils retorna escalares/tuplas fixas — NUNCA indexar o retorno com
    [-1], len() ou .get(). Usar o resultado diretamente.
  - Lei 3: TODO sinal retorna sl_pts via calc_sl(symbol, atr, params).
  - SANDBOX: sem imports (sem os, subprocess, mt5). Tudo via utils e params.
"""

STRATEGY_NAME = "AGI4_WIN_061355"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Verifica sinal AGI4_WIN_061355.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calculate_ema = utils["calculate_ema"]
    calculate_rsi = utils["calculate_rsi"]
    calc_sl = utils["calc_sl"]

    # --- Params (sempre com default defensivo) ---
    ema_trend_period = params.get("ema_trend", 50)
    rsi_period = params.get("rsi_period", 14)
    rsi_oversold = params.get("rsi_oversold", 30)
    rsi_overbought = params.get("rsi_overbought", 70)

    # --- Guardas de warmup ---
    min_bars = max(ema_trend_period, rsi_period * 2) + 5
    if not bars or len(bars) < min_bars:
        return None
    if atr <= 0 or price <= 0:
        return None

    # --- Indicadores atuais (retornos escalares — uso direto) ---
    ema_trend_val = calculate_ema(bars, ema_trend_period)
    rsi = calculate_rsi(bars, rsi_period)

    if ema_trend_val == 0:
        return None

    # --- Filtro de tendência (EMA 50 do H1) + reversão de extremo ---
    direction = None
    logic = None

    # BUY: oversold + preço ACIMA da EMA50 (reversão a favor da tendência)
    if price > ema_trend_val and rsi <= rsi_oversold:
        direction = "BUY"
        logic = "reversion_buy_with_trend"

    # SELL: overbought + preço ABAIXO da EMA50 (reversão a favor da tendência)
    elif price < ema_trend_val and rsi >= rsi_overbought:
        direction = "SELL"
        logic = "reversion_sell_with_trend"

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
            "ema_trend": round(ema_trend_val, 2),
            "rsi": round(rsi, 2),
            "atr": round(atr, 2),
        },
    }
