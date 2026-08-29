"""
AGI4_WSP_073213 — Estratégia "Adaptive Kalman Regime Shift" para WSP_H1.

Abordagem genuinamente diferente das 28 testadas sem sucesso em WSP:
- Nenhuma das 28 opera sobre um ESTADO INTERNO estimado adaptativamente.
  Esta estratégia combina:
    1. Estimador de estado tipo Kalman (proxy via filtro EMA duplo
       adaptativo, ponderado por variância local de retorno).
    2. Resíduo (surpresa) entre preço e estado: mede desvio do "consenso".
    3. Variância do resíduo em janela curta: quando está comprimida, há
       energia acumulada — entramos no PRIMEIRO rompimento direcional.
    4. Filtro de persistência (autocorrelação serial do retorno) — só
       opera se o regime atual for persistente, evitando mercados random
       walk (onde reversão/continuação tem edge ≈ 0).
    5. Confirmação direcional via EMA stack (trend) + RSI não-extremo
       (espaço para correr).

Lógica:
  ESTADO[n] = (1 - K[n]) * ESTADO[n-1] + K[n] * PREÇO[n]
  K[n] = variância_curta / (variância_curta + variância_longa)
  RES[n] = PREÇO[n] - ESTADO[n]
  ROMPIMENTO quando |RES[n]| > k_atr * ATR E volat_residuo < limiar (compressão)

Só dispara quando:
  - autocorrelação(retornos, lag=1) > limiar (mercado com memória = persistente)
  - volatilidade do resíduo está comprimida (energia sendo carregada)
  - direção do rompimento alinhada com EMA stack + RSI em zona de continuação

Inspiração: equity curve do WSP é errática — em H1 a maior parte dos
sinais morre porque o ativo alterna micro-trends com chop. Esta
estratégia FILTRA o chop medindo memória serial e só age quando há
"setup" de expansão (resíduo comprimido) seguido de rompimento real
(resíduo > limiar).
"""

