"""
AGI4_WIN_171517 — Lógica condicionada a regime para WIN_H1.

Substitui reversão pura (contra-tendência) em WIN_H1 por lógica que respeita o
regime de mercado: WIN é o mini índice de day trade da B3 (futuro do Ibovespa),
ativo com forte componente direcional intradiário — reversão em H1 (barra de
1h, poucos sinais/dia) é incompatível com esse perfil. O estudo de reversão RSI
em futuros de índice mostra dependência de regime: a mesma lógica que perde em
tendência ganha em range, então o gate de regime é a alavanca correta.

Entrada (2 ramos mutuamente exclusivos, sem empilhamento de filtros):

1) REGIME TRENDING (ADX >= adx_trend_min E EMAs inclinadas):
   - Operar A FAVOR da tendência (momentum/breakout):
     BUY : EMA rápida > EMA lenta E +DI > -DI E preço acima da EMA lenta.
     SELL: EMA rápida < EMA lenta E -DI > +DI E preço abaixo da EMA lenta.

2) REGIME RANGING (ADX < adx_trend_min E EMAs sem inclinação — distância
   fast/slow < ema_flat_atr_mult * ATR):
   - Operar reversão às Bandas de Bollinger com confirmação RSI, somente com
     o preço DENTRO das bandas:
     SELL: preço no meio-superior da banda E RSI >= rsi_ob (sobrecomprado).
     BUY : preço no meio-inferior da banda E RSI <= rsi_os (sobrevendido).

Contrato: utils retorna escalares/tuplas — NUNCA indexar com [-1], len(), ou
.get(). Lei 3: SL obrigatório via calc_sl. Sem imports (sandbox).
"""

STRATEGY_NAME = "AGI4_WIN_171517"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Verifica sinal AGI4_WIN_171517. Retorna None ou dict de sinal."""
    calculate_ema = utils["calculate_ema"]
    calculate_rsi = utils["calculate_rsi"]
    calculate_adx = utils["calculate_adx"]
    calculate_bollinger = utils["calculate_bollinger"]
    calc_sl = utils["calc_sl"]

    # --- Params ---
    ema_fast_period = params.get("ema_fast", 9)
    ema_slow_period = params.get("ema_slow", 21)
    adx_period = params.get("adx_period", 14)
    adx_trend_min = params.get("adx_trend_min", 25)
    bb_period = params.get("bb_period", 20)
    bb_std = params.get("bb_std", 2.0)
    rsi_period = params.get("rsi_period", 14)
    rsi_ob = params.get("rsi_ob", 68)
    rsi_os = params.get("rsi_os", 32)
    ema_flat_atr_mult = params.get("ema_flat_atr_mult", 0.35)

    # --- Guardas de warmup ---
    min_bars = max(ema_slow_period, adx_period * 2, bb_period) + 5
    if not bars or len(bars) < min_bars:
        return None
    if atr <= 0 or price <= 0:
        return None

    # --- Indicadores (retornos escalares — uso direto, sem indexação) ---
    ema_fast_val = calculate_ema(bars, ema_fast_period)
    ema_slow_val = calculate_ema(bars, ema_slow_period)
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    rsi = calculate_rsi(bars, rsi_period)

    if ema_fast_val == 0 or ema_slow_val == 0 or adx_val == 0:
        return None

    # --- Regime: inclinação das EMAs (proxy de tendência, em unidades de ATR) ---
    ema_dist = abs(ema_fast_val - ema_slow_val)
    ema_flat = ema_dist < ema_flat_atr_mult * atr

    direction = None
    regime = None
    logic = None

    if adx_val >= adx_trend_min and not ema_flat:
        # --- REGIME TRENDING: momentum/breakout a favor da tendência ---
        regime = "TRENDING"
        logic = "momentum_ema_di"
        if ema_fast_val > ema_slow_val and plus_di > minus_di and price > ema_slow_val:
            direction = "BUY"
        elif ema_fast_val < ema_slow_val and minus_di > plus_di and price < ema_slow_val:
            direction = "SELL"
    elif adx_val < adx_trend_min and ema_flat:
        # --- REGIME RANGING: reversão às bandas com confirmação RSI ---
        upper, mid, lower = calculate_bollinger(bars, bb_period, bb_std)
        if upper == 0 or mid == 0 or lower == 0:
            return None

        regime = "RANGING"
        logic = "bb_rsi_reversal"
        # Reversão somente com o preço DENTRO das bandas (sem esticada)
        if lower < price < upper:
            if price >= mid and rsi >= rsi_ob and price < upper:
                direction = "SELL"  # meio-superior + sobrecomprado
            elif price <= mid and rsi <= rsi_os and price > lower:
                direction = "BUY"  # meio-inferior + sobrevendido

    if direction is None:
        return None

    # --- Lei 3: SL obrigatório via calc_sl ---
    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": STRATEGY_NAME,
            "entry_price": price,
            "atr": round(atr, 2),
            "regime": regime,
            "adx": round(adx_val, 2),
            "plus_di": round(plus_di, 2),
            "minus_di": round(minus_di, 2),
            "ema_fast": round(ema_fast_val, 2),
            "ema_slow": round(ema_slow_val, 2),
            "ema_dist": round(ema_dist, 2),
            "rsi": round(rsi, 2),
            "logic": logic,
        },
    }
