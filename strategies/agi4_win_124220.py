"""
AGI4_WIN_124220: Momentum/Trend strategy for WIN_H1.
Adapted from AGI4_BIT_171647.
Trigger: EMA cross (9/21) or Donchian breakout (20).
Trend Filter: Price > EMA50 and ADX > 20.
Regime Gate: ATR(14) normalized > 0.5% of price.
Direction: Long only on bullish signals, Short only on bearish signals.
"""

STRATEGY_NAME = "AGI4_WIN_124220"

TUNABLE_PARAMS = {
    "ema_fast": (int, 5, 15),
    "ema_slow": (int, 15, 30),
    "donchian_period": (int, 10, 30),
    "adx_min": (float, 15.0, 30.0),
    "atr_pct_min": (float, 0.3, 1.5)
}


def check_entry(symbol, tf, price, atr, bar_ts, bars, params, utils):
    calculate_ema = utils["calculate_ema"]
    calculate_adx = utils["calculate_adx"]
    calculate_donchian = utils["calculate_donchian"]
    calculate_atr = utils["calculate_atr"]
    calc_sl = utils["calc_sl"]

    # Parameters with defensive defaults
    ema_fast_p = params.get("ema_fast", 9)
    ema_slow_p = params.get("ema_slow", 21)
    donchian_p = params.get("donchian_period", 20)
    adx_min = params.get("adx_min", 20.0)
    atr_pct_min = params.get("atr_pct_min", 0.5)
    
    # Fixed parameters for trend filter
    ema_trend_p = 50
    adx_period = 14
    atr_period = 14

    # Warmup check
    min_bars = max(ema_slow_p, donchian_p, ema_trend_p, adx_period * 2, atr_period) + 5
    if not bars or len(bars) < min_bars:
        return None
    
    if atr <= 0 or price <= 0:
        return None

    # Calculate indicators
    ema_fast = calculate_ema(bars, ema_fast_p)
    ema_slow = calculate_ema(bars, ema_slow_p)
    ema_trend = calculate_ema(bars, ema_trend_p)
    
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    
    # Donchian channels exclude current bar, so we compare current price to previous highs/lows
    don_upper, don_mid, don_lower = calculate_donchian(bars, donchian_p)
    
    # Regime Gate: Volatility check
    # ATR normalized by price must be above threshold
    atr_norm = (atr / price) * 100.0
    if atr_norm < atr_pct_min:
        return None

    # Trend Filter: ADX strength and Price vs EMA50
    if adx_val < adx_min:
        return None
        
    # Check for valid values
    if ema_fast == 0 or ema_slow == 0 or ema_trend == 0 or don_upper == 0 or don_lower == 0:
        return None

    direction = None
    info = {}

    # --- LONG LOGIC ---
    # Condition 1: Bullish Trend (Price > EMA50)
    is_bullish_trend = price > ema_trend
    
    # Condition 2: Trigger (EMA Cross OR Donchian Breakout)
    # EMA Cross: Fast > Slow
    ema_cross_up = ema_fast > ema_slow
    # Donchian Breakout: Price > Previous High
    don_breakout_up = price > don_upper
    
    trigger_long = is_bullish_trend and (ema_cross_up or don_breakout_up)

    if trigger_long:
        direction = "BUY"
        info = {
            "trigger": "EMA_Cross" if ema_cross_up else "Donchian_Breakout",
            "adx": adx_val,
            "atr_pct": atr_norm,
            "price_vs_ema50": price - ema_trend
        }

    # --- SHORT LOGIC ---
    # Condition 1: Bearish Trend (Price < EMA50)
    is_bearish_trend = price < ema_trend
    
    # Condition 2: Trigger (EMA Cross OR Donchian Breakout)
    # EMA Cross: Fast < Slow
    ema_cross_down = ema_fast < ema_slow
    # Donchian Breakout: Price < Previous Low
    don_breakout_down = price < don_lower
    
    trigger_short = is_bearish_trend and (ema_cross_down or don_breakout_down)

    if trigger_short:
        direction = "SELL"
        info = {
            "trigger": "EMA_Cross" if ema_cross_down else "Donchian_Breakout",
            "adx": adx_val,
            "atr_pct": atr_norm,
            "price_vs_ema50": price - ema_trend
        }

    if direction is None:
        return None

    sl_pts = calc_sl(symbol, atr, params)
    
    return {
        "direction": direction,
        "sl_pts": sl_pts,
        "info": info
    }