"""
Estratégia VWAP_VALUE_AREA — Wave 5.2 (2026-06-26).

Edge mecânico:
  - O VWAP representa o "preço justo" do dia ponderado por volume.
  - Desvios-padrão do VWAP formam bandas (σ, 2σ, 3σ) que funcionam
    como níveis de "value area" institucionais.
  - Em mercados ranging, preço que toca +1σ tende a reverter para
    o VWAP; que toca -1σ tende a voltar para cima.
  - Edge documentado em day-trade B3 (WIN/WDO) e em futuros CME.

Sinal (mean reversion):
  - BUY:  preço toca -1σ do VWAP + RSI < 35 (sobrevenda)
  - SELL: preço toca +1σ do VWAP + RSI > 65 (sobrecompra)

Filtros:
  - ADX < 25 (mercado RANGING — se trending, BB extension vira continuation)
  - ATR mínimo (evita dias parados)
  - Volume confirma (ratio > 0.5)

Defensivo:
  - sl_atr_mult=1.5, cooldown_seconds=300
  - SL: 1.5 × ATR (coloca stop do lado de fora da value area)

Parâmetros (defaults):
  vwap_period=30, stddev_band=1.0, rsi_period=14,
  rsi_oversold=35, rsi_overbought=65,
  adx_threshold=25, vol_ratio_min=0.5,
  sl_atr_mult=1.5, cooldown_seconds=300

Notas:
  - stddev calculado sobre TODAS as barras do dia (close-to-VWAP distance)
  - Funciona melhor em WIN/WDO M5/M15 onde reversão à VWAP é estatística
    forte.
"""
STRATEGY_NAME = "VWAP_VALUE_AREA"

DEFAULT_PARAMS = {
    "vwap_period": 30,
    "stddev_period": 30,        # barras para calcular σ
    "stddev_band": 1.0,         # ±1σ = value area clássica
    "rsi_period": 14,
    "rsi_oversold": 35,
    "rsi_overbought": 65,
    "adx_period": 14,
    "adx_threshold": 25,        # ADX < 25 = mercado ranging
    "vol_ratio_min": 0.5,
    "atr_period": 14,
    "sl_atr_mult": 1.5,
    "cooldown_seconds": 300,
}


def _compute_vwap_and_stddev(bars, period):
    """Calcula VWAP + desvio-padrão dos preços vs VWAP (bandas).

    Retorna (vwap, upper_band, lower_band) ou (0, 0, 0) se insuficiente.

    O σ é calculado sobre o módulo da distância (close - VWAP) usando
    todas as barras do período — captura a "largura" típica do dia.
    """
    if not bars or len(bars) < period:
        return 0.0, 0.0, 0.0

    data = bars[:period]

    # VWAP padrão: typical price * volume / volume
    sum_pv = 0.0
    sum_v = 0.0
    for b in data:
        typical = (b["high"] + b["low"] + b["close"]) / 3.0
        vol = max(b.get("volume", 0), b.get("tick_volume", 0), 1)
        sum_pv += typical * vol
        sum_v += vol

    if sum_v <= 0:
        return 0.0, 0.0, 0.0

    vwap = sum_pv / sum_v

    # σ sobre distância do close ao VWAP (proxy de dispersão)
    diffs_sq = []
    for b in data:
        d = b["close"] - vwap
        diffs_sq.append(d * d)
    variance = sum(diffs_sq) / len(diffs_sq)
    stddev = variance ** 0.5

    return vwap, vwap + stddev, vwap - stddev


def _avg_volume(bars, n=20):
    """Média de volume das últimas N barras."""
    if not bars:
        return 0
    sample = bars[:n]
    vols = [b.get("tick_volume", 0) or b.get("volume", 0) for b in sample]
    if not vols:
        return 0
    return sum(vols) / len(vols)


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Verifica sinal de entrada VWAP_VALUE_AREA.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    p = {**DEFAULT_PARAMS, **(params or {})}

    calc_sl = utils["calc_sl"]
    calculate_vwap = utils.get("calculate_vwap")
    calculate_rsi = utils.get("calculate_rsi")
    calculate_adx = utils.get("calculate_adx")
    calculate_atr = utils.get("calculate_atr")

    if not bars or len(bars) < 50:
        return None
    if atr is None or atr <= 0:
        return None

    # VWAP + bandas
    vwap = upper = lower = 0.0
    if calculate_vwap is not None:
        vwap = calculate_vwap(bars, p["vwap_period"])
        if vwap <= 0:
            return None
        # Calcula stddev localmente (não temos calculate_stddev nas utils)
        vwap, upper, lower = _compute_vwap_and_stddev(bars, p["stddev_period"])
        if vwap <= 0 or upper <= 0 or lower <= 0:
            return None
    else:
        return None  # sem VWAP não tem edge

    # Ajusta banda pelo parâmetro stddev_band
    # upper/lower estão em ±1σ; multiplica para ±Nσ
    if p["stddev_band"] != 1.0:
        sigma = (upper - vwap)  # banda calculada em ±1σ
        upper = vwap + sigma * p["stddev_band"]
        lower = vwap - sigma * p["stddev_band"]

    # Filtro ADX (mercado RANGING — ADX < threshold)
    adx_val = 0
    if calculate_adx is not None:
        adx_tuple = calculate_adx(bars, p["adx_period"])
        if adx_tuple is None:
            return None
        if isinstance(adx_tuple, tuple):
            adx_val = adx_tuple[0]
        else:
            adx_val = adx_tuple
        if adx_val > p["adx_threshold"]:
            return None  # trending demais — mean reversion falha

    # RSI
    rsi = 50
    if calculate_rsi is not None:
        rsi = calculate_rsi(bars, p["rsi_period"])
        if rsi is None:
            return None

    # Filtro de volatilidade mínima
    if calculate_atr is not None:
        atr_calc = calculate_atr(bars, p["atr_period"])
        if atr_calc > 0 and atr_calc < atr * 0.5:
            return None

    # Filtro de volume
    avg_vol = _avg_volume(bars, n=20)
    last_bar = bars[0] if bars else {}
    last_vol = last_bar.get("tick_volume", 0) or last_bar.get("volume", 0) or 0
    vol_ratio = None
    if avg_vol > 0 and last_vol > 0:
        vol_ratio = last_vol / avg_vol
        if vol_ratio < p["vol_ratio_min"]:
            return None

    # Sinal de mean reversion
    # BUY: preço toca ou rompe lower band + RSI sobrevenda
    # SELL: preço toca ou rompe upper band + RSI sobrecompra
    # Usamos tolerância (preço <= lower * 1.001 ou >= upper * 0.999) para
    # capturar toques sem exigir rompimento agressivo.
    tolerance = atr * 0.05
    direction = None

    if price <= lower + tolerance and rsi < p["rsi_oversold"]:
        direction = "BUY"
    elif price >= upper - tolerance and rsi > p["rsi_overbought"]:
        direction = "SELL"

    if not direction:
        return None

    sl_pts = calc_sl(symbol, atr, p)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "VWAP_VALUE_AREA",
            "vwap": round(vwap, 4),
            "upper_band": round(upper, 4),
            "lower_band": round(lower, 4),
            "distance_to_band_pct": round(
                (price - vwap) / vwap * 100, 3
            ) if vwap > 0 else None,
            "rsi": round(rsi, 1),
            "adx": round(adx_val, 1),
            "vol_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
            "atr": atr,
        },
    }
