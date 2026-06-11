#!/usr/bin/env python3
"""
Vibe-Trading Autotrader — Daemon autônomo com estratégias SPLIT.

WDO → VWAP(20): buy > 1.003, sell < 0.997 (mercado trending)
WIN → Bollinger(15,2) + RSI(14,35/65): reversão à média (mercado choppy)

Funcionalidades:
- Estratégia por símbolo (split)
- SL obrigatório (1.5x ATR)
- Trailing stop (ativa 1.5x ATR, distância 0.5x ATR)
- Fecha tudo às 16:45
- Log completo no SQLite
- Notificações Telegram

Uso:
    python vt_autotrader.py              # Roda durante horário de mercado
    python vt_autotrader.py --once      # Uma única verificação
    python vt_autotrader.py --close     # Fecha tudo e encerra
    python vt_autotrader.py --status    # Status atual
"""

import sys
import os
import time
import json
import subprocess
import signal
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from vt_trade_log import init_db, log_entry, log_exit, import_mt5_history, get_daily_summary
from mt5_orchestrator import status, buy, sell, close, close_all, tick, resolve_symbol, _run_wine, EXECUTOR_WIN
from vt_config_loader import load_config
from vt_strategy_loader import load_strategies, get_strategy_func, reload_strategies

# ===== CONFIGURAÇÃO =====
# Config carregada do vt_config.json com hot reload
# Para alterar parâmetros: edite vt_config.json ou use save_params/save_full_config
CONFIG = load_config()

# Funções utilitárias passadas para as estratégias plugins
_strategy_utils = {}


def _init_strategy_utils():
    """Inicializa o dict de utils para as estratégias (chamado no startup)."""
    global _strategy_utils
    _strategy_utils = {
        "calculate_vwap": calculate_vwap,
        "calculate_ema": calculate_ema,
        "calculate_rsi": calculate_rsi,
        "calculate_adx": calculate_adx,
        "calculate_bollinger": calculate_bollinger,
        "calculate_atr": calculate_atr,
        "get_market_regime": get_market_regime,
        "calc_sl": _calc_sl,
    }


class SessionState:
    def __init__(self):
        self.positions = {}
        self.last_signals = {}
        self.daily_pnl = 0
        self.trade_count = 0
        self.wins = 0
        self.losses = 0
        self.started_at = None
        self.closed = False
        self.notified_close = False
        self.last_trade_time = {}
        self.daily_trade_count = 0
        self.current_day = None
        self.daily_trade_by_symbol = {}  # {symbol: count}
        self.consecutive_losses = {}      # per-symbol tracking: {symbol: count}
        self.max_consecutive_losses = 3   # halt after N consecutive losses per symbol
        self.resolved_symbols = {}        # cache: {"WDO": "WDON26", "WIN": "WINM26"}
        self.resolved_day = ""            # dia do cache (reseta a cada dia)

    def to_dict(self):
        return {
            "positions": self.positions,
            "last_signals": {k: {**v, "ts": v["ts"].isoformat() if v.get("ts") else None}
                             for k, v in self.last_signals.items()},
            "daily_pnl": self.daily_pnl,
            "trade_count": self.trade_count,
            "wins": self.wins,
            "losses": self.losses,
            "started_at": str(self.started_at) if self.started_at else None,
            "closed": self.closed,
        }


state = SessionState()
log_file = Path("/tmp/vt_autotrader.log")


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)


TELEGRAM_TARGET = "telegram:-1004284773048"


def notify_telegram(msg: str):
    try:
        subprocess.run(
            ["hermes", "send", "-t", TELEGRAM_TARGET, msg],
            capture_output=True, timeout=30,
            env={**os.environ, "WINEDEBUG": "-all"}
        )
    except Exception as e:
        log(f"[NOTIFY FAIL] {e}")


def fetch_bars(symbol: str, tf: str = "M5", count: int = 30) -> list:
    result = _run_wine(EXECUTOR_WIN, "bars", symbol, tf, str(count))
    if "bars" in result:
        return result["bars"]
    return []


def calculate_vwap(bars: list, period: int = 20) -> float:
    if not bars or len(bars) < period:
        return 0
    data = bars[:period]
    sum_pv = 0
    sum_v = 0
    for b in data:
        typical = (b["high"] + b["low"] + b["close"]) / 3
        vol = max(b["volume"], 1)
        sum_pv += typical * vol
        sum_v += vol
    return sum_pv / sum_v if sum_v > 0 else 0


def calculate_atr(bars: list, period: int = 14) -> float:
    if not bars or len(bars) < period + 1:
        return 0
    data = bars[:period + 1]
    tr_sum = 0
    for i in range(period):
        h = data[i]["high"]
        l = data[i]["low"]
        c_prev = data[i + 1]["close"]
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        tr_sum += tr
    return tr_sum / period


def calculate_ema(bars: list, period: int) -> float:
    if not bars or len(bars) < period:
        return 0
    # bars are newest-first; reverse to process chronologically
    chronological = list(reversed(bars))
    seed = sum(b["close"] for b in chronological[:period]) / period
    ema = seed
    multiplier = 2 / (period + 1)
    for b in chronological[period:]:
        ema = b["close"] * multiplier + ema * (1 - multiplier)
    return ema


def calculate_rsi(bars: list, period: int = 14) -> float:
    if not bars or len(bars) < period + 1:
        return 50
    gains = []
    losses = []
    for i in range(min(period + 1, len(bars) - 1)):
        diff = bars[i]["close"] - bars[i + 1]["close"]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0.001
    rs = avg_gain / avg_loss if avg_loss > 0 else 100
    return 100 - (100 / (1 + rs))


def calculate_bollinger(bars: list, period: int = 20, num_std: float = 2.0):
    """Retorna (upper, middle, lower) das Bollinger Bands."""
    if not bars or len(bars) < period:
        return 0, 0, 0
    closes = [b["close"] for b in bars[:period]]
    mid = sum(closes) / period
    variance = sum((c - mid) ** 2 for c in closes) / period
    std = variance ** 0.5
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


