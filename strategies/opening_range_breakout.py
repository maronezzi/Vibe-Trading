"""
Estratégia OPENING_RANGE_BREAKOUT (ORB) — Wave 5.2 (2026-06-26).

Edge mecânico:
  - Os primeiros 30 minutos do pregão (9:00–9:30 BRT) definem um range
    de "opening range" usado como referência institucional.
  - Breakouts desse range têm continuation acima de 60% em WIN/WDO
    (literatura day-trade B3/CME; ver Harris 1986, "S&P 500 Daytrade").
  - Filtramos por regime (ADX > 15) para evitar breakouts em chop,
    e por volatilidade (ATR mínimo) para evitar dias planos.

Sinal:
  - BUY: preço rompe ACIMA da high do OR com confirmação (fechou acima)
  - SELL: preço rompe ABAIXO da low do OR com confirmação

Defensivo:
  - sl_atr_mult=1.5, cooldown_seconds=300
  - Rejeita se ADX < 15 (sem regime), ATR < min_atr
  - Volume ratio mínimo para confirmar participação

Parâmetros (defaults):
  opening_range_minutes=30, atr_period=14, adx_threshold=15,
  min_volume_ratio=0.5, sl_atr_mult=1.5, cooldown_seconds=300

Notas:
  - O OR é detectado por `bar_ts` (timestamp unix da barra atual).
  - Em timeframe M5, primeiras 6 barras do pregão = 30 min.
  - Para outros TFs (M1/M15), a contagem muda — ajustamos via
    `opening_range_minutes // tf_minutes`.
"""
from datetime import datetime, timezone

STRATEGY_NAME = "OPENING_RANGE_BREAKOUT"

DEFAULT_PARAMS = {
    "opening_range_minutes": 30,
    "atr_period": 14,
    "adx_period": 14,
    "adx_threshold": 15,
    "min_volume_ratio": 0.5,
    "sl_atr_mult": 1.5,
    "cooldown_seconds": 300,
}

# Timeframes em minutos
_TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60}

# Janela de "frozen" do OR (não recalcula depois do range formado)
_FROZEN_AFTER_MINUTES = 240  # após 4h de pregão, OR perde relevância


def _tf_minutes(tf):
    """Retorna a duração em minutos do timeframe."""
    return _TF_MINUTES.get(tf, 5)


def _bar_hour_utc_minus_3(bar_ts):
    """Converte bar_ts (unix) para hora BRT (UTC-3).

    bar_ts pode ser int (unix seconds) ou float. Tratamento defensivo.
    Retorna None se não conseguir converter.
    """
    try:
        ts = float(bar_ts)
    except (TypeError, ValueError):
        return None
    # BRT = UTC-3
    brt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(
        timezone(timedelta(hours=-3))
    )
    return brt


# Local import para evitar overhead global
from datetime import timedelta  # noqa: E402


