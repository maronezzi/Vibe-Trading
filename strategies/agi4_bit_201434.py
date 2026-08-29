"""
Estratégia AGI4_BIT_201434 — VWAP Deviation + Volume Climax + Exhaustion
projetada especificamente para BIT_M30 (Mini Bitcoin / B3).

Motivação
---------
27 estratégias testadas em BIT não entregaram edge em M30. O padrão dominante
em BTC é *tendência com exaustões abruptas*: o preço se afasta do "preço justo"
(intraday VWAP) até um ponto onde a liquidez se esgota — marcado por um candle
de rejeição com volume anômalo — antes de reverter parcial ou totalmente.

Nenhuma das estratégias anteriores combina exatamente este trio:
  - VWAP deviation (mede "quão longe o preço foi do justo")
  - Volume climax ratio (mede "exaustão de fluxo")
  - Exhaustion wick (mede "rejeição institucional")

Componentes individuais já foram tentados (VWAP sozinho, RSI_REVERSION,
BOLLINGER, candle patterns), mas a *combinação* VWAP-distance + volume-spike +
wick-rejection com filtro RSI extremo é nova.

Lógica (todas as condições devem ser satisfeitas):
  1. Distância do preço ao VWAP normalizada pelo ATR >= vwap_dev_min
     → preço esticou o suficiente para ancorar no VWAP
  2. Volume da vela atual >= volume_spike_mult × média das últimas N velas
     → climax de fluxo (exaustão)
  3. Pavio dominante na direção oposta ao trade (wick_reject_min)
     → rejeição institucional confirma exaustão
  4. RSI em zona extrema (rsi_os < rsi < rsi_ob dependendo da direção)
     → momentum esticado confirma reversão
  5. ADX mínimo (adx_min) garante volatilidade suficiente para reverter
  6. EMA fast/slow coerente com a direção do trade (slope já morreu)

Direção:
  BUY  → preço muito abaixo do VWAP + candle bullish rejeitando + RSI oversold
  SELL → preço muito acima do VWAP + candle bearish rejeitando + RSI overbought

Parâmetros (via vt_config.json → bit_m30):
  vwap_period, vwap_dev_min
  volume_avg_period, volume_spike_mult
  wick_reject_min
  rsi_period, rsi_os, rsi_ob
  adx_period, adx_min
  ema_fast, ema_slow
  sl_atr_mult, trail_activate, trail_distance
"""

