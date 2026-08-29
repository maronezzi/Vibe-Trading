"""
AGI4_WSP_134734 — Estratégia Adaptativa de Regime para WSP_M15
================================================================
Abordagem diferente das 30 existentes: em vez de um mecanismo único
(trend-following OU mean-reversion OU breakout), esta estratégia
classifica o regime de mercado via ADX e opera de forma OPOSTA em
cada regime:

  - Regime de TREND (ADX forte): continuação — entrada na correção
    (pullback) contra a EMA rápida, apenas na direção da tendência,
    confirmada por +DI/-DI. Evita adivinhar topo/fundo.
  - Regime de RANGE (ADX fraco): reversão — fade das extremidades
    das Bandas de Bollinger + RSI extremo. Evita perseguir ruído.

Isso a torna robusta porque a maioria dos pares oscila entre trending
e lateral; uma única lógica falha em pelo menos um dos regimes.

Indicadores usados (contrato utils):
  calculate_ema       -> float
  calculate_rsi       -> float
  calculate_adx       -> (adx, plus_di, minus_di)
  calculate_bollinger -> (upper, mid, lower)
  calc_sl             -> int (sl_pts)

Sandbox puro: nenhum import; tudo vem via params/utils.
"""

STRATEGY_NAME = "AGI4_WSP_134734"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    # --- Helpers locais (contrato de indicadores) ---
    calculate_ema = utils["calculate_ema"]
    calculate_rsi = utils["calculate_rsi"]
    calculate_adx = utils["calculate_adx"]
    calculate_bollinger = utils["calculate_bollinger"]
    calc_sl = utils["calc_sl"]

    # --- Parâmetros (com defaults) ---
    ema_fast_period = params.get("ema_fast", 9)
    ema_slow_period = params.get("ema_slow", 21)
    adx_period = params.get("adx_period", 14)
    bb_period = params.get("bb_period", 20)
    bb_std = params.get("bb_std", 2.0)
    rsi_period = params.get("rsi_period", 14)

    adx_trend = params.get("adx_trend", 24.0)     # ADX >= => regime TREND
    adx_range = params.get("adx_range", 18.0)     # ADX <= => regime RANGE
    rsi_ob = params.get("rsi_overbought", 72.0)   # RSI overbought (fade)
    rsi_os = params.get("rsi_oversold", 28.0)     # RSI oversold (fade)
    pullback_tol = params.get("pullback_tol", 0.0015)  # tolr. p/ pullback na EMA (fração do preço)
    band_zone = params.get("band_zone", 0.15)     # p/ considerar "próximo" de uma banda (fração da largura)

    # --- Guardas de dados mínimos ---
    min_bars = max(ema_slow_period, adx_period * 2, bb_period) + 5
    if not bars or len(bars) < min_bars:
        return None
    if atr <= 0:
        return None

    # --- Indicadores (retornos escalares/tuplas, usados DIRETAMENTE) ---
    ema_fast_val = calculate_ema(bars, ema_fast_period)
    ema_slow_val = calculate_ema(bars, ema_slow_period)
    upper, mid, lower = calculate_bollinger(bars, bb_period, bb_std)
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    rsi = calculate_rsi(bars, rsi_period)

    # --- Validação de valores calculáveis ---
    if ema_fast_val == 0 or ema_slow_val == 0 or mid == 0:
        return None
    if adx_val == 0 or rsi == 0:
        return None

    band_width = upper - lower
    if band_width <= 0:
        return None

    # tolerância de preço em pontos (usando ATR como referência de escala)
    tol = max(pullback_tol * price, atr * 0.15)

    direction = None
    reason = ""

    # ============================================================
    # REGIME 1: TREND (ADX forte) -> continuação no pullback
    # ============================================================
    if adx_val >= adx_trend:
        uptrend = ema_fast_val > ema_slow_val and plus_di > minus_di
        downtrend = ema_fast_val < ema_slow_val and minus_di > plus_di

        if uptrend and rsi < rsi_ob:
            # preço recuou até a EMA rápida mas segue acima da média
            if price <= ema_fast_val + tol and price >= mid:
                direction = "BUY"
                reason = "trend_up_pullback"

        elif downtrend and rsi > rsi_os:
            # preço recuou até a EMA rápida mas segue abaixo da média
            if price >= ema_fast_val - tol and price <= mid:
                direction = "SELL"
                reason = "trend_down_pullback"

    # ============================================================
    # REGIME 2: RANGE (ADX fraco) -> reversão nas bandas + RSI
    # ============================================================
    elif adx_val <= adx_range:
        # quão perto o preço está de cada banda (fração da largura)
        touch_upper = (upper - price) / band_width
        touch_lower = (price - lower) / band_width

        if touch_upper <= band_zone and rsi >= rsi_ob:
            # topo da banda + sobrecompra -> fade de venda
            direction = "SELL"
            reason = "range_band_upper"

        elif touch_lower <= band_zone and rsi <= rsi_os:
            # fundo da banda + sobrevenda -> fade de compra
            direction = "BUY"
            reason = "range_band_lower"

        # sem sinal em regime intermediário (weak trend) -> aguardar

    # ============================================================
    # Saída
    # ============================================================
    if direction is None:
        return None

    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": STRATEGY_NAME,
            "reason": reason,
            "adx": round(adx_val, 2),
            "plus_di": round(plus_di, 2),
            "minus_di": round(minus_di, 2),
            "rsi": round(rsi, 2),
            "ema_fast": round(ema_fast_val, 2),
            "ema_slow": round(ema_slow_val, 2),
            "bb_upper": round(upper, 2),
            "bb_mid": round(mid, 2),
            "bb_lower": round(lower, 2),
            "regime": "trend" if adx_val >= adx_trend else ("range" if adx_val <= adx_range else "weak"),
            "atr": round(atr, 2),
        },
    }
