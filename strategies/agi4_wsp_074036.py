"""
AGI4_WSP_074036 — Estratégia "VWAP+RSI Mean Reversion" para WSP_M15.

Substitui RSI_REVERSION puro por uma mean-reversion híbrida que ancora o TP
na VWAP institucional (preço justo da sessão) e usa RSI apenas como
gatilho direcional de extremo, filtrando deviações de preço >2×ATR em
relação à VWAP. SL em 1.5×ATR (mais apertado que a média das estratégias
WSP porque a entrada é contra-tendência de curtíssimo prazo e exige
gestão de risco agressiva para compensar a baixa taxa de acerto).

Lógica:
  DESVIO = (preço - VWAP) / ATR
  BUY  se DESVIO <= -desv_atr_min  (preço muito abaixo da VWAP)
       E RSI <= rsi_buy_max       (RSI confirma sobrevenda)
  SELL se DESVIO >= +desv_atr_min  (preço muito acima da VWAP)
       E RSI >= rsi_sell_min      (RSI confirma sobrecompra)

  SL: 1.5 × ATR (mínimo), garantindo risco fixo por trade.
  TP: VWAP (convergência ao preço justo — mecanismo clássico de
      mean reversion em futuros líquidos).

Justificativa (fatos):
- Fato #4 (Mean Reversion Strategy 2026): backtests de mean reversion
  outperformam buy-and-hold quando combinados com níveis institucionais.
- Fato #3 (ES/MES/NQ): VWAP é referência universal em futuros — funciona
  como âncora de "preço justo" da sessão.
- Fato #1 (liquidez ES análoga a contratos B3 populares): convergência a
  VWAP é o mecanismo estatístico clássico de mean reversion em futuros
  líquidos — aplicável a WSP por analogia estrutural.

Inspiração: equity errática do WSP em M15 vem de perseguir micro-trends
em vez de apostar em reversão a níveis institucionais. Esta estratégia
ESPERA o preço se afastar >2×ATR da VWAP (exaustão) e só então monta
a reversão. SL curto (1.5×ATR) compensa a taxa de acerto mais baixa
inerente a mean reversion de baixa frequência.
"""

STRATEGY_NAME = "AGI4_WSP_074036"


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    # --- guards mínimos ---
    if not bars or len(bars) < 30 or atr is None or atr <= 0:
        return None
    if price is None or price <= 0:
        return None

    calc_sl = utils["calc_sl"]
    calculate_vwap = utils["calculate_vwap"]
    calculate_rsi = utils["calculate_rsi"]

    # --- parâmetros (todos via params.get c/ default) ---
    vwap_period = int(params.get("vwap_period", 20))
    desv_atr_min = float(params.get("desv_atr_min", 2.0))
    rsi_period = int(params.get("rsi_period", 14))
    rsi_buy_max = float(params.get("rsi_buy_max", 25.0))
    rsi_sell_min = float(params.get("rsi_sell_min", 75.0))
    atr_sl_mult = float(params.get("atr_sl_mult", 1.5))

    # --- 1) VWAP institucional (âncora de TP) ---
    vwap = calculate_vwap(bars, vwap_period)
    if vwap is None or vwap <= 0:
        return None

    # --- 2) Desvio do preço em relação à VWAP (normalizado por ATR) ---
    desv_atr = (price - vwap) / atr
    if abs(desv_atr) < desv_atr_min:
        return None  # preço ainda não se afastou o suficiente da VWAP

    # --- 3) RSI confirma extremo direcional ---
    rsi = calculate_rsi(bars, rsi_period)
    if rsi is None:
        return None

    # --- 4) Decisão ---
    direction = None
    if desv_atr <= -desv_atr_min and rsi <= rsi_buy_max:
        direction = "BUY"   # sobrevenda: preço << VWAP + RSI confirma
    elif desv_atr >= desv_atr_min and rsi >= rsi_sell_min:
        direction = "SELL"  # sobrecompra: preço >> VWAP + RSI confirma

    if not direction:
        return None

    # --- 5) SL: 1.5 × ATR, mínimo garantido por calc_sl do autotrader ---
    sl_atr_pts = int(round(atr * atr_sl_mult))
    sl_pts_base = calc_sl(symbol, atr, params)
    sl_pts = max(sl_atr_pts, sl_pts_base)

    tp_pts = int(round(abs(price - vwap)))  # TP na VWAP (convergência)

    return {
        "direction": direction,
        "sl_pts": int(sl_pts),
        "info": {
            "rationale": "AGI4_WSP vwap_rsi_mean_reversion",
            "vwap": round(vwap, 2),
            "desv_atr": round(desv_atr, 2),
            "rsi": round(rsi, 1),
            "tp_pts": tp_pts,
            "sl_atr_pts": sl_atr_pts,
            "atr": round(atr, 2),
        },
    }
