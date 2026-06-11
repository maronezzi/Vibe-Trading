#!/usr/bin/env python3
"""
Backtest: compara estratégias para WIN e WDO usando dados do MT5.
Roda dentro do Linux (usa mt5_orchestrator via Wine).
"""
import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from mt5_orchestrator import _run_wine, EXECUTOR_WIN, resolve_symbol
import pandas as pd
from datetime import datetime

def fetch_bars(symbol, tf="M5", count=500):
    result = _run_wine(EXECUTOR_WIN, "bars", symbol, tf, str(count))
    if "bars" not in result:
        print(f"  [ERR] Sem dados para {symbol} {tf}: {result}")
        return None
    df = pd.DataFrame(result["bars"])
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df

def calc_atr(df, period=14):
    tr = pd.DataFrame()
    tr["hl"] = df["high"] - df["low"]
    tr["hc"] = abs(df["high"] - df["close"].shift(1))
    tr["lc"] = abs(df["low"] - df["close"].shift(1))
    tr["tr"] = tr[["hl", "hc", "lc"]].max(axis=1)
    return tr["tr"].rolling(period).mean()

def calc_rsi(df, period=14):
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calc_bb(df, period=25, num_std=1.5):
    mid = df["close"].rolling(period).mean()
    std = df["close"].rolling(period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower

def calc_vwap(df, period=20):
    tp = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"].clip(lower=1)
    vwap = (tp * vol).rolling(period).sum() / vol.rolling(period).sum()
    return vwap

def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

# ============================================================
# STRATEGY: VWAP (WDO)
# ============================================================
def backtest_vwap(df, params):
    """VWAP threshold strategy for WDO."""
    atr = calc_atr(df, 14)
    vwap = calc_vwap(df, params.get("vwap_period", 20))
    ema_fast = calc_ema(df["close"], params.get("ema_fast", 9))
    ema_slow = calc_ema(df["close"], params.get("ema_slow", 21))
    rsi = calc_rsi(df, params.get("rsi_period", 14))

    sl_mult = params.get("sl_atr_mult", 1.5)
    buy_thresh = params.get("vwap_buy_threshold", 1.003)
    sell_thresh = params.get("vwap_sell_threshold", 0.997)

    trades = []
    position = None
    entry_price = 0
    sl_price = 0
    best_price = 0

    for i in range(max(30, params.get("vwap_period", 20)), len(df)):
        close = df["close"].iloc[i]
        high = df["high"].iloc[i]
        low = df["low"].iloc[i]
        cur_atr = atr.iloc[i]
        cur_vwap = vwap.iloc[i]
        cur_ema_f = ema_fast.iloc[i]
        cur_ema_s = ema_slow.iloc[i]
        cur_rsi = rsi.iloc[i]

        if pd.isna(cur_atr) or pd.isna(cur_vwap) or cur_atr == 0 or cur_vwap == 0:
            continue

        # Manage position
        if position is not None:
            if position == "BUY":
                best_price = max(best_price, high)
                # Trailing
                if best_price - entry_price >= params.get("trail_activate", 1.5) * cur_atr:
                    trail_sl = best_price - params.get("trail_distance", 0.3) * cur_atr
                    if trail_sl > sl_price:
                        sl_price = trail_sl
                # Exit
                if low <= sl_price:
                    pnl = (sl_price - entry_price) * 0.20  # WIN multiplier
                    trades.append({"entry": entry_price, "exit": sl_price, "pnl": pnl, "reason": "SL"})
                    position = None
                    continue
            elif position == "SELL":
                best_price = min(best_price, low)
                if entry_price - best_price >= params.get("trail_activate", 1.5) * cur_atr:
                    trail_sl = best_price + params.get("trail_distance", 0.3) * cur_atr
                    if trail_sl < sl_price:
                        sl_price = trail_sl
                if high >= sl_price:
                    pnl = (entry_price - sl_price) * 0.20
                    trades.append({"entry": entry_price, "exit": sl_price, "pnl": pnl, "reason": "SL"})
                    position = None
                    continue

        # Entry
        if position is None:
            # Market regime
            spread = abs(cur_ema_f - cur_ema_s) / close if close > 0 else 0
            if spread < params.get("trend_min_spread", 0.001):
                continue  # CHOPPY

            # VWAP signal
            direction = None
            if close > cur_vwap * buy_thresh and cur_ema_f > cur_ema_s:
                direction = "BUY"
            elif close < cur_vwap * sell_thresh and cur_ema_f < cur_ema_s:
                direction = "SELL"

            if direction is None:
                continue

            # RSI filter
            if direction == "BUY" and cur_rsi > params.get("rsi_overbought", 70):
                continue
            if direction == "SELL" and cur_rsi < params.get("rsi_oversold", 30):
                continue

            # Entry
            position = direction
            entry_price = close
            best_price = close
            if direction == "BUY":
                sl_price = entry_price - sl_mult * cur_atr
            else:
                sl_price = entry_price + sl_mult * cur_atr

    # Close remaining
    if position is not None:
        close = df["close"].iloc[-1]
        if position == "BUY":
            pnl = (close - entry_price) * 0.20
        else:
            pnl = (entry_price - close) * 0.20
        trades.append({"entry": entry_price, "exit": close, "pnl": pnl, "reason": "EOD"})

    return trades

# ============================================================
# STRATEGY: BOLLINGER + RSI (WIN)
# ============================================================
def backtest_bollinger(df, params):
    """Bollinger Bands + RSI reversal for WIN."""
    atr = calc_atr(df, 14)
    bb_upper, bb_mid, bb_lower = calc_bb(df, params.get("bb_period", 25), params.get("bb_std", 1.5))
    rsi = calc_rsi(df, params.get("rsi_period", 14))

    sl_mult = params.get("sl_atr_mult", 2.0)
    rsi_buy = params.get("rsi_buy", 30)
    rsi_sell = params.get("rsi_sell", 75)

    trades = []
    position = None
    entry_price = 0
    sl_price = 0
    best_price = 0

    for i in range(max(30, params.get("bb_period", 25)), len(df)):
        close = df["close"].iloc[i]
        high = df["high"].iloc[i]
        low = df["low"].iloc[i]
        cur_atr = atr.iloc[i]
        cur_upper = bb_upper.iloc[i]
        cur_mid = bb_mid.iloc[i]
        cur_lower = bb_lower.iloc[i]
        cur_rsi = rsi.iloc[i]

        if pd.isna(cur_atr) or pd.isna(cur_upper) or cur_atr == 0:
            continue

        # Manage position
        if position is not None:
            if position == "BUY":
                best_price = max(best_price, high)
                # Tighten at BB mid
                if close >= cur_mid and best_price - entry_price > 0:
                    trail_sl = best_price - 0.3 * cur_atr
                    if trail_sl > sl_price:
                        sl_price = trail_sl
                # Trailing
                elif best_price - entry_price >= params.get("trail_activate", 1.5) * cur_atr:
                    trail_sl = best_price - params.get("trail_distance", 0.4) * cur_atr
                    if trail_sl > sl_price:
                        sl_price = trail_sl
                # Exit
                if low <= sl_price:
                    pnl = (sl_price - entry_price) * 0.20
                    trades.append({"entry": entry_price, "exit": sl_price, "pnl": pnl, "reason": "SL"})
                    position = None
                    continue
            elif position == "SELL":
                best_price = min(best_price, low)
                if close <= cur_mid and entry_price - best_price > 0:
                    trail_sl = best_price + 0.3 * cur_atr
                    if trail_sl < sl_price:
                        sl_price = trail_sl
                elif entry_price - best_price >= params.get("trail_activate", 1.5) * cur_atr:
                    trail_sl = best_price + params.get("trail_distance", 0.4) * cur_atr
                    if trail_sl < sl_price:
                        sl_price = trail_sl
                if high >= sl_price:
                    pnl = (entry_price - sl_price) * 0.20
                    trades.append({"entry": entry_price, "exit": sl_price, "pnl": pnl, "reason": "SL"})
                    position = None
                    continue

        # Entry
        if position is None:
            direction = None
            if low <= cur_lower and cur_rsi < rsi_buy:
                direction = "BUY"
            elif high >= cur_upper and cur_rsi > rsi_sell:
                direction = "SELL"

            if direction is None:
                continue

            position = direction
            entry_price = close
            best_price = close
            if direction == "BUY":
                sl_price = entry_price - sl_mult * cur_atr
            else:
                sl_price = entry_price + sl_mult * cur_atr

    # Close remaining
    if position is not None:
        close = df["close"].iloc[-1]
        if position == "BUY":
            pnl = (close - entry_price) * 0.20
        else:
            pnl = (entry_price - close) * 0.20
        trades.append({"entry": entry_price, "exit": close, "pnl": pnl, "reason": "EOD"})

    return trades

def print_results(name, trades):
    if not trades:
        print(f"  {name}: 0 trades")
        return
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in trades)
    wr = len(wins) / len(trades) * 100
    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
    profit_factor = abs(sum(t["pnl"] for t in wins) / sum(t["pnl"] for t in losses)) if losses and sum(t["pnl"] for t in losses) != 0 else float("inf")
    sl_exits = len([t for t in trades if t["reason"] == "SL"])
    max_dd = 0
    running = 0
    for t in trades:
        running += t["pnl"]
        if running < max_dd:
            max_dd = running
    print(f"  {name}:")
    print(f"    Trades: {len(trades)} | Wins: {len(wins)} | WR: {wr:.1f}%")
    print(f"    Total PnL: R$ {total_pnl:+.2f}")
    print(f"    Avg Win: R$ {avg_win:+.2f} | Avg Loss: R$ {avg_loss:+.2f}")
    print(f"    Profit Factor: {profit_factor:.2f}")
    print(f"    SL exits: {sl_exits}/{len(trades)} ({sl_exits/len(trades)*100:.0f}%)")
    print(f"    Max Drawdown: R$ {max_dd:.2f}")

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print(f"BACKTEST — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print("=" * 60)

    # WIN backtest
    print("\n--- WINQ26 M5 (500 bars) ---")
    win_sym = resolve_symbol("WIN")
    if win_sym:
        df_win = fetch_bars(win_sym, "M5", 500)
        if df_win is not None:
            print(f"  Bars: {len(df_win)} | Range: {df_win['time'].iloc[0]} → {df_win['time'].iloc[-1]}")
            print(f"  Price: {df_win['close'].iloc[-1]:.0f} | ATR(14): {calc_atr(df_win, 14).iloc[-1]:.0f}")

            # Current config
            from vt_autotrader import CONFIG
            print("\n  [A] Bollinger+RSI (config atual)")
            trades_a = backtest_bollinger(df_win, CONFIG["win"])
            print_results("Bollinger+RSI", trades_a)

            # Alternative: wider SL
            alt_params = CONFIG["win"].copy()
            alt_params["sl_atr_mult"] = 2.5
            alt_params["bb_std"] = 2.0
            print("\n  [B] Bollinger(2.0std)+RSI — SL 2.5x ATR (mais largo)")
            trades_b = backtest_bollinger(df_win, alt_params)
            print_results("BB(2.0)+RSI 2.5x", trades_b)

            # Alternative: tighter RSI
            alt_params2 = CONFIG["win"].copy()
            alt_params2["rsi_buy"] = 25
            alt_params2["rsi_sell"] = 80
            alt_params2["sl_atr_mult"] = 2.0
            print("\n  [C] Bollinger+RSI(25/80) — SL 2.0x ATR (RSI mais exigente)")
            trades_c = backtest_bollinger(df_win, alt_params2)
            print_results("BB+RSI(25/80) 2.0x", trades_c)

    # WDO backtest
    print("\n--- WDOQ26 M5 (500 bars) ---")
    wdo_sym = resolve_symbol("WDO")
    if wdo_sym:
        df_wdo = fetch_bars(wdo_sym, "M5", 500)
        if df_wdo is not None:
            print(f"  Bars: {len(df_wdo)} | Range: {df_wdo['time'].iloc[0]} → {df_wdo['time'].iloc[-1]}")
            atr_wdo = calc_atr(df_wdo, 14).iloc[-1]
            print(f"  Price: {df_wdo['close'].iloc[-1]:.1f} | ATR(14): {atr_wdo:.3f}")

            from vt_autotrader import CONFIG
            print("\n  [A] VWAP (config atual — SL 1.5x ATR)")
            trades_wdo_a = backtest_vwap(df_wdo, CONFIG["wdo"])
            print_results("VWAP 1.5x", trades_wdo_a)

            alt_wdo = CONFIG["wdo"].copy()
            alt_wdo["sl_atr_mult"] = 2.0
            print("\n  [B] VWAP — SL 2.0x ATR (mais largo)")
            trades_wdo_b = backtest_vwap(df_wdo, alt_wdo)
            print_results("VWAP 2.0x", trades_wdo_b)

            alt_wdo2 = CONFIG["wdo"].copy()
            alt_wdo2["sl_atr_mult"] = 2.5
            alt_wdo2["vwap_buy_threshold"] = 1.002
            alt_wdo2["vwap_sell_threshold"] = 0.998
            print("\n  [C] VWAP — SL 2.5x ATR, threshold mais apertado")
            trades_wdo_c = backtest_vwap(df_wdo, alt_wdo2)
            print_results("VWAP 2.5x tight", trades_wdo_c)

    print("\n" + "=" * 60)
    print("BACKTEST COMPLETO")
