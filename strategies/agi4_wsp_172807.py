"""AGI4_WSP_172807 — Gate de qualidade de entrada para WSP_M5.

Filtra crossings EMA em range de baixa volatilidade (ATR relativo minimo +
momentum minimo via ADX) para descartar sinais falsos onde o stop
(sl_atr_mult=1.8) e varrido antes de qualquer movimento a favor.
Sem import de modulos externos: tudo chega via utils e params (SANDBOX).
"""

STRATEGY_NAME = "AGI4_WSP_172807"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    calculate_ema = utils["calculate_ema"]
    calculate_rsi = utils["calculate_rsi"]
    calculate_adx = utils["calculate_adx"]
    calculate_bollinger = utils["calculate_bollinger"]
    calc_sl = utils["calc_sl"]

    ema_fast_period = params.get("ema_fast", 9)
    ema_slow_period = params.get("ema_slow", 21)
    adx_period = params.get("adx_period", 14)
    rsi_period = params.get("rsi_period", 14)
    boll_period = params.get("boll_period", 20)
    boll_std = params.get("boll_std", 2.0)

    # Gates de qualidade de entrada
    min_atr_ratio = params.get("min_atr_ratio", 0.0006)   # volatilidade relativa minima
    min_adx = params.get("min_adx", 20)                   # momentum minimo
    min_bars = max(ema_slow_period, adx_period * 2, boll_period) + 5

    if not bars or len(bars) < min_bars:
        return None
    if atr <= 0 or price <= 0:
        return None

    # Gate de volatilidade: descarta range de baixa volatilidade (Fato web #4).
    # Nesses trechos o ATR-based stop e varrido por ruido, sem edge.
    atr_ratio = atr / price
    if atr_ratio < min_atr_ratio:
        return None

    ema_fast_val = calculate_ema(bars, ema_fast_period)
    ema_slow_val = calculate_ema(bars, ema_slow_period)
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    rsi = calculate_rsi(bars, rsi_period)
    boll_upper, boll_mid, boll_lower = calculate_bollinger(bars, boll_period, boll_std)

    if ema_fast_val == 0 or ema_slow_val == 0 or adx_val == 0:
        return None

    # Gate de momentum: exige tendencia minima (evita crossings/ruido em range)
    if adx_val < min_adx:
        return None

    direction = None
    # Cross de alta: EMA fast acima da slow, DI+ dominando, RSI sem excesso
    if ema_fast_val > ema_slow_val and plus_di > minus_di and rsi < 75:
        direction = "BUY"
    # Cross de baixa: EMA fast abaixo da slow, DI- dominando, RSI sem excesso
    elif ema_fast_val < ema_slow_val and minus_di > plus_di and rsi > 25:
        direction = "SELL"

    if direction is None:
        return None

    sl_pts = calc_sl(symbol, atr, params)
    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "ema_fast": round(ema_fast_val, 4),
            "ema_slow": round(ema_slow_val, 4),
            "adx": round(adx_val, 2),
            "plus_di": round(plus_di, 2),
            "minus_di": round(minus_di, 2),
            "rsi": round(rsi, 2),
            "boll_upper": round(boll_upper, 4),
            "boll_mid": round(boll_mid, 4),
            "boll_lower": round(boll_lower, 4),
            "atr_ratio": round(atr_ratio, 6),
            "min_atr_ratio": min_atr_ratio,
            "min_adx": min_adx,
        },
    }