def calculate_adx(bars: list, period: int = 14):
    """Average Directional Index — mede força da tendência."""
    if not bars or len(bars) < period * 2:
        return 0, 0, 0
    highs = [b["high"] for b in bars[:period * 2]]
    lows = [b["low"] for b in bars[:period * 2]]
    closes = [b["close"] for b in bars[:period * 2]]
    plus_dm = []
    minus_dm = []
    for i in range(1, len(highs)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)
    tr_list = []
    for i in range(1, len(highs)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        tr_list.append(tr)
    if len(tr_list) < period:
        return 0, 0, 0
    atr_val = sum(tr_list[:period]) / period
    plus_dm_smooth = sum(plus_dm[:period]) / period
    minus_dm_smooth = sum(minus_dm[:period]) / period
    for i in range(period, len(tr_list)):
        atr_val = (atr_val * (period - 1) + tr_list[i]) / period
        plus_dm_smooth = (plus_dm_smooth * (period - 1) + plus_dm[i]) / period
        minus_dm_smooth = (minus_dm_smooth * (period - 1) + minus_dm[i]) / period
    if atr_val == 0:
        return 0, 0, 0
    plus_di = 100 * plus_dm_smooth / atr_val
    minus_di = 100 * minus_dm_smooth / atr_val
    di_sum = plus_di + minus_di
    dx = 100 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0
    return dx, plus_di, minus_di


def get_market_regime(bars: list, params: dict = None) -> str:
    if params is None:
        params = CONFIG["wdo"]
    ema_slow_val = params.get("ema_slow", 21)
    if not bars or len(bars) < ema_slow_val + 5:
        return "CHOPPY"
    ema_f = calculate_ema(bars, params.get("ema_fast", 9))
    ema_s = calculate_ema(bars, ema_slow_val)
    current_price = bars[0]["close"]
    if ema_f == 0 or ema_s == 0 or current_price == 0:
        return "CHOPPY"
    spread = abs(ema_f - ema_s) / current_price
    if spread < params.get("trend_min_spread", 0.001):
        return "CHOPPY"
    elif ema_f > ema_s:
        return "TREND_UP"
    else:
        return "TREND_DOWN"


def is_trading_time() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    h, m = now.hour, now.minute
    start = CONFIG["start_hour"] * 60 + CONFIG["start_minute"]
    end = CONFIG["close_hour"] * 60 + CONFIG["close_minute"]
    current = h * 60 + m
    return start <= current < end


def is_close_time() -> bool:
    now = datetime.now()
    return (now.hour == CONFIG["close_hour"] and now.minute >= CONFIG["close_minute"])


def _get_strategy(symbol_root: str) -> str:
    """Retorna a estratégia para o símbolo: VWAP ou BOLLINGER."""
    return CONFIG["strategy"].get(symbol_root, "VWAP")


def _get_params(symbol_root: str) -> dict:
    """Retorna os parâmetros otimizados para o símbolo."""
    return CONFIG.get(symbol_root.lower(), {})


def _reset_daily_counter():
    """Reseta contador diário se mudou o dia."""
    today = datetime.now().date()
    if state.current_day != today:
        state.current_day = today
        state.daily_trade_count = 0
        state.daily_trade_by_symbol = {}
        state.last_trade_time = {}
        state.consecutive_losses = {}
        log(f"[DAILY] Contador diário resetado para {today}")


def _is_safe_time_window() -> bool:
    """Evita operar nos primeiros/últimos 15 min da sessão.
    - Abertura 9:05-9:20: mercado ainda definindo direção
    - Fechamento 16:30-16:45: risco de gap/ilha
    Retorna True se está em janela segura.
    """
    now = datetime.now()
    h, m = now.hour, now.minute
    current = h * 60 + m
    # Primeiros 15 min após abertura (9:05-9:20)
    if 9 * 60 + 5 <= current <= 9 * 60 + 20:
        return False
    # Últimos 15 min antes do fechamento (16:30-16:45)
    if 16 * 60 + 30 <= current <= 16 * 60 + 45:
        return False
    return True


def _check_cooldown(symbol: str, params: dict) -> bool:
    """Retorna True se pode operar (cooldown ok)."""
    now = datetime.now()
    last_time = state.last_trade_time.get(symbol)
    if last_time:
        elapsed = (now - last_time).total_seconds()
        if elapsed < params.get("cooldown_seconds", CONFIG["wdo"]["cooldown_seconds"]):
            return False
    return True


def _check_max_trades(params: dict, symbol: str = "") -> bool:
    """Retorna True se pode operar (limite não atingido). Conta por símbolo."""
    # Limite global (segurança)
    if state.daily_trade_count >= 30:
        return False
    # Limite por símbolo
    sym_count = state.daily_trade_by_symbol.get(symbol, 0)
    max_per_sym = params.get("max_daily_trades", 15)
    if sym_count >= max_per_sym:
        return False
    return True


def _check_consecutive_losses(symbol: str) -> bool:
    """Retorna True se pode operar (sem sequência de derrotas)."""
    # Se 3+ perdas consecutivas no símbolo, pausar
    sym_losses = state.consecutive_losses.get(symbol, 0)
    if sym_losses >= state.max_consecutive_losses:
        log(f"[BLOQUEADO] {symbol} — {sym_losses} perdas consecutivas")
        return False
    return True


def check_and_trade():
    from vt_analyst import fetch_snapshot, save_snapshot, detect_anomalies, log_anomaly, notify as analyst_notify

    # Safety: avoid first/last 15 min of session
    if not _is_safe_time_window():
        return

    for symbol_root in CONFIG["symbols"]:
        # Cache resolved symbol por dia (evita flip entre WDON26/WDOQ26)
        today_str = datetime.now().strftime("%Y-%m-%d")
        if state.resolved_day != today_str or symbol_root not in state.resolved_symbols:
            symbol = resolve_symbol(symbol_root)
            if symbol:
                state.resolved_symbols[symbol_root] = symbol
                state.resolved_day = today_str
                log(f"[RESOLVE] {symbol_root} → {symbol} (cached pro dia)")
        symbol = state.resolved_symbols.get(symbol_root)
        if not symbol:
            log(f"[WARN] Não resolveu símbolo {symbol_root}")
            continue

        # Coletar snapshot + anomalias
        snap = fetch_snapshot(symbol, CONFIG["timeframes"][0])
        if "error" not in snap:
            save_snapshot(snap)
            anomalies = detect_anomalies(snap)
            for a in anomalies:
                log_anomaly(symbol, a["type"], a)
                analyst_notify(a["type"], symbol, a["msg"])

        strategy = _get_strategy(symbol_root)
        params = _get_params(symbol_root)
        # Timeframes por símbolo (override do global)
        timeframes = CONFIG.get("timeframes_by_symbol", {}).get(symbol_root, CONFIG["timeframes"])

        for tf in timeframes:
            bars = fetch_bars(symbol, tf, CONFIG["bars_count"])
            if not bars or len(bars) < CONFIG["bars_count"]:
                continue

            atr = calculate_atr(bars, params.get("atr_period", 14))
            if atr == 0:
                continue

            last_close = bars[1]["close"]
            last_bar_ts = bars[1].get("time")

            pos = state.positions.get(f"{symbol}_{tf}")
            if pos:
                manage_position(symbol, tf, pos, atr, strategy, params)
            else:
                # Dispatch dinâmico de estratégia
                strategy_func = get_strategy_func(strategy)
                if strategy_func:
                    result = strategy_func(symbol, tf, last_close, atr,
                                           bar_ts=last_bar_ts, bars=bars,
                                           params=params, utils=_strategy_utils)
                    if result:
                        info = result.get("info", {})
                        info.pop("strategy", None)  # evita conflito com kwarg
                        _execute_entry(symbol, tf, result["direction"],
                                       last_close, result["sl_pts"], atr,
                                       last_bar_ts, strategy=strategy,
                                       **info)
                else:
                    log(f"[ERRO] Estratégia '{strategy}' não encontrada")


def check_entry_vwap(symbol: str, tf: str, price: float,
                     atr: float, bar_ts=None, bars=None, params=None):
    """Entrada via VWAP (para WDO — mercado trending)."""
    if params is None:
        params = CONFIG["wdo"]
    _reset_daily_counter()

    if not _check_cooldown(symbol, params):
        return

    if not _check_max_trades(params, symbol):
        log(f"[BLOQUEADO] {symbol} {tf} — máximo diário atingido")
        return

    if not _check_consecutive_losses(symbol):
        return

    # Market regime
    regime = "UNKNOWN"
    ema_slow_val_cfg = params.get("ema_slow", 21)
    if bars and len(bars) >= ema_slow_val_cfg + 5:
        regime = get_market_regime(bars, params)
        if regime == "CHOPPY":
            return  # silencioso — WDO em choppy não opera

    # VWAP
    vwap = calculate_vwap(bars, params.get("vwap_period", 20))
    if vwap == 0:
        return

    # Trend direction
    ema_fast = ema_slow_val = 0
    if bars and len(bars) >= ema_slow_val_cfg + 5:
        ema_fast = calculate_ema(bars, params.get("ema_fast", 9))
        ema_slow_val = calculate_ema(bars, ema_slow_val_cfg)

    # Threshold adaptativo
    atr_pct = (atr / price) if price > 0 else 0
    if atr_pct < 0.0015:
        buy_mult = 1.0005
        sell_mult = 0.9995
    elif atr_pct < 0.003:
        buy_mult = 1.0015
        sell_mult = 0.9985
    else:
        buy_mult = params.get("vwap_buy_threshold", 1.003)
        sell_mult = params.get("vwap_sell_threshold", 0.997)

    buy_thresh = vwap * buy_mult
    sell_thresh = vwap * sell_mult

    direction = None
    if price > buy_thresh:
        direction = "BUY"
    elif price < sell_thresh:
        direction = "SELL"

    if not direction:
        return

    # Trend filter
    if ema_fast > 0 and ema_slow_val > 0:
        if direction == "BUY" and ema_fast < ema_slow_val:
            return
        if direction == "SELL" and ema_fast > ema_slow_val:
            return

    # RSI filter
    rsi = 50
    rsi_period = params.get("rsi_period", 14)
    if bars and len(bars) >= rsi_period + 2:
        rsi = calculate_rsi(bars, rsi_period)
        if direction == "BUY" and rsi > params.get("rsi_overbought", 70):
            return
        if direction == "SELL" and rsi < params.get("rsi_oversold", 30):
            return

    # Defesas
    if not _defenses_ok(symbol, tf, direction, bar_ts):
        return

    # SL
    sl_pts = _calc_sl(symbol, atr, params)

    # Executar
    _execute_entry(symbol, tf, direction, price, sl_pts, atr, bar_ts,
                   strategy="VWAP", vwap=vwap, rsi=rsi, regime=regime,
                   ema_fast=ema_fast, ema_slow=ema_slow_val,
                   buy_thresh=buy_thresh, sell_thresh=sell_thresh)


def check_entry_bollinger(symbol: str, tf: str, price: float,
                          atr: float, bar_ts=None, bars=None, params=None):
    """Entrada via Bollinger Bands (para WIN — mercado choppy, reversão à média)."""
    if params is None:
        params = CONFIG["win"]
    _reset_daily_counter()

    if not _check_cooldown(symbol, params):
        return

    if not _check_max_trades(params, symbol):
        log(f"[BLOQUEADO] {symbol} {tf} — máximo diário atingido")
        return

    if not _check_consecutive_losses(symbol):
        return

    # Bollinger Bands
    bb_upper, bb_mid, bb_lower = calculate_bollinger(bars, params.get("bb_period", 20), params.get("bb_std", 2.0))
    if bb_upper == 0 or bb_lower == 0:
        return

    # RSI
    rsi = 50
    rsi_period = params.get("rsi_period", 14)
    if bars and len(bars) >= rsi_period + 2:
        rsi = calculate_rsi(bars, rsi_period)

    # Sinal: reversão à média
    direction = None
    high = bars[1]["high"]
    low = bars[1]["low"]

    rsi_buy = params.get("rsi_buy", 30)
    rsi_sell = params.get("rsi_sell", 75)
    if low <= bb_lower and rsi < rsi_buy:
        direction = "BUY"
    elif high >= bb_upper and rsi > rsi_sell:
        direction = "SELL"

    if not direction:
        return

    # Volume filter: só entra se volume > média (confirmação)
    if bars and len(bars) >= 20:
        avg_vol = sum(b.get("volume", 1) for b in bars[:20]) / 20
        current_vol = bars[1].get("volume", 1)
        if current_vol < avg_vol * 0.8:  # volume precisa ser >= 80% da média
            return

    # Trend filter (NOVO): só compra em uptrend, vende em downtrend
    # Mean reversion funciona melhor na direção da tendência
    if params.get("trend_filter", False) and bars and len(bars) >= 26:
        ema_f = calculate_ema(bars, params.get("ema_fast", 9))
        ema_s = calculate_ema(bars, params.get("ema_slow", 21))
        if ema_f > 0 and ema_s > 0:
            if direction == "BUY" and ema_f < ema_s:
                return  # mercado em downtrend, não comprar
            if direction == "SELL" and ema_f > ema_s:
                return  # mercado em uptrend, não vender

    # Defesas
    if not _defenses_ok(symbol, tf, direction, bar_ts):
        return

    # SL
    sl_pts = _calc_sl(symbol, atr, params)

    # Executar
    _execute_entry(symbol, tf, direction, price, sl_pts, atr, bar_ts,
                   strategy="BOLLINGER",
                   bb_upper=bb_upper, bb_mid=bb_mid, bb_lower=bb_lower,
                   rsi=rsi)


def check_entry_ema_crossover(symbol: str, tf: str, price: float,
                               atr: float, bar_ts=None, bars=None, params=None):
    """Entrada via EMA Crossover + ADX (para WIN — trend-following)."""
    if params is None:
        params = CONFIG["win"]
    _reset_daily_counter()

    if not _check_cooldown(symbol, params):
        return

    if not _check_max_trades(params, symbol):
        log(f"[BLOQUEADO] {symbol} {tf} — máximo diário atingido")
        return

    if not _check_consecutive_losses(symbol):
        return

    ema_fast_period = params.get("ema_fast", 12)
    ema_slow_period = params.get("ema_slow", 21)
    adx_period = params.get("adx_period", 14)
    adx_threshold = params.get("adx_threshold", 20)
    rsi_period = params.get("rsi_period", 14)
    rsi_ob = params.get("rsi_overbought", 70)
    rsi_os = params.get("rsi_oversold", 30)

    ema_fast_val = calculate_ema(bars, ema_fast_period)
    ema_slow_val = calculate_ema(bars, ema_slow_period)
    adx_val, plus_di, minus_di = calculate_adx(bars, adx_period)
    rsi = calculate_rsi(bars, rsi_period)

    if ema_fast_val == 0 or ema_slow_val == 0 or adx_val == 0:
        return

    if adx_val < adx_threshold:
        return

    prev_fast = calculate_ema(bars[1:], ema_fast_period) if len(bars) > ema_fast_period else ema_fast_val
    prev_slow = calculate_ema(bars[1:], ema_slow_period) if len(bars) > ema_slow_period else ema_slow_val

    direction = None
    if prev_fast <= prev_slow and ema_fast_val > ema_slow_val:
        direction = "BUY"
    elif prev_fast >= prev_slow and ema_fast_val < ema_slow_val:
        direction = "SELL"

    if not direction:
        return

    if direction == "BUY" and rsi > rsi_ob:
        return
    if direction == "SELL" and rsi < rsi_os:
        return

    if direction == "BUY" and plus_di < minus_di:
        return
    if direction == "SELL" and minus_di < plus_di:
        return

    if not _defenses_ok(symbol, tf, direction, bar_ts):
        return

    sl_pts = _calc_sl(symbol, atr, params)

    _execute_entry(symbol, tf, direction, price, sl_pts, atr, bar_ts,
                   strategy="EMA_CROSSOVER",
                   ema_fast=ema_fast_val, ema_slow=ema_slow_val,
                   adx=adx_val, plus_di=plus_di, minus_di=minus_di,
                   rsi=rsi)


def _calc_sl(symbol: str, atr: float, params: dict = None) -> int:
    """Calcula SL em pontos (unidade do executor = price * point).

    WIN: point=1.0 → sl_pts=200 = 200 pontos reais ✓
    WDO: point=0.001 → sl_pts precisa ser 1000x maior!
         sl_pts=20000 = 200 pontos reais ✓
    """
    if params is None:
        params = CONFIG["wdo"] if "WDO" in symbol else CONFIG["win"]
    sl_pts = int(atr * params.get("sl_atr_mult", 1.5))
    if "WIN" in symbol:
        sl_pts = max(sl_pts, 200)     # WIN point=1.0, mínimo 200 pts
    elif "WDO" in symbol:
        sl_pts = max(sl_pts, 200)     # pontos reais (ATR ~2-5)
        sl_pts *= 1000                # WDO point=0.001, multiplicar!
    return ((sl_pts + 4) // 5) * 5


def _defenses_ok(symbol: str, tf: str, direction: str, bar_ts) -> bool:
    """Verifica defesas anti-duplicação."""
    # Defesa 1: posição no state
    state_key = f"{symbol}_{tf}"
    if state.positions.get(state_key):
        return False

    # Defesa 2: posição no MT5
    try:
        status_data = status()
        mt5_positions = status_data.get("positions", [])
        for p in mt5_positions:
            if p.get("symbol") == symbol and p.get("type", "").lower().startswith(direction.lower()):
                return False
    except Exception:
        pass

    # Defesa 3: sinal idêntico na mesma barra
    sig_key = f"{symbol}_{tf}_{direction}"
    last = state.last_signals.get(sig_key)
    if last and last.get("bar_ts") is not None and last.get("bar_ts") == bar_ts:
        return False

    return True


def _execute_entry(symbol: str, tf: str, direction: str, price: float,
                   sl_pts: int, atr: float, bar_ts, strategy: str = "VWAP", **kwargs):
    """Executa entrada e registra tudo."""
    # Log
    detail_parts = [f"{strategy}"]
    if strategy == "VWAP":
        detail_parts.append(f"VWAP={kwargs.get('vwap', 0):.2f}")
    detail_parts.append(f"ATR={atr:.0f}")
    detail_parts.append(f"RSI={kwargs.get('rsi', 50):.1f}")
    if strategy == "VWAP":
        detail_parts.append(f"Regime={kwargs.get('regime', 'UNKNOWN')}")
    elif strategy == "BOLLINGER":
        detail_parts.append(f"BB=[{kwargs.get('bb_lower', 0):.0f}|{kwargs.get('bb_mid', 0):.0f}|{kwargs.get('bb_upper', 0):.0f}]")
    log(f"[SINAL] {symbol} {tf}: {direction} @ {price:.2f} | {' | '.join(detail_parts)}")

    # Ordem
    if direction == "BUY":
        result = buy(symbol, CONFIG["volume"], sl_pts=sl_pts)
    else:
        result = sell(symbol, CONFIG["volume"], sl_pts=sl_pts)

    if result.get("status") == "FILLED":
        ticket = result.get("ticket", "?")
        exec_price = result.get("price", price)

        state_key = f"{symbol}_{tf}"
        sig_key = f"{symbol}_{tf}_{direction}"

        # Trava anti-duplicação
        state.last_signals[sig_key] = {
            "ts": datetime.now(),
            "close": price,
            "bar_ts": bar_ts,
            "ticket": ticket,
            "direction": direction,
        }

        # Signal detail pro banco
        signal_detail = {
            "strategy": strategy,
            "atr": round(atr, 2),
            "rsi": round(kwargs.get("rsi", 50), 1),
            "sl_pts": sl_pts,
        }
        if strategy == "VWAP":
            signal_detail.update({
                "vwap": round(kwargs.get("vwap", 0), 2),
                "regime": kwargs.get("regime", "UNKNOWN"),
                "ema_fast": round(kwargs.get("ema_fast", 0), 0),
                "ema_slow": round(kwargs.get("ema_slow", 0), 0),
                "threshold_buy": round(kwargs.get("buy_thresh", 0), 2),
                "threshold_sell": round(kwargs.get("sell_thresh", 0), 2),
            })
        elif strategy == "BOLLINGER":
            signal_detail.update({
                "bb_upper": round(kwargs.get("bb_upper", 0), 2),
                "bb_mid": round(kwargs.get("bb_mid", 0), 2),
                "bb_lower": round(kwargs.get("bb_lower", 0), 2),
            })
        elif strategy == "EMA_CROSSOVER":
            signal_detail.update({
                "ema_fast": round(kwargs.get("ema_fast", 0), 2),
                "ema_slow": round(kwargs.get("ema_slow", 0), 2),
                "adx": round(kwargs.get("adx", 0), 1),
                "plus_di": round(kwargs.get("plus_di", 0), 1),
                "minus_di": round(kwargs.get("minus_di", 0), 1),
            })

        # Registrar no banco
        # entry_sl: calcular preço real do SL baseado no symbol
        if "WDO" in symbol:
            point_val = 0.001  # WDO point value
        else:
            point_val = 1.0   # WIN point value
        entry_sl_price = exec_price - sl_pts * point_val if direction == "BUY" else exec_price + sl_pts * point_val
        trade_id = log_entry(
            symbol=symbol, direction=direction,
            volume=CONFIG["volume"],
            entry_price=exec_price,
            entry_sl=entry_sl_price,
            entry_ticket=ticket,
            timeframe=tf,
            strategy=strategy,
            signal_detail=signal_detail,
            raw_json=result,
        )

        # Estado
        state.positions[state_key] = {
            "direction": direction,
            "entry_price": exec_price,
            "entry_ticket": ticket,
            "sl_pts": sl_pts,
            "atr": atr,
            "trail_on": False,
            "best_price": exec_price,
            "bar_count": 0,
            "trade_log_id": trade_id,
            "strategy": strategy,
            "bb_mid": kwargs.get("bb_mid", 0),
        }

        # Cooldown
        state.last_trade_time[symbol] = datetime.now()
        state.daily_trade_count += 1
        state.daily_trade_by_symbol[symbol] = state.daily_trade_by_symbol.get(symbol, 0) + 1

        # Notificação
        sl_label = exec_price - sl_pts * point_val if direction == "BUY" else exec_price + sl_pts * point_val
        strategy_label_map = {"VWAP": "VWAP", "BOLLINGER": "Bollinger", "EMA_CROSSOVER": "EMA Cross"}
        strategy_label = strategy_label_map.get(strategy, strategy)
        notify_telegram(
            f"📊 *{direction} {symbol} {tf}* ({strategy_label})\n"
            f"• Entrada: {exec_price:.2f} | SL: {sl_label:.2f}\n"
            f"• ATR: {atr:.0f} | RSI: {kwargs.get('rsi', 50):.1f}\n"
            f"• Trade {state.daily_trade_count}/dia"
        )
    else:
        reason = result.get("comment", result.get("error", "desconhecido"))
        log(f"[REJEITADO] {symbol} {tf} {direction}: {reason}")


def manage_position(symbol: str, tf: str, pos: dict, current_atr: float, strategy: str = "VWAP", params: dict = None):
    """Gerencia trailing stop e verifica saídas."""
    if params is None:
        params = CONFIG["wdo"] if "WDO" in symbol else CONFIG["win"]
    key = f"{symbol}_{tf}"
    direction = pos["direction"]
    entry_price = pos["entry_price"]
    atr = pos["atr"]
    sl_pts = pos["sl_pts"]
    best = pos["best_price"]
    trail_on = pos["trail_on"]
    bar_count = pos["bar_count"]
    trade_log_id = pos["trade_log_id"]
    point_val = 0.001 if "WDO" in symbol else 1.0  # WDO=R$0.001/pt, WIN=R$1/pt

    tick_data = tick(symbol)
    if not tick_data or tick_data.get("bid", 0) == 0:
        return

    current_price = tick_data["bid"] if direction == "BUY" else tick_data["ask"]

    # Atualizar melhor preço
    if direction == "BUY":
        best = max(best, tick_data["bid"])
    else:
        best = min(best, tick_data["ask"]) if best > 0 else tick_data["ask"]

    pos["best_price"] = best
    pos["bar_count"] = bar_count + 1

    # Lucro em pontos
    if direction == "BUY":
        profit_pts = best - entry_price
    else:
        profit_pts = entry_price - best

    # Trailing
    trail_act = params.get("trail_activate", 1.5)
    trail_dist_cfg = params.get("trail_distance", 0.5)
    if not trail_on and profit_pts >= trail_act * atr:
        trail_on = True
        pos["trail_on"] = True
        log(f"[TRAIL] Ativado trailing {symbol} | Lucro: {profit_pts:.0f} pts ({profit_pts/atr:.1f}x ATR)")

    old_sl = pos.get("sl_pts", 0)
    if trail_on:
        trail_dist = trail_dist_cfg * atr
        if direction == "BUY":
            new_sl = best - trail_dist
            if new_sl > entry_price - sl_pts:
                pos["sl_pts"] = int(entry_price - new_sl)  # distance from entry to new SL
        else:
            new_sl = best + trail_dist
            if new_sl < entry_price + sl_pts:
                pos["sl_pts"] = int(new_sl - entry_price)  # distance from entry to new SL

    # ===== BOLLINGER: Tight trailing na banda oposta =====
    if strategy == "BOLLINGER":
        bb_mid = pos.get("bb_mid", 0)
        if bb_mid > 0:
            if direction == "BUY" and current_price >= bb_mid and profit_pts > 0:
                tight_dist = 0.3 * atr
                new_sl = best - tight_dist
                if new_sl > entry_price - sl_pts:
                    pos["sl_pts"] = int(entry_price - new_sl)
            elif direction == "SELL" and current_price <= bb_mid and profit_pts > 0:
                tight_dist = 0.3 * atr
                new_sl = best + tight_dist
                if new_sl < entry_price + sl_pts:
                    pos["sl_pts"] = int(new_sl - entry_price)

    # Enviar modify SL pro MT5 se mudou (after both trailing + BB tight)
    if pos["sl_pts"] != old_sl:
        try:
            from mt5_orchestrator import modify_sl
            result = modify_sl(symbol, pos["entry_ticket"], pos["sl_pts"])
            if result.get("status") == "ok":
                log(f"[TRAIL] SL atualizado no MT5: {symbol} ticket={pos['entry_ticket']} → SL={pos['sl_pts']} pts")
            else:
                log(f"[TRAIL] Falha modify SL: {result.get('error', '?')}")
        except Exception as e:
            log(f"[TRAIL] Erro modify SL: {e}")

    # Verificar se posição ainda existe no MT5
    status_data = status()
    mt5_positions = status_data.get("positions", [])
    mt5_tickets = [str(p["ticket"]) for p in mt5_positions]

    if str(pos["entry_ticket"]) not in mt5_tickets:
        log(f"[FECHADO PELO SERVIDOR] {symbol} | Ticket {pos['entry_ticket']}")

        # Estimate PnL from price difference (position no longer in MT5)
        if direction == "BUY":
            profit = (current_price - entry_price) * point_val
        else:
            profit = (entry_price - current_price) * point_val

        exit_result = log_exit(
            trade_log_id,
            exit_price=current_price,
            exit_reason="SL_SERVIDOR",
            exit_ticket="server",
            swap=0,
            notes=f"Posição fechada pelo servidor MT5. PnL estimado: R${profit:.2f}",
        )
        if exit_result:
            pnl = exit_result.get("net_pnl", 0)
            state.daily_pnl += pnl
            state.trade_count += 1
            if pnl > 0:
                state.wins += 1
                state.consecutive_losses[symbol] = 0  # reset streak per symbol
            else:
                state.losses += 1
                state.consecutive_losses[symbol] = state.consecutive_losses.get(symbol, 0) + 1
                if state.consecutive_losses[symbol] >= state.max_consecutive_losses:
                    log(f"[HALT] {symbol}: {state.consecutive_losses[symbol]} perdas consecutivas! Pausando novas entradas.")
                    notify_telegram(
                        f"🛑 *HALT TRADING*\n"
                        f"{symbol}: {state.consecutive_losses.get(symbol, 0)} perdas consecutivas\n"
                        f"PnL diário: R$ {state.daily_pnl:+.2f}\n"
                        f"Aguardando reset (próximo dia)"
                    )

        notify_telegram(
            f"⚡ *Fechou {symbol}*\n"
            f"• {direction} | R$ {pnl:+.2f}\n"
            f"• SL atingido no servidor"
        )

        del state.positions[key]
        return


def close_all_and_report():
    """Fecha todas posições e gera relatório diário."""
    log("=== FECHANDO TUDO 16:45 ===")

    for key, pos in list(state.positions.items()):
        parts = key.rsplit("_", 1)
        symbol = parts[0]
        tf = parts[1] if len(parts) > 1 else "M5"

        result = close(symbol)
        log(f"Fechei {symbol}: {result}")

        tick_data = tick(symbol)
        exit_price = tick_data.get("bid", pos["entry_price"]) if tick_data else pos["entry_price"]

        exit_result = log_exit(
            pos["trade_log_id"],
            exit_price=exit_price,
            exit_reason="EOD_16:45",
            exit_ticket="eod",
            notes="Fechamento obrigatório de intraday",
        )
        if exit_result:
            pnl = exit_result.get("net_pnl", 0)
            state.daily_pnl += pnl
            state.trade_count += 1
            if pnl > 0:
                state.wins += 1
                state.consecutive_losses[symbol] = 0
            else:
                state.losses += 1
                state.consecutive_losses[symbol] = state.consecutive_losses.get(symbol, 0) + 1

    time.sleep(2)
    try:
        hist_result = _run_wine(EXECUTOR_WIN, "history")
        if isinstance(hist_result, dict) and "history" in hist_result:
            import_mt5_history(hist_result["history"])
    except Exception:
        pass

    state.closed = True

    today = datetime.now().strftime("%d/%m/%Y")
    db_summary = {}
    try:
        db_summary = get_daily_summary()
        n_trades_db = db_summary["total_trades"]
        net_pnl_db = db_summary["net_pnl"]
        best = db_summary["best_trade"]
        worst = db_summary["worst_trade"]
        wr = db_summary["win_rate"]
    except Exception:
        n_trades_db = state.trade_count
        net_pnl_db = state.daily_pnl
        best = worst = 0
        wr = 0

    try:
        mt5_status = status()
        acc = mt5_status.get("account", {})
        balance = acc.get("balance", 0)
        equity = acc.get("equity", 0)
        margin_free = acc.get("free_margin", 0)
    except Exception:
        balance = equity = margin_free = 0

    pnl_emoji = "🟢" if net_pnl_db >= 0 else "🔴"
    msg = (
        f"📊 *RELATÓRIO DIÁRIO Vibe-Trading*\n"
        f"📅 {today}\n"
        f"{'─' * 25}\n\n"
        f"🤖 *Estado da Conta*\n"
        f"• Saldo: R$ {balance:,.2f}\n"
        f"• Equity: R$ {equity:,.2f}\n"
        f"• Margem livre: R$ {margin_free:,.2f}\n\n"
    )

    msg += (
        f"📈 *Operações do Dia*\n"
        f"• Trades: {n_trades_db}\n"
        f"• Acertos: {db_summary.get('wins', state.wins)} ({wr:.0f}%)\n"
        f"• Erros: {db_summary.get('losses', state.losses)}\n"
    )

    if n_trades_db > 0:
        msg += (
            f"• Melhor: R$ {best:+.2f}\n"
            f"• Pior: R$ {worst:+.2f}\n"
        )

    msg += f"\n{pnl_emoji} *PnL Líquido: R$ {net_pnl_db:+.2f}*\n"

    try:
        mt5_status = status()
        mt5_positions = mt5_status.get("positions", [])
    except Exception:
        mt5_positions = []

    if mt5_positions:
        msg += f"\n📂 *Posições Abertas* ({len(mt5_positions)})\n"
        for p in mt5_positions:
            direction = p.get("type", "?")
            pnl_pos = p.get("profit", 0)
            emoji_pos = "🟢" if pnl_pos >= 0 else "🔴"
            msg += (
                f"  {emoji_pos} {p['symbol']} {direction} "
                f"@ {p['price_open']:,.0f} "
                f"SL={p['sl']:,.0f} "
                f"PnL=R$ {pnl_pos:+.2f}\n"
            )
    else:
        msg += "\n✅ Nenhuma posição aberta.\n"

    notify_telegram(msg)
    log(f"Relatório: {n_trades_db} trades, PnL R$ {net_pnl_db:+.2f}")


def run_once():
    init_db()
    log("Verificação única...")
    check_and_trade()
    log("Verificação concluída")


def recover_open_positions():
    try:
        mt5_status = status()
    except Exception as e:
        log(f"[RECOVER] Erro ao conectar MT5: {e}")
        return

    mt5_positions = mt5_status.get("positions", [])
    if not mt5_positions:
        log("[RECOVER] Nenhuma posição aberta no MT5")
        return

    log(f"[RECOVER] {len(mt5_positions)} posições abertas no MT5, verificando...")

    import sqlite3
    conn = sqlite3.connect("vt_trades.db")
    conn.row_factory = sqlite3.Row
    open_in_db = {r["entry_ticket"]: r for r in conn.execute(
        "SELECT * FROM trades WHERE exit_time IS NULL"
    ).fetchall()}
    conn.close()

    recovered = 0
    for p in mt5_positions:
        symbol = p["symbol"]
        symbol_root = "WIN" if "WIN" in symbol else "WDO" if "WDO" in symbol else None
        if symbol_root not in CONFIG["symbols"]:
            continue

        ticket = str(p.get("ticket", ""))
        comment = p.get("comment", "")
        if comment != "VibeTrading":
            continue

        already_managed = any(str(v.get("entry_ticket")) == ticket for v in state.positions.values())
        if already_managed:
            continue

        db_trade = open_in_db.get(ticket) or open_in_db.get(int(ticket))
        strategy = _get_strategy(symbol_root)
        params = _get_params(symbol_root)

        if db_trade:
            tf = db_trade["timeframe"] or "M5"
            direction = db_trade["direction"]
            entry_price = db_trade["entry_price"]
            atr = 0
            sl_pts = 0
            try:
                sig = json.loads(db_trade["signal_detail"]) if db_trade["signal_detail"] else {}
                atr = sig.get("atr", 0) or 0
                sl_pts = sig.get("sl_pts", 0) or 0
                if sig.get("strategy"):
                    strategy = sig["strategy"]
            except Exception:
                pass

            if not atr or not sl_pts:
                bars = fetch_bars(symbol, tf, CONFIG["bars_count"])
                atr_calc = calculate_atr(bars, params.get("atr_period", 14)) if bars else 0
                atr = atr_calc or 200
                sl_pts = _calc_sl(symbol, atr)
        else:
            direction = "BUY" if p["type"] == 0 else "SELL"
            entry_price = p["price_open"]
            tf = "M5"
            bars = fetch_bars(symbol, tf, CONFIG["bars_count"])
            atr = calculate_atr(bars, params.get("atr_period", 14)) if bars else 200
            sl_pts = _calc_sl(symbol, atr, params)

        current_price = p.get("price_current", entry_price)
        if direction == "BUY":
            profit_pts = current_price - entry_price
            best = max(entry_price, p.get("high_price", current_price))
        else:
            profit_pts = entry_price - current_price
            best = min(entry_price, p.get("low_price", current_price))

        trail_on = atr > 0 and profit_pts >= params.get("trail_activate", 1.5) * atr

        # BB mid pra estratégia Bollinger
        bb_mid = 0
        if strategy == "BOLLINGER" and bars:
            _, bb_mid, _ = calculate_bollinger(bars, params.get("bb_period", 20), params.get("bb_std", 2.0))

        state.positions[f"{symbol}_{tf}"] = {
            "direction": direction,
            "entry_price": entry_price,
            "entry_ticket": ticket,
            "sl_pts": sl_pts,
            "atr": atr,
            "trail_on": trail_on,
            "best_price": best,
            "bar_count": 999,
            "trade_log_id": db_trade["id"] if db_trade else None,
            "recovered": True,
            "strategy": strategy,
            "bb_mid": bb_mid,
        }

        sig_key = f"{symbol}_{tf}_{direction}"
        state.last_signals[sig_key] = {
            "ts": datetime.now(),
            "close": entry_price,
            "bar_ts": None,
            "ticket": int(ticket) if str(ticket).isdigit() else ticket,
            "direction": direction,
        }
        recovered += 1
        log(f"[RECOVER] ✅ {direction} {symbol} {tf} @ {entry_price:.2f} SL={sl_pts} trail={'on' if trail_on else 'off'} [{strategy}]")

    if recovered:
        notify_telegram(
            f"🔄 *Recuperadas {recovered} posição(ões)*\n"
            f"O bot está gerenciando trailing/SL normalmente"
        )


def run_daemon():
    global CONFIG
    init_db()
    _init_strategy_utils()
    load_strategies()
    state.started_at = datetime.now()

    # Log das estratégias
    strat_info = []
    for sym, strat in CONFIG["strategy"].items():
        strat_info.append(f"{sym}={strat}")
    strat_str = " | ".join(strat_info)

    log("=" * 60)
    log("Vibe-Trading Autotrader SPLIT INICIADO")
    log(f"Símbolos: {CONFIG['symbols']}")
    log(f"Estratégias: {strat_str}")
    log(f"WDO: SL {CONFIG['wdo']['sl_atr_mult']}x ATR | Trail {CONFIG['wdo']['trail_activate']}x/{CONFIG['wdo']['trail_distance']}x ATR")
    log(f"WIN: SL {CONFIG['win']['sl_atr_mult']}x ATR | Trail {CONFIG['win']['trail_activate']}x/{CONFIG['win']['trail_distance']}x ATR")
    log(f"WDO: Cooldown({CONFIG['wdo']['cooldown_seconds']}s) | Max({CONFIG['wdo']['max_daily_trades']}/dia)")
    log(f"WIN: Cooldown({CONFIG['win']['cooldown_seconds']}s) | Max({CONFIG['win']['max_daily_trades']}/dia)")
    log(f"Volume: {CONFIG['volume']} contrato(s)")
    log("=" * 60)

    notify_telegram(
        f"🚀 *Vibe-Trading Autotrader SPLIT*\n"
        f"⏰ {datetime.now().strftime('%d/%m/%Y %H:%M')}\n"
        f"📊 {strat_str}\n"
        f"⏱️ M5+M15 | 1 contratos\n"
        f"🎯 WDO SL {CONFIG['wdo']['sl_atr_mult']}x | WIN SL {CONFIG['win']['sl_atr_mult']}x ATR\n"
        f"🛡️ WDO: {CONFIG['wdo']['cooldown_seconds']}s/{CONFIG['wdo']['max_daily_trades']}t | WIN: {CONFIG['win']['cooldown_seconds']}s/{CONFIG['win']['max_daily_trades']}t"
    )

    recover_open_positions()

    while True:
        try:
            # Hot reload config + strategies
            CONFIG = load_config()
            reload_strategies()

            if is_close_time() and not state.closed:
                close_all_and_report()
                time.sleep(10)
                continue

            if not is_trading_time():
                if state.started_at:
                    log("Fora do horário de trading. Aguardando...")
                time.sleep(60)
                continue

            check_and_trade()
        except Exception as e:
            log(f"[ERRO] {e}")
            import traceback
            traceback.print_exc()

        time.sleep(CONFIG["check_interval"])


def main():
    if "--once" in sys.argv:
        run_once()
    elif "--close" in sys.argv:
        init_db()
        close_all_and_report()
    elif "--status" in sys.argv:
        init_db()
        s = status()
        print(json.dumps(s, indent=2, default=str))
    else:
        RESTART_FLAG = "/tmp/vt_autotrader_restart"

        def signal_handler(sig, frame):
            # Se flag de restart existe, NÃO fechar posições
            if os.path.exists(RESTART_FLAG):
                log("Restart detectado — NÃO fechando posições abertas")
                os.remove(RESTART_FLAG)
                sys.exit(0)
            log("Sinal de encerramento recebido. Fechando tudo...")
            if state.positions and not state.closed:
                close_all_and_report()
            sys.exit(0)

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        run_daemon()


if __name__ == "__main__":
    main()
