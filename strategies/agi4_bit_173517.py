"""
AGI4_BIT_173517 — Breakout de Bollinger com confirmação de tendência (EMA + ADX) para BIT_H1.

Tese:
  BIT é o futuro de Bitcoin da B3 (micro bitcoin, lote de 0,1 BTC). Futuros de
  Bitcoin (e Micro Bitcoin) possuem família comprovada de estratégias de
  momentum/breakout no banco de estratégias — a ADX_TREND atual com config
  inerte não é uma delas. Esta estratégia implementa a família de breakout com
  confirmação de tendência: saída de banda de Bollinger na direção do
  alinhamento de EMAs + direcionalidade do ADX (+DI/-DI) + confirmação de RSI.

  O ADX_TREND atual tem params parcialmente mortos para BIT_H1; aqui TODOS os
  params ficam expostos via params.get() para o exhaustive search do AGI
  (todas as estratégias x grid de params) selecionar o melhor combo para
  BIT_H1 antes de aplicar. Como BIT_H1 está sem dados em 7d e desabilitado, a
  troca não sacrifica edge existente e segue a regra imperativa de testar
  todas as estratégias por par.

  Lógica de entrada:
    - BUY  (momentum de alta): price > banda superior E ema_fast > ema_slow
      E +DI >= -DI E rsi > rsi_buy_min E (adx >= adx_threshold se filtro ativo).
    - SELL (momentum de baixa): price < banda inferior E ema_fast < ema_slow
      E -DI >= +DI E rsi < rsi_sell_max E (adx >= adx_threshold se filtro ativo).
    - Sem saída de banda ou sem confirmação -> None (sem sinal).

Contrato:
  - utils retorna escalares/tuplas fixas — NUNCA indexar com [-1], len(), ou
    .get(). Usar o resultado diretamente.
  - Lei 3: TODO sinal retorna sl_pts via calc_sl(symbol, atr, params).
  - SANDBOX: sem imports (sem os, subprocess, mt5). Tudo via utils e params.
"""

STRATEGY_NAME = "AGI4_BIT_173517"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Verifica sinal AGI4_BIT_173517.

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
    ema_fast_period = params.get("ema_fast", 9)
    ema_slow_period = params.get("ema_slow", 21)
    adx_period = params.get("adx_period", 14)
    adx_threshold = params.get("adx_threshold", 22)
    use_adx_filter = params.get("use_adx_filter", True)
    rsi_period = params.get("rsi_period", 14)
    rsi_buy_min = params.get("rsi_buy_min", 55)
    rsi_sell_max = params.get("rsi_sell_max", 45)

    # --- Guardas de warmup ---
    min_bars = max(bb_period, ema_slow_period, ema_fast_period, adx_period * 2, rsi_period) + 5
    if not bars or len(bars) < min_bars:
        return None
    if atr <= 0 or price <= 0:
        return None

    # --- Indicadores (retornos escalares/tuplas — uso direto) ---
    ema_fast_val = calculate_ema(bars, ema_fast_period)
    ema_slow_val = calculate_ema(bars, ema_slow_period)
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    rsi = calculate_rsi(bars, rsi_period)
    upper, mid, lower = calculate_bollinger(bars, bb_period, bb_std)

    if ema_fast_val == 0 or ema_slow_val == 0 or adx_val == 0 or upper == 0 or lower == 0:
        return None

    # --- Confirmação de tendência ---
    bullish_alignment = ema_fast_val > ema_slow_val
    bearish_alignment = ema_fast_val < ema_slow_val
    bullish_di = plus_di >= minus_di
    bearish_di = minus_di >= plus_di
    adx_ok = (not use_adx_filter) or adx_val >= adx_threshold

    direction = None
    logic = None

    # --- Breakout de banda com confirmação ---
    if price > upper:
        # Momentum de alta: banda superior rompida + alinhamento + direcionalidade + RSI
        if bullish_alignment and bullish_di and rsi > rsi_buy_min and adx_ok:
            direction = "BUY"
            logic = "bb_breakout_up"
    elif price < lower:
        # Momentum de baixa: banda inferior rompida + alinhamento + direcionalidade + RSI
        if bearish_alignment and bearish_di and rsi < rsi_sell_max and adx_ok:
            direction = "SELL"
            logic = "bb_breakout_down"

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
            "bb_upper": round(upper, 2),
            "bb_mid": round(mid, 2),
            "bb_lower": round(lower, 2),
            "ema_fast": round(ema_fast_val, 2),
            "ema_slow": round(ema_slow_val, 2),
            "adx": round(adx_val, 2),
            "plus_di": round(plus_di, 2),
            "minus_di": round(minus_di, 2),
            "rsi": round(rsi, 2),
            "atr": round(atr, 2),
        },
    }