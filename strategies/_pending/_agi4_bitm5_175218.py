"""AGI4 BIT M5 — EMA-cross multi-timeframe + ATR-volatility filter.

Substitui MACD_MOMENTUM (WR 24.3%) por regime-switch: só entra quando a
EMÁ rápida (9) cruza a EMA lenta (21) na direção da EMA200 (regime de
tendência) e a volatilidade (ATR vs distribuição histórica) está acima
do percentil 50 — exatamente as janelas onde os 37 trades recentes de
MACD puro perderam em chop.

Aplica cooldown de 300s por slot (symbol+tf) via ``utils["cooldown_ok"]``
quando disponível, e respeita o contrato de plugins do autotrader
(retorna ``None`` ou ``dict`` com ``sl_pts`` calculado pelo ``utils``
fornecido — sem nada importado).
"""

STRATEGY_NAME = "AGI4_BIT_175218"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Avalia entrada long/short BIT_M5 com EMA-cross + ATR-vol filter."""

    # ---- parâmetros com defaults seguros -----------------------------
    ema_fast = int(params.get("ema_fast", 9))
    ema_slow = int(params.get("ema_slow", 21))
    ema_trend = int(params.get("ema_trend", 200))
    atr_period = int(params.get("atr_period", 14))
    atr_lookback = int(params.get("atr_lookback", 100))
    atr_min_pct = float(params.get("atr_min_percentile", 50.0))
    cooldown_s = int(params.get("cooldown_seconds", 300))

    # ATR em pontos obrigatório (LEI 3 + sanity check)
    if atr is None or atr <= 0 or len(bars) < max(ema_trend, atr_lookback) + 2:
        return None

    # ---- indicadores ------------------------------------------------
    ema_f = utils["calculate_ema"](bars, ema_fast)
    ema_s = utils["calculate_ema"](bars, ema_slow)
    ema_t = utils["calculate_ema"](bars, ema_trend)

    if ema_f is None or ema_s is None or ema_t is None:
        return None
    if len(ema_f) < 2 or len(ema_s) < 2:
        return None

    ef_prev, ef_curr = ema_f[-2], ema_f[-1]
    es_prev, es_curr = ema_s[-2], ema_s[-1]
    trend_curr = ema_t[-1]

    # ---- ATR-volatility filter (percentil sobre lookback) -----------
    atr_pct = None
    if atr_lookback >= 20 and atr_period < len(bars):
        atr_series = utils["calculate_atr"](bars, atr_period)
        window = atr_series[-atr_lookback:] if atr_series is not None else None
        if window is not None and len(window) >= 20:
            sorted_w = sorted(window)
            rank = sum(1 for x in sorted_w if x < atr)
            atr_pct = 100.0 * rank / max(1, len(sorted_w))
            if atr_pct < atr_min_pct:
                return None

    # ---- coerente com regime: preço vs EMA200 -----------------------
    if price is None or trend_curr is None:
        return None
    bull_regime = price > trend_curr
    bear_regime = price < trend_curr

    # ---- cooldown por slot, se o utils oferecer ---------------------
    if cooldown_s > 0 and "cooldown_ok" in utils:
        if not utils["cooldown_ok"](symbol, tf, cooldown_s, bar_ts):
            return None

    # ---- detecção de cruzamento -------------------------------------
    crossed_up = ef_prev <= es_prev and ef_curr > es_curr
    crossed_dn = ef_prev >= es_prev and ef_curr < es_curr

    info = {
        "ema_fast": ef_curr,
        "ema_slow": es_curr,
        "ema_trend": trend_curr,
        "cross_up": crossed_up,
        "cross_down": crossed_dn,
        "atr_percentile": atr_pct,
    }

    # Long: cruzamento de alta + regime de alta
    if crossed_up and bull_regime:
        sl_pts = utils["calc_sl"](symbol, atr, params)
        if sl_pts is None or sl_pts <= 0:
            return None
        return {
            "direction": "BUY",
            "sl_pts": int(sl_pts),
            "info": {**info, "regime": "BULL"},
        }

    # Short: cruzamento de baixa + regime de baixa
    if crossed_dn and bear_regime:
        sl_pts = utils["calc_sl"](symbol, atr, params)
        if sl_pts is None or sl_pts <= 0:
            return None
        return {
            "direction": "SELL",
            "sl_pts": int(sl_pts),
            "info": {**info, "regime": "BEAR"},
        }

    return None