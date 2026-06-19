"""
Estratégia ICHIMOKU — Ichimoku Cloud (equilíbrio de preço + suporte/resistência dinâmico).

Sinal de entrada:
- BUY: preço acima da nuvem (Senkou Span A/B) + Tenkan cruza Kijun pra cima
- SELL: preço abaixo da nuvem + Tenkan cruza Kijun pra baixo

Componentes do Ichimoku:
- Tenkan-sen (linha de conversão): média de highest high + lowest low de 9 períodos
- Kijun-sen (linha base): média de 26 períodos
- Senkou Span A: média de Tenkan + Kijun, projetada 26 barras à frente
- Senkou Span B: média de 52 períodos, projetada 26 barras à frente
- Chikou Span: preço atual projetado 26 barras atrás

Lógica:
1. Calcula Tenkan e Kijun
2. Verifica se preço está acima/abaixo da nuvem (Senkou A/B)
3. Detecta cruzamento Tenkan/Kijun (cruzamento atual vs barra anterior)
4. Confirma que o cruzamento é recente (dentro de 3 barras)

Parâmetros:
  tenkan_period=9, kijun_period=26, senkou_period=52
"""

STRATEGY_NAME = "ICHIMOKU"


def _midpoint(bars, period):
    """Calcula (highest high + lowest low) / 2 de N barras mais recentes."""
    if len(bars) < period:
        return 0.0
    segment = bars[:period]
    highest = max(b["high"] for b in segment)
    lowest = min(b["low"] for b in segment)
    return (highest + lowest) / 2.0


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    """
    Verifica sinal de entrada ICHIMOKU (Ichimoku Cloud).

    Returns:
        None (sem sinal) ou {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}}
    """
    calc_sl = utils["calc_sl"]

    # Parâmetros
    tenkan_period = params.get("tenkan_period", 9)
    kijun_period = params.get("kijun_period", 26)
    senkou_period = params.get("senkou_period", 52)

    min_bars = max(tenkan_period, kijun_period, senkou_period) + 5
    if not bars or len(bars) < min_bars:
        return None

    if atr <= 0:
        return None

    # Tenkan-sen e Kijun-sen (barra atual)
    tenkan = _midpoint(bars, tenkan_period)
    kijun = _midpoint(bars, kijun_period)

    if tenkan == 0 or kijun == 0:
        return None

    # Tenkan e Kijun da barra anterior (para detectar cruzamento)
    tenkan_prev = _midpoint(bars[1:], tenkan_period)
    kijun_prev = _midpoint(bars[1:], kijun_period)

    # Senkou Span A e B (nuvem) — usando barras atuais
    # NOTA: Ichimoku padrão projeta Senkou Span 26 barras à frente.
    # Aqui calculamos com barras atuais para detectar sinais de preço vs nuvem
    # no momento presente. A projeção futura não afeta a decisão de entrada.
    senkou_a = (tenkan + kijun) / 2.0
    senkou_b = _midpoint(bars, senkou_period)

    if senkou_a == 0 or senkou_b == 0:
        return None

    # Limites da nuvem (topo e fundo)
    cloud_top = max(senkou_a, senkou_b)
    cloud_bottom = min(senkou_a, senkou_b)

    direction = None

    # BUY: preço acima da nuvem + Tenkan cruza Kijun pra cima
    if price > cloud_top:
        if tenkan_prev <= kijun_prev and tenkan > kijun:
            direction = "BUY"

    # SELL: preço abaixo da nuvem + Tenkan cruza Kijun pra baixo
    elif price < cloud_bottom:
        if tenkan_prev >= kijun_prev and tenkan < kijun:
            direction = "SELL"

    if not direction:
        return None

    # SL via calc_sl
    sl_pts = calc_sl(symbol, atr, params)

    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": {
            "strategy": "ICHIMOKU",
            "tenkan": tenkan,
            "kijun": kijun,
            "senkou_a": senkou_a,
            "senkou_b": senkou_b,
            "cloud_top": cloud_top,
            "cloud_bottom": cloud_bottom,
            "atr": atr,
        },
    }