def _compute_opening_range(bars, tf, opening_range_minutes, current_bar_ts):
    """Calcula a opening range (high, low) dos primeiros N minutos do pregão.

    Critério:
      - Identifica a primeira barra do dia (hour BRT < 9:00 + range_min).
      - Pega todas as barras até `opening_range_minutes` depois das 9:00.
      - Retorna (or_high, or_low, bars_in_range).

    Se a barra atual está DENTRO do range de formação, retorna None
    (range ainda não formado — não podemos operar breakout antes
    do range estar consolidado).
    """
    tf_min = _tf_minutes(tf)
    if tf_min <= 0:
        return None

    # Encontra a primeira barra do dia atual (mesma data BRT)
    today_brt = _bar_hour_utc_minus_3(current_bar_ts)
    if today_brt is None:
        return None

    today_date = today_brt.date()

    # Filtra barras do dia atual
    day_bars = []
    for b in bars:
        bts = b.get("time")
        bdt = _bar_hour_utc_minus_3(bts)
        if bdt is None:
            continue
        if bdt.date() != today_date:
            continue
        day_bars.append(b)

    if not day_bars:
        return None

    # bars são newest-first; ordena cronologicamente (mais antigo primeiro)
    day_bars_chrono = sorted(day_bars, key=lambda b: b["time"])

    # Abertura BRT = 9:00 (mini-contratos B3: WIN/WDO 9:00-17:30 WDO / 17:55 WIN)
    market_open_brt = today_brt.replace(hour=9, minute=0, second=0, microsecond=0)
    market_open_ts = market_open_brt.timestamp()

    # Quantas barras cabem em `opening_range_minutes` para este TF
    bars_in_range = max(1, opening_range_minutes // tf_min)

    # Seleciona as N primeiras barras a partir de 9:00
    or_bars = []
    for b in day_bars_chrono:
        if b["time"] < market_open_ts:
            continue
        or_bars.append(b)
        if len(or_bars) >= bars_in_range:
            break

    if len(or_bars) < bars_in_range:
        return None  # range ainda não formado

    or_high = max(b["high"] for b in or_bars)
    or_low = min(b["low"] for b in or_bars)

    return or_high, or_low, bars_in_range, len(day_bars_chrono)


def _avg_volume(bars, n=20):
    """Média de volume das últimas N barras. Retorna 0 se vazio."""
    if not bars:
        return 0
    sample = bars[:n]
    vols = [b.get("tick_volume", 0) or b.get("volume", 0) for b in sample]
    if not vols:
        return 0
    return sum(vols) / len(vols)


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """Verifica sinal de entrada OPENING_RANGE_BREAKOUT.

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    p = {**DEFAULT_PARAMS, **(params or {})}

    calc_sl = utils["calc_sl"]
    calculate_adx = utils.get("calculate_adx")
    calculate_atr = utils.get("calculate_atr")

    if not bars or len(bars) < 50:
        return None
    if atr is None or atr <= 0:
        return None

    # OR congelado após X minutos — não opera tarde demais
    brt_now = _bar_hour_utc_minus_3(bar_ts)
    if brt_now is None:
        return None

    market_open = brt_now.replace(hour=9, minute=0, second=0, microsecond=0)
    minutes_since_open = (brt_now - market_open).total_seconds() / 60.0

    if minutes_since_open < p["opening_range_minutes"]:
        return None  # range ainda formando — não opera
    if minutes_since_open > _FROZEN_AFTER_MINUTES:
        return None  # range perdeu relevância

    # Calcula OR do dia
    or_result = _compute_opening_range(
        bars, tf, p["opening_range_minutes"], bar_ts
    )
    if or_result is None:
        return None
    or_high, or_low, _bars_in_range, total_day_bars = or_result

    if or_high <= 0 or or_low <= 0 or or_high <= or_low:
        return None

    # Range muito apertado (sem volatilidade) — sem breakout válido
    or_range = or_high - or_low
    if or_range < atr * 0.5:
        return None

    # Filtro ADX (regime)
    adx_val = 0
    if calculate_adx is not None:
        adx_tuple = calculate_adx(bars, p["adx_period"])
        if adx_tuple is None:
            return None
        # calculate_adx pode retornar (adx, plus_di, minus_di) ou só adx
        if isinstance(adx_tuple, tuple):
            adx_val = adx_tuple[0]
        else:
            adx_val = adx_tuple
        if adx_val < p["adx_threshold"]:
            return None

    # Filtro de volatilidade mínima (ATR floor)
    if calculate_atr is not None:
        atr_calc = calculate_atr(bars, p["atr_period"])
        if atr_calc > 0 and atr_calc < atr * 0.5:
            return None

    # Filtro de volume
    vol_ratio = None
    avg_vol = _avg_volume(bars, n=20)
    last_bar = bars[0] if bars else {}
    last_vol = last_bar.get("tick_volume", 0) or last_bar.get("volume", 0) or 0
    if avg_vol > 0 and last_vol > 0:
        vol_ratio = last_vol / avg_vol
        if vol_ratio < p["min_volume_ratio"]:
            return None

    # Breakout detection
    # BUY: preço ACIMA da or_high (com buffer mínimo para evitar wick falso)
    breakout_buffer = atr * 0.1
    direction = None

    if price >= or_high + breakout_buffer:
        direction = "BUY"
    elif price <= or_low - breakout_buffer:
        direction = "SELL"

    if not direction:
        return None

    sl_pts = calc_sl(symbol, atr, p)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "OPENING_RANGE_BREAKOUT",
            "or_high": round(or_high, 4),
            "or_low": round(or_low, 4),
            "or_range_pts": round(or_range, 4),
            "adx": round(adx_val, 1),
            "vol_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
            "minutes_since_open": round(minutes_since_open, 1),
            "atr": atr,
        },
    }