STRATEGY_NAME = "AGI4_WSP_073213"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    # --- guards mínimos ---
    if not bars or len(bars) < 60 or atr is None or atr <= 0:
        return None
    if price is None or price <= 0:
        return None

    calc_sl = utils["calc_sl"]
    calculate_rsi = utils["calculate_rsi"]
    calculate_ema = utils["calculate_ema"]
    utils["calculate_bollinger"]
    calculate_adx = utils["calculate_adx"]

    # --- parâmetros (todos via params.get c/ default) ---
    state_fast_w = int(params.get("state_fast_w", 7))
    state_slow_w = int(params.get("state_slow_w", 28))
    int(params.get("resid_var_win", 12))
    vol_compress_win = int(params.get("vol_compress_win", 18))
    vol_compress_max_atr = float(params.get("vol_compress_max_atr", 0.55))
    breakout_resid_atr = float(params.get("breakout_resid_atr", 1.65))
    autocorr_lag = int(params.get("autocorr_lag", 1))
    autocorr_min = float(params.get("autocorr_min", 0.18))
    ema_fast = int(params.get("ema_fast", 8))
    ema_slow = int(params.get("ema_slow", 34))
    ema_trend = int(params.get("ema_trend", 89))
    rsi_period = int(params.get("rsi_period", 14))
    rsi_long_min = float(params.get("rsi_long_min", 48.0))
    rsi_long_max = float(params.get("rsi_long_max", 70.0))
    rsi_short_min = float(params.get("rsi_short_min", 30.0))
    rsi_short_max = float(params.get("rsi_short_max", 52.0))
    adx_period = int(params.get("adx_period", 14))
    adx_min = float(params.get("adx_min", 14.0))
    adx_max = float(params.get("adx_max", 38.0))
    atr_sl_mult = float(params.get("atr_sl_mult", 2.4))
    sl_pts_bonus = int(params.get("sl_pts_bonus", 60))

    # --- 1) Estimador de estado adaptativo (Kalman proxy) ---
    # Janela mínima: state_slow_w + vol_compress_win + autocorr_lag + slack
    if len(bars) < state_slow_w + vol_compress_win + 5:
        return None

    closes = [float(b.get("close", 0) or 0) for b in bars]
    if not closes or closes[-1] <= 0:
        return None

    # Variância de retornos em janela curta e longa (ganho K adaptativo)
    rets = []
    for i in range(1, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        if prev > 0:
            rets.append((cur - prev) / prev)
    if len(rets) < state_slow_w + 5:
        return None

    def _variance(arr):
        if len(arr) < 2:
            return 0.0
        m = sum(arr) / len(arr)
        return sum((x - m) ** 2 for x in arr) / (len(arr) - 1)

    var_fast = _variance(rets[-state_fast_w:])
    var_slow = _variance(rets[-state_slow_w:])
    # Ganho tipo Kalman: 0..1 — quanto maior var_fast relativa a var_slow,
    # mais "ruidoso" o regime e mais pesamos o estado anterior (inercia);
    # quanto menor var_fast relativa, mais "calmo" e seguimos o preço.
    denom = var_fast + var_slow
    if denom <= 0:
        return None
    K = var_fast / denom
    # Clamp para evitar patologias numéricas
    K = max(0.05, min(0.95, K))

    # Calcula estado estimado via filtro IIR: state[n] = K*price[n] + (1-K)*state[n-1]
    state_vals = []
    s = closes[0]
    for c in closes:
        s = K * c + (1.0 - K) * s
        state_vals.append(s)
    last_state = state_vals[-1]
    last_resid = closes[-1] - last_state

    # --- 2) Volatilidade do resíduo (compressão de energia) ---
    # Reestima resíduo nas últimas N barras usando a mesma recursão
    # truncada para evitar dependência do passado distante
    resid_window_vals = []
    s2 = closes[-vol_compress_win]
    for c in closes[-vol_compress_win:]:
        s2 = K * c + (1.0 - K) * s2
        resid_window_vals.append(c - s2)
    resid_var = _variance(resid_window_vals)
    resid_std = resid_var ** 0.5
    # Normaliza pela ATR (resíduo relativo)
    resid_std_atr = resid_std / atr if atr > 0 else 1.0

    if resid_std_atr >= vol_compress_max_atr:
        return None  # resíduo ainda está "barulhento" — sem energia carregada

    # --- 3) Autocorrelação serial do retorno (filtro de persistência) ---
    if len(rets) < autocorr_lag + 6:
        return None
    r_use = rets[-(autocorr_lag + 20):]
    m_r = sum(r_use) / len(r_use)
    cov = 0.0
    var0 = 0.0
    for i in range(len(r_use) - autocorr_lag):
        a = r_use[i] - m_r
        b = r_use[i + autocorr_lag] - m_r
        cov += a * b
    for i in range(len(r_use) - autocorr_lag):
        var0 += (r_use[i] - m_r) ** 2
    if var0 <= 0:
        return None
    autocorr = cov / var0

    if autocorr < autocorr_min:
        return None  # mercado tipo random walk — sem edge direcional

    # --- 4) EMAs + RSI + ADX (confirmação direcional) ---
    ema_f = calculate_ema(bars, ema_fast)
    ema_s = calculate_ema(bars, ema_slow)
    ema_t = calculate_ema(bars, ema_trend)
    if ema_f is None or ema_s is None or ema_t is None:
        return None
    if len(ema_f) < 1 or len(ema_s) < 1 or len(ema_t) < 1:
        return None

    rsi = calculate_rsi(bars, rsi_period)
    if rsi is None:
        return None

    adx_pack = calculate_adx(bars, adx_period)
    if adx_pack is None or adx_pack[0] is None:
        return None
    adx_now = float(adx_pack[0])
    if adx_now < adx_min or adx_now > adx_max:
        return None  # regime morto ou já esticado — sem "fuel"

    # --- 5) Decisão LONG ---
    # Rompimento: resíduo positivo forte + compressão + persistência
    # + EMA stack bullish + RSI em zona de continuação
    long_stack = (ema_f[-1] > ema_s[-1] > ema_t[-1])
    long_rsi = rsi_long_min <= rsi <= rsi_long_max
    long_break = last_resid >= breakout_resid_atr * atr
    if long_stack and long_rsi and long_break:
        sl_atr_pts = int(round(atr * atr_sl_mult))
        sl_pts = max(sl_atr_pts, calc_sl(symbol, atr, params) + sl_pts_bonus)
        return {
            "direction": "BUY",
            "sl_pts": int(sl_pts),
            "info": {
                "rationale": "AGI4_WSP kalman_resid_breakout_long",
                "kalman_gain_K": round(K, 3),
                "resid": round(last_resid, 2),
                "resid_atr": round(last_resid / atr, 2),
                "resid_vol_atr": round(resid_std_atr, 3),
                "autocorr": round(autocorr, 3),
                "adx": round(adx_now, 1),
                "rsi": round(rsi, 1),
                "ema_stack": "8>34>89",
                "atr": round(atr, 2),
            },
        }

    # --- 6) Decisão SHORT (simétrico) ---
    short_stack = (ema_f[-1] < ema_s[-1] < ema_t[-1])
    short_rsi = rsi_short_min <= rsi <= rsi_short_max
    short_break = last_resid <= -breakout_resid_atr * atr
    if short_stack and short_rsi and short_break:
        sl_atr_pts = int(round(atr * atr_sl_mult))
        sl_pts = max(sl_atr_pts, calc_sl(symbol, atr, params) + sl_pts_bonus)
        return {
            "direction": "SELL",
            "sl_pts": int(sl_pts),
            "info": {
                "rationale": "AGI4_WSP kalman_resid_breakout_short",
                "kalman_gain_K": round(K, 3),
                "resid": round(last_resid, 2),
                "resid_atr": round(last_resid / atr, 2),
                "resid_vol_atr": round(resid_std_atr, 3),
                "autocorr": round(autocorr, 3),
                "adx": round(adx_now, 1),
                "rsi": round(rsi, 1),
                "ema_stack": "8<34<89",
                "atr": round(atr, 2),
            },
        }

    return None
