"""
AGI4_BIT_214244 — Scalp rápido para BIT_H1 (substitui ADX_TREND trend-following).

Tese:
  Para cripto futures (BTC/BIT), a abordagem mais comprovada e segura é
  SCALP com alavancagem limitada a <=10x — entradas e saídas rápidas, TP
  curto e cooldown reduzido. O ADX_TREND atual opera H1 com cooldown de
  120s e holding longo (perfil de trend-following), o oposto do que o
  ativo recomenda: em Bitcoin Futures, sistemas específicos de scalp
  superam o ADX genérico.

  Esta estratégia opera o pullback dentro da micro-tendência (EMA 5/16 em
  H1), comprando a correção em tendência de alta e vendendo o rali em
  tendência de baixa, com RSI de período curto (escala scalp) como
  gatilho e Bollinger como limite de zona:

    - BUY:  ema_fast > ema_slow (micro alta) + RSI < rsi_buy_trigger
            (correção) + preço entre banda inferior e média (não em
            breakdown)
    - SELL: ema_fast < ema_slow (micro baixa) + RSI > rsi_sell_trigger
            (rali) + preço entre média e banda superior

  Holding curto: sem trailing agressivo, TP curto configurado à parte
  (take_profit_pts), cooldown reduzido (ex.: 45-60s) e alavancagem <=10x
  definidos no vt_config.json — gestão fica FORA dos TUNABLE_PARAMS.

Contrato:
  - utils retorna escalares/tuplas fixas — NUNCA indexar com [-1], len(),
    ou .get() no RESULTADO. Usar o retorno diretamente.
  - Lei 3: TODO sinal retorna sl_pts via calc_sl(symbol, atr, params).
  - SANDBOX: sem imports (sem os, subprocess, mt5). Tudo via utils e params.
  - TUNABLE_PARAMS: apenas params de setup (otimizados pelo AGI). Params de
    gestão (sl_atr_mult, cooldown_seconds) ficam fora — otimizados à parte.
"""

STRATEGY_NAME = "AGI4_BIT_214244"

TUNABLE_PARAMS = {
    "ema_fast": (int, 3, 10),
    "ema_slow": (int, 12, 24),
    "rsi_period": (int, 2, 7),
    "rsi_buy_trigger": (float, 35.0, 50.0),
    "rsi_sell_trigger": (float, 50.0, 65.0),
}


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Verifica sinal AGI4_BIT_214244 (scalp H1).

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calculate_ema = utils["calculate_ema"]
    calculate_rsi = utils["calculate_rsi"]
    calculate_bollinger = utils["calculate_bollinger"]
    calc_sl = utils["calc_sl"]

    # --- Params (sempre com default defensivo) ---
    ema_fast_period = params.get("ema_fast", 5)
    ema_slow_period = params.get("ema_slow", 16)
    rsi_period = params.get("rsi_period", 3)
    rsi_buy_trigger = params.get("rsi_buy_trigger", 45.0)
    rsi_sell_trigger = params.get("rsi_sell_trigger", 55.0)
    bb_period = params.get("bb_period", 20)
    bb_std = params.get("bb_std", 2.0)

    # --- Guardas de warmup ---
    min_bars = max(ema_slow_period, bb_period, rsi_period * 4) + 5
    if not bars or len(bars) < min_bars:
        return None
    if atr <= 0 or price <= 0:
        return None

    # --- Indicadores (retornos escalares — uso direto) ---
    ema_fast_val = calculate_ema(bars, ema_fast_period)
    ema_slow_val = calculate_ema(bars, ema_slow_period)
    rsi = calculate_rsi(bars, rsi_period)
    upper, mid, lower = calculate_bollinger(bars, bb_period, bb_std)

    if ema_fast_val == 0 or ema_slow_val == 0 or rsi == 0:
        return None
    if upper == 0 or mid == 0 or lower == 0:
        return None

    # --- Lógica de scalp (pullback dentro da micro-tendência) ---
    direction = None
    trigger = None

    # BUY: micro alta + correção (RSI baixo) + preço na zona inferior
    if ema_fast_val > ema_slow_val:
        if rsi < rsi_buy_trigger:
            if lower < price <= mid:
                direction = "BUY"
                trigger = "pullback_buy_uptrend"

    # SELL: micro baixa + rali (RSI alto) + preço na zona superior
    if direction is None:
        if ema_fast_val < ema_slow_val:
            if rsi > rsi_sell_trigger:
                if mid <= price < upper:
                    direction = "SELL"
                    trigger = "rally_sell_downtrend"

    if direction is None or trigger is None:
        return None

    # --- Lei 3: SL obrigatório via calc_sl ---
    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": STRATEGY_NAME,
            "entry_price": price,
            "trigger": trigger,
            "rsi": round(rsi, 2),
            "ema_fast": round(ema_fast_val, 2),
            "ema_slow": round(ema_slow_val, 2),
            "bb_upper": round(upper, 2),
            "bb_mid": round(mid, 2),
            "bb_lower": round(lower, 2),
            "atr": round(atr, 2),
            "leverage_max": "10x",  # limite operacional — config, não código
        },
    }