STRATEGY_NAME = "AGI4_BIT_201434"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada AGI4_BIT_201434.

    Args:
        symbol:  ticker B3 (ex: "BITM26")
        tf:      timeframe string (ex: "M30")
        price:   preço de fechamento da vela atual (float)
        atr:     ATR já calculado para o símbolo (float)
        bar_ts:  timestamp da vela atual (int/str)
        bars:    lista de candles newest-first [{"open","high","low","close","volume"}, ...]
        params:  dict de parâmetros configurados
        utils:   dict com calculate_rsi / calculate_ema / calculate_bollinger /
                 calculate_adx / calc_sl / calculate_vwap

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calculate_ema = utils["calculate_ema"]
    calculate_rsi = utils["calculate_rsi"]
    calculate_adx = utils["calculate_adx"]
    calc_sl = utils["calc_sl"]
    # calculate_vwap pode não estar disponível em utils; tratado defensivamente.
    calculate_vwap = utils.get("calculate_vwap")

    # --- Parâmetros -----------------------------------------------------------
    vwap_period = params.get("vwap_period", 24)        # ~12h em M30
    vwap_dev_min = params.get("vwap_dev_min", 1.8)      # em múltiplos de ATR

    volume_avg_period = params.get("volume_avg_period", 20)
    volume_spike_mult = params.get("volume_spike_mult", 1.6)

    wick_reject_min = params.get("wick_reject_min", 0.45)

    rsi_period = params.get("rsi_period", 14)
    rsi_os = params.get("rsi_os", 30)
    rsi_ob = params.get("rsi_ob", 70)

    adx_period = params.get("adx_period", 14)
    adx_min = params.get("adx_min", 18)

    ema_fast_period = params.get("ema_fast", 8)
    ema_slow_period = params.get("ema_slow", 34)

    # --- Guardas --------------------------------------------------------------
    min_bars = max(
        vwap_period + 2 if calculate_vwap is not None else 0,
        volume_avg_period + 2,
        rsi_period + 2,
        adx_period * 2 + 2,
        ema_slow_period + 2,
    )
    if not bars or len(bars) < min_bars:
        return None
    if price is None or atr is None or atr <= 0:
        return None

    cur = bars[0]
    o = float(cur.get("open", 0))
    h = float(cur.get("high", 0))
    lo = float(cur.get("low", 0))
    c = float(cur.get("close", 0))
    vol = float(cur.get("volume", 0))
    if h <= lo or o == 0 or c == 0:
        return None

    # --- VWAP ----------------------------------------------------------------
    # calculate_vwap retorna o VWAP da vela atual (typical price * volume /
    # volume cumulativo). Se a função não estiver exposta via utils, caímos
    # num proxy: média ponderada por volume do range vwap_period.
    if calculate_vwap is not None:
        vwap = calculate_vwap(bars, vwap_period)
    else:
        # Proxy: soma(typical*vol)/soma(vol) sobre as últimas vwap_period velas
        window = bars[:vwap_period]
        num = 0.0
        den = 0.0
        for b in window:
            bh = float(b.get("high", 0))
            bl = float(b.get("low", 0))
            bc = float(b.get("close", 0))
            bv = float(b.get("volume", 0))
            if bh <= bl or bc == 0 or bv <= 0:
                continue
            typical = (bh + bl + bc) / 3.0
            num += typical * bv
            den += bv
        vwap = num / den if den > 0 else 0

    if vwap <= 0:
        return None

    # Distância do preço ao VWAP normalizada pelo ATR.
    vwap_dev = (price - vwap) / atr

    # --- Volume climax -------------------------------------------------------
    avg_vol = 0.0
    n_vol = 0
    for b in bars[1:volume_avg_period + 1]:
        v = float(b.get("volume", 0))
        if v > 0:
            avg_vol += v
            n_vol += 1
    avg_vol = avg_vol / n_vol if n_vol >= max(5, volume_avg_period // 2) else 0
    if avg_vol <= 0:
        return None
    vol_ratio = vol / avg_vol

    # --- Anatomia da vela atual (exaustão) -----------------------------------
    candle_range = h - lo
    body = c - o
    body_abs = abs(body)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - lo
    upper_wick_ratio = upper_wick / candle_range if candle_range > 0 else 0
    lower_wick_ratio = lower_wick / candle_range if candle_range > 0 else 0

    # Para BUY queremos pavio inferior dominante (vendedores rejeitados).
    # Para SELL queremos pavio superior dominante (compradores rejeitados).
    # wick_reject_min é fração mínima do range que o pavio oposto deve ocupar.

    # --- Indicadores base ----------------------------------------------------
    rsi = calculate_rsi(bars, rsi_period)
    adx = calculate_adx(bars, adx_period)
    ema_fast_now = calculate_ema(bars, ema_fast_period)
    ema_slow_now = calculate_ema(bars, ema_slow_period)

    if rsi == 0 or adx == 0 or ema_fast_now == 0 or ema_slow_now == 0:
        return None

    # --- Filtros universais --------------------------------------------------
    if vol_ratio < volume_spike_mult:
        return None
    if adx < adx_min:
        return None

    # --- Sinal BUY: VWAP stretched pra baixo + exhaustion bullish ------------
    if vwap_dev <= -vwap_dev_min and rsi <= rsi_os:
        # Pavio inferior dominante: vendedores esgotaram, compradores reagiram.
        if lower_wick_ratio < wick_reject_min:
            return None
        # O candle deve fechar positivo (corpo bullish ou doji tendendo a alta).
        if body < 0:
            return None
        # EMA rápida acima da lenta confirma que a reversão tem momentum.
        if ema_fast_now <= ema_slow_now:
            return None
        # Preço ainda abaixo do VWAP mas slope da EMA fast aponta pra cima:
        # vamos exigir que o close esteja acima da EMA rápida (recuperação).
        if c < ema_fast_now:
            return None
        direction = "BUY"

    # --- Sinal SELL: VWAP stretched pra cima + exhaustion bearish ------------
    elif vwap_dev >= vwap_dev_min and rsi >= rsi_ob:
        # Pavio superior dominante: compradores esgotaram, vendedores reagiram.
        if upper_wick_ratio < wick_reject_min:
            return None
        # O candle deve fechar negativo.
        if body > 0:
            return None
        # EMA rápida abaixo da lenta confirma reversão.
        if ema_fast_now >= ema_slow_now:
            return None
        # Close abaixo da EMA rápida (virou pra baixo).
        if c > ema_fast_now:
            return None
        direction = "SELL"

    else:
        return None

    # --- LEI 3: SL sempre via utils["calc_sl"] -------------------------------
    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": STRATEGY_NAME,
            "vwap": vwap,
            "vwap_dev": vwap_dev,
            "vwap_dev_atr_mult": vwap_dev_min,
            "volume": vol,
            "avg_volume": avg_vol,
            "volume_ratio": vol_ratio,
            "rsi": rsi,
            "adx": adx,
            "ema_fast": ema_fast_now,
            "ema_slow": ema_slow_now,
            "upper_wick_ratio": upper_wick_ratio,
            "lower_wick_ratio": lower_wick_ratio,
            "body": body,
            "body_ratio": body_abs / candle_range if candle_range > 0 else 0,
        },
    }
