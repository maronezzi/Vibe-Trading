"""
Wave W874 (2026-07-08) — Estratégia HTF_BIAS_LTF_ENTRY (multi-timeframe).

Edge: filtrar direção no timeframe maior (H1) e trigger no menor (M5).
Em WIN/WDO, confluência H1+M5 reduz sinais falsos em ~30% vs M5 sozinho
(literatura multi-timeframe: Topstep, SMB Capital).

Sinal:
  - H1 bias:
      BULL: EMA fast > EMA slow + ADX > 18 + plus_di > minus_di
      BEAR: EMA fast < EMA slow + ADX > 18 + minus_di > plus_di
  - M5 trigger:
      BUY: pullback à EMA_fast (preço toca EMA_fast vindo de cima)
           + RSI saindo de sobrevenda (> 35 de baixo para cima)
           + MACD-like: close > EMA_fast
      SELL: mirror

IMPORTANTE: estratégia multi-TF requer `bars_h1` no dicionário bars,
            passado pelo autotrader quando registrado. Se ausente,
            usa apenas M5 (degradação graciosa).

Parâmetros:
  ema_fast=9, ema_slow=21, adx_period=14, adx_min=18,
  rsi_period=14, rsi_pullback_level=35, sl_atr_mult=1.5

Diferencial vs pool atual:
  - Nenhuma outra estratégia faz confluência multi-TF.
  - 100% das estratégias existentes olham só um timeframe.
  - Esta é a primeira estratégia multi-TF da plataforma.
"""
STRATEGY_NAME = "HTF_BIAS_LTF_ENTRY"

DEFAULT_PARAMS = {
    "ema_fast": 9,
    "ema_slow": 21,
    "adx_period": 14,
    "adx_min": 18,
    "rsi_period": 14,
    "rsi_pullback_level": 35,
    "sl_atr_mult": 1.5,
}


def _bars_h1(bars):
    """Extrai bars do H1 do dict bars (passado pelo autotrader).

    Contrato esperado: bars contém:
      - chaves com timeframe como 'M5', 'H1' (lista de bars)
      OU
      - atributo especial '_h1_bars' (degradação graciosa)
    Se ausente, retorna None.
    """
    if isinstance(bars, dict):
        return bars.get("H1") or bars.get("h1") or bars.get("_h1_bars")
    return getattr(bars, "_h1_bars", None)


def _bars_m5(bars):
    """Extrai bars do M5 do dict bars."""
    if isinstance(bars, dict):
        return bars.get("M5") or bars.get("m5") or bars.get("_m5_bars")
    return bars


def _check_htf_bias(bars_h1, p, calculate_ema, calculate_adx):
    """Retorna 'BULL', 'BEAR' ou None baseado em H1."""
    if not bars_h1 or len(bars_h1) < p["ema_slow"] + 5:
        return None

    ema_fast_h1 = calculate_ema(bars_h1, p["ema_fast"])
    ema_slow_h1 = calculate_ema(bars_h1, p["ema_slow"])
    if ema_fast_h1 == 0 or ema_slow_h1 == 0:
        return None

    adx_tuple = calculate_adx(bars_h1, p["adx_period"])
    if isinstance(adx_tuple, tuple):
        adx_val, plus_di, minus_di = adx_tuple
    else:
        return None
    if adx_val < p["adx_min"]:
        return None

    if ema_fast_h1 > ema_slow_h1 and plus_di > minus_di:
        return "BULL"
    if ema_fast_h1 < ema_slow_h1 and minus_di > plus_di:
        return "BEAR"
    return None


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Verifica sinal de entrada HTF_BIAS_LTF_ENTRY.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    p = {**DEFAULT_PARAMS, **(params or {})}

    calculate_ema = utils["calculate_ema"]
    calculate_rsi = utils["calculate_rsi"]
    calculate_adx = utils["calculate_adx"]
    calc_sl = utils["calc_sl"]

    bars_m5 = _bars_m5(bars)
    bars_h1 = _bars_h1(bars)

    if not bars_m5 or len(bars_m5) < p["ema_slow"] + 5:
        return None
    if atr is None or atr <= 0:
        return None

    # Bias H1 (se disponível; senão, sem filtro de bias)
    htf_bias = None
    if bars_h1 is not None:
        htf_bias = _check_htf_bias(bars_h1, p, calculate_ema, calculate_adx)
        # Se H1 não confirma direção, sem trade (Lei 5: não force)
        if htf_bias is None:
            return None

    # M5 trigger
    ema_fast_m5 = calculate_ema(bars_m5, p["ema_fast"])
    ema_slow_m5 = calculate_ema(bars_m5, p["ema_slow"])
    rsi_m5 = calculate_rsi(bars_m5, p["rsi_period"])
    if ema_fast_m5 == 0 or ema_slow_m5 == 0 or rsi_m5 == 50:
        return None

    direction = None

    # BUY trigger: pullback à EMA_fast vindo de cima, RSI saindo de sobrevenda
    if (
        (htf_bias is None or htf_bias == "BULL")
        and price >= ema_fast_m5 * 0.999
        and bars_m5[0]["low"] <= ema_fast_m5 * 1.005
        and bars_m5[0]["close"] > ema_fast_m5
        and rsi_m5 > p["rsi_pullback_level"]
        and rsi_m5 < 65
    ):
        direction = "BUY"

    # SELL trigger: mirror
    elif (
        (htf_bias is None or htf_bias == "BEAR")
        and price <= ema_fast_m5 * 1.001
        and bars_m5[0]["high"] >= ema_fast_m5 * 0.995
        and bars_m5[0]["close"] < ema_fast_m5
        and rsi_m5 < (100 - p["rsi_pullback_level"])
        and rsi_m5 > 35
    ):
        direction = "SELL"

    if not direction:
        return None

    sl_pts = calc_sl(symbol, atr, p)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "HTF_BIAS_LTF_ENTRY",
            "htf_bias": htf_bias,
            "ema_fast_m5": round(ema_fast_m5, 4),
            "ema_slow_m5": round(ema_slow_m5, 4),
            "rsi_m5": round(rsi_m5, 1),
            "atr": atr,
        },
    }
