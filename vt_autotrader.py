#!/usr/bin/env python3
"""
Vibe-Trading Autotrader — Daemon autônomo com estratégias plugin.

Estratégias definidas em vt_config.json (atualmente EMA_PULLBACK para WDO e WIN).
Novas estratégias: adicione em strategies/ e referencie no config.

Funcionalidades:
- Estratégias por símbolo (configurável)
- SL obrigatório (ATR × multiplicador)
- Trailing stop (ativa X×ATR, distância Y×ATR)
- Breakeven automático após X minutos
- Time-trail após Y minutos
- Validação pós-envio com LLM (validator)
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
import shutil
import time
import json
import subprocess
import signal
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from vt_trade_log import init_db, log_entry, log_exit, import_mt5_history, get_daily_summary, sync_fees_from_mt5
from mt5_orchestrator import status, buy, sell, close, close_all, tick, modify_sl, _run_wine, EXECUTOR_WIN
from mt5_error_recovery import safe_buy, safe_sell, safe_modify_sl, safe_close
from vt_config_loader import load_config
from vt_strategy_loader import load_strategies, get_strategy_func, reload_strategies
from vt_order_validator import validate_order
from vt_calendar import is_trading_day, resolve_all_symbols, resolve_symbol, get_contract_expiry, _parse_contract_code

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
        self.max_consecutive_losses = 999  # DESATIVADO (demo mode) — era 3
        self.halt_until = {}              # per-symbol: {symbol: datetime} — halt until this time
        self.resolved_symbols = {}        # cache: {"WDO": "WDON26", "WIN": "WINM26"}
        self.resolved_day = ""            # dia do cache (reseta a cada dia)

    STATE_FILE = "/tmp/vt_autotrader_state.json"

    @staticmethod
    def _json_default(obj):
        """Serializa datetime/date pra JSON."""
        if isinstance(obj, (datetime,)):
            return obj.isoformat()
        if hasattr(obj, 'isoformat'):
            return obj.isoformat()
        return str(obj)

    def _serialize_positions(self):
        """Serializa positions tratando datetimes."""
        out = {}
        for k, v in self.positions.items():
            pos = {}
            for pk, pv in v.items():
                if isinstance(pv, datetime):
                    pos[pk] = pv.isoformat()
                else:
                    pos[pk] = pv
            out[k] = pos
        return out

    def to_dict(self):
        return {
            "positions": self._serialize_positions(),
            "last_signals": {k: {**v, "ts": v["ts"].isoformat() if isinstance(v.get("ts"), datetime) else None}
                             for k, v in self.last_signals.items()},
            "last_trade_time": {k: v.isoformat() if isinstance(v, datetime) else str(v)
                                for k, v in self.last_trade_time.items()},
            "daily_pnl": self.daily_pnl,
            "trade_count": self.trade_count,
            "wins": self.wins,
            "losses": self.losses,
            "started_at": str(self.started_at) if self.started_at else None,
            "closed": self.closed,
            "daily_trade_count": self.daily_trade_count,
            "current_day": str(self.current_day) if self.current_day else None,
            "daily_trade_by_symbol": self.daily_trade_by_symbol,
            "consecutive_losses": self.consecutive_losses,
            "halt_until": {k: v.isoformat() if isinstance(v, datetime) else str(v)
                           for k, v in self.halt_until.items()},
        }

    def save(self):
        """Persiste state em disco (escrita atômica)."""
        import json as _json
        import os
        tmp = self.STATE_FILE + ".tmp"
        try:
            with open(tmp, "w") as f:
                _json.dump(self.to_dict(), f, indent=2, default=self._json_default)
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp, self.STATE_FILE)
        except Exception as e:
            print(f"[STATE] Erro ao salvar: {e}", flush=True)
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def load(self):
        """Restaura state do disco (se existe e é do mesmo dia)."""
        import json as _json
        try:
            with open(self.STATE_FILE) as f:
                data = _json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"[STATE] Erro ao carregar state: {e} — resetando", flush=True)
            return

        saved_day = data.get("current_day")
        today = str(datetime.now().date())
        if saved_day != today:
            print(f"[STATE] State salvo é de {saved_day}, hoje é {today} — resetando", flush=True)
            return

        self.daily_trade_count = data.get("daily_trade_count", 0)
        self.current_day = datetime.strptime(saved_day, "%Y-%m-%d").date() if saved_day else None
        self.daily_trade_by_symbol = data.get("daily_trade_by_symbol", {})
        self.consecutive_losses = data.get("consecutive_losses", {})
        self.trade_count = data.get("trade_count", 0)
        self.wins = data.get("wins", 0)
        self.losses = data.get("losses", 0)
        self.daily_pnl = data.get("daily_pnl", 0)

        # Restaura halt_until (string → datetime)
        raw_halt = data.get("halt_until", {})
        self.halt_until = {}
        for k, v in raw_halt.items():
            try:
                self.halt_until[k] = datetime.fromisoformat(v)
            except (ValueError, TypeError):
                pass

        # Restaura positions (entry_time string → datetime)
        raw_pos = data.get("positions", {})
        self.positions = {}
        for k, v in raw_pos.items():
            pos = dict(v)
            if isinstance(pos.get("entry_time"), str):
                try:
                    pos["entry_time"] = datetime.fromisoformat(pos["entry_time"])
                except (ValueError, TypeError):
                    pass
            self.positions[k] = pos

        # Restaura last_trade_time (string → datetime)
        raw_ltt = data.get("last_trade_time", {})
        self.last_trade_time = {}
        for k, v in raw_ltt.items():
            try:
                self.last_trade_time[k] = datetime.fromisoformat(v)
            except (ValueError, TypeError):
                pass

        # Restaura last_signals (ts string → datetime)
        raw_sigs = data.get("last_signals", {})
        self.last_signals = {}
        for k, v in raw_sigs.items():
            sig = dict(v)
            if isinstance(sig.get("ts"), str):
                try:
                    sig["ts"] = datetime.fromisoformat(sig["ts"])
                except (ValueError, TypeError):
                    pass
            if isinstance(sig.get("bar_ts"), str):
                try:
                    sig["bar_ts"] = sig["bar_ts"]  # bar_ts pode ser int/string
                except (ValueError, TypeError):
                    pass
            self.last_signals[k] = sig

        print(f"[STATE] Restaurado: trades={self.daily_trade_count}, losses={self.consecutive_losses}, halt={self.halt_until}, positions={list(self.positions.keys())}", flush=True)


state = SessionState()
state.load()  # ← restaura do disco na inicialização
log_file = Path("/tmp/vt_autotrader.log")


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)


TELEGRAM_TARGET = "telegram:-1004284773048"


def notify_telegram(msg: str):
    try:
        from vt_hermes_helper import hermes_send
        ok = hermes_send(TELEGRAM_TARGET, msg)
        if not ok:
            log(f"[NOTIFY FAIL] hermes_send retornou False")
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
    for i in range(min(period, len(bars) - 1)):
        diff = bars[i]["close"] - bars[i + 1]["close"]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
    _n = max(len(gains), 1)
    avg_gain = sum(gains) / _n if gains else 0
    avg_loss = sum(losses) / _n if losses else 0.001
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
    # CRITICAL: bars do MT5 são newest-first; inverter para ordem cronológica
    # Sem isso, +DI/-DI ficam invertidos (tendência de alta parece queda)
    chron_bars = list(reversed(bars[:period * 2]))
    highs = [b["high"] for b in chron_bars]
    lows = [b["low"] for b in chron_bars]
    closes = [b["close"] for b in chron_bars]
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
        params = CONFIG.get("win", {})
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
    # Verifica dia útil + feriados B3
    ok, motivo = is_trading_day(now.date())
    if not ok:
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


def _get_strategy_for_tf(symbol_root: str, tf: str) -> str:
    """Retorna a estratégia para o símbolo+TF.
    Prioridade: strategy_by_tf["SYMBOL_TF"] > strategy[symbol] > VWAP
    """
    key = f"{symbol_root}_{tf}"
    by_tf = CONFIG.get("strategy_by_tf", {})
    if key in by_tf:
        return by_tf[key]
    return CONFIG["strategy"].get(symbol_root, "VWAP")


def _get_params_for_tf(symbol_root: str, tf: str) -> dict:
    """Retorna parâmetros para o símbolo+TF.
    Prioridade: params_by_tf["symbol_tf"] > params[symbol] > {}
    """
    key = f"{symbol_root}_{tf}"
    by_tf = CONFIG.get("params_by_tf", {})
    base = CONFIG.get(symbol_root.lower(), {})
    # Tentar match case-insensitive
    if key in by_tf:
        return {**base, **by_tf[key]}
    key_lower = key.lower()
    if key_lower in by_tf:
        return {**base, **by_tf[key_lower]}
    return base


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
        state.save()  # persistir reset diário


def _is_safe_time_window() -> bool:
    """Evita operar nos primeiros/últimos minutos da sessão.
    - warmup_minutes: primeiros X min após abertura (mercado definindo direção)
    - winddown_minutes: últimos X min antes do fechamento (risco de gap/ilha)
    Retorna True se está em janela segura.
    """
    now = datetime.now()
    current = now.hour * 60 + now.minute
    start = CONFIG["start_hour"] * 60 + CONFIG["start_minute"]
    end = CONFIG["close_hour"] * 60 + CONFIG["close_minute"]
    warmup = CONFIG.get("warmup_minutes", 15)
    winddown = CONFIG.get("winddown_minutes", 15)
    # Primeiros warmup minutos após abertura
    if start <= current <= start + warmup:
        return False
    # Últimos winddown minutos antes do fechamento
    if end - winddown <= current <= end:
        return False
    return True


def _check_cooldown(symbol: str, params: dict, tf: str = "", direction: str = "") -> bool:
    """Retorna True se pode operar (cooldown ok).
    Cooldown por (symbol, tf, direction) para evitar reversões rápidas.
    Falls back a cooldown por symbol se tf/direction vazios.
    """
    now = datetime.now()
    cd = params.get("cooldown_seconds", 300)
    if tf and direction:
        key = f"{symbol}_{tf}_{direction}"
        last_time = state.last_trade_time.get(key)
        if last_time:
            elapsed = (now - last_time).total_seconds()
            if elapsed < cd:
                return False
    # Também checa cooldown por symbol (proteção geral)
    last_time_sym = state.last_trade_time.get(symbol)
    if last_time_sym:
        elapsed = (now - last_time_sym).total_seconds()
        if elapsed < cd * 0.6:  # symbol-level cooldown pode ser 60% do per-direction
            return False
    return True


def _check_max_trades(params: dict, symbol: str = "") -> bool:
    """Retorna True se pode operar (limite não atingido). Conta por símbolo."""
    # ── KILL SWITCH: Max daily loss ──
    max_daily_loss = CONFIG.get("max_daily_loss", -500)
    if state.daily_pnl <= max_daily_loss:
        log(f"🛑 KILL SWITCH: PnL diário R$ {state.daily_pnl:.2f} ≤ limite R$ {max_daily_loss:.2f} — TRAVADO")
        return False

    # ── KILL SWITCH: disabled_ativos (AGI pode desativar ativos que perdem) ──
    disabled = CONFIG.get("disabled_symbols", [])
    if symbol in disabled:
        return False

    # Limite global (segurança)
    if state.daily_trade_count >= 50:
        return False
    # Limite por símbolo
    sym_count = state.daily_trade_by_symbol.get(symbol, 0)
    max_per_sym = params.get("max_daily_trades", 15)
    if sym_count >= max_per_sym:
        return False
    return True


def _check_consecutive_losses(symbol: str) -> bool:
    """Retorna True se pode operar (sem sequência de derrotas)."""
    # Check halt_until first
    halt_time = state.halt_until.get(symbol)
    if halt_time and datetime.now() < halt_time:
        remaining = (halt_time - datetime.now()).total_seconds() / 60
        log(f"[BLOQUEADO] {symbol} — HALT ativo, {remaining:.0f}min restantes")
        return False
    
    # Se 3+ perdas consecutivas no símbolo, pausar
    sym_losses = state.consecutive_losses.get(symbol, 0)
    if sym_losses >= state.max_consecutive_losses:
        from datetime import timedelta
        state.halt_until[symbol] = datetime.now() + timedelta(hours=1)
        log(f"[HALT] {symbol}: {sym_losses} perdas consecutivas! Pausado 1h")
        return False
    if sym_losses > 0:
        log(f"[DEBUG] {symbol} — {sym_losses}/{state.max_consecutive_losses} perdas consecutivas")
    return True


def check_and_trade():
    from vt_analyst import fetch_snapshot, save_snapshot, detect_anomalies, log_anomaly, notify as analyst_notify

    _reset_daily_counter()  # ← sempre resetar no início do ciclo

    # Safety: avoid first/last 15 min of session
    if not _is_safe_time_window():
        return

    for symbol_root in CONFIG["symbols"]:
        # Se o config tem symbol resolvido (ex: "WDO": "WDON26"), usa direto
        # Caso contrário, resolve e cacheia no state
        today_str = datetime.now().strftime("%Y-%m-%d")
        
        # Verificar se config tem símbolos resolvidos
        resolved_map = CONFIG.get("resolved_symbols", {})
        if resolved_map.get(symbol_root):
            symbol = resolved_map[symbol_root]
        else:
            log(f"[ERROR] Símbolo {symbol_root} não encontrado em resolved_symbols. Verifique vt_config.json.")
            continue

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
                analyst_notify(a["type"], symbol, a["msg"], a.get("tf", ""))

        # Strategy/params per TF (with fallback to symbol-level)
        _default_strategy = _get_strategy(symbol_root)
        _default_params = _get_params(symbol_root)
        # Timeframes por símbolo (override do global)
        timeframes = CONFIG.get("timeframes_by_symbol", {}).get(symbol_root, CONFIG["timeframes"])

        for tf in timeframes:
            # ── KILL SWITCH: TF desativado pelo AGI ──
            disabled_tfs = CONFIG.get("disabled_timeframes", [])
            if f"{symbol_root}_{tf}" in disabled_tfs:
                continue

            strategy = _get_strategy_for_tf(symbol_root, tf)
            params = _get_params_for_tf(symbol_root, tf)
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
                # ===== SAFETY CHECKS (cooldown, max, consecutive losses) =====
                # Cooldown precisa de tf e direction — mas ainda não sabemos a direction
                # do sinal. Pré-checa por symbol-level apenas aqui; por direction
                # é re-checado dentro da strategy_func antes de executar.
                if not _check_cooldown(symbol, params, tf=tf):
                    continue
                if not _check_max_trades(params, symbol):
                    log(f"[BLOQUEADO] {symbol} {tf} — máximo diário atingido")
                    continue
                if not _check_consecutive_losses(symbol):
                    continue

                # Dispatch dinâmico de estratégia
                strategy_func = get_strategy_func(strategy)
                if strategy_func:
                    result = strategy_func(symbol, tf, last_close, atr,
                                           bar_ts=last_bar_ts, bars=bars,
                                           params=params, utils=_strategy_utils)
                    if result:
                        info = result.get("info", {})
                        # Pitfall #2 fix: pop TODOS os campos que conflitam com
                        # _execute_entry params (strategy, atr, sl_pts, direction, price, symbol, tf, bar_ts).
                        # Sem isso, spread **info causa "got multiple values for argument X".
                        for k in ("strategy", "atr", "sl_pts", "direction", "price", "symbol", "tf", "bar_ts"):
                            info.pop(k, None)
                        # DEFESAS: plugins não chamam _defenses_ok — validar aqui
                        if not _defenses_ok(symbol, tf, result["direction"], last_bar_ts):
                            continue
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
        params = CONFIG.get("win", {})
    _reset_daily_counter()

    if not _check_cooldown(symbol, params, tf=tf):
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
        params = CONFIG.get("win", {})
    _reset_daily_counter()

    if not _check_cooldown(symbol, params, tf=tf):
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
        params = CONFIG.get("win", {})
    _reset_daily_counter()

    if not _check_cooldown(symbol, params, tf=tf):
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
    """Calcula SL em unidades do executor (sl_pts * point = distância em preço).

    ATR vem em "pontos nativos" do preço (ex: DOL ATR≈4.5 pts).
    point_mult converte pra unidades do executor (sl_pts * mt5_point = dist).

    min_native é o SL MÍNIMO em pontos nativos (antes do point_mult):
    - WIN/IND: 150 pts (1.0 → sl_pts direto)
    - WDO/DOL: 3 pts  (point=0.001 → sl_pts * 1000)
    - BIT:     30 pts  (point=0.01  → sl_pts * 100)
    - WSP:      5 pts  (point=0.01  → sl_pts * 100)

    MAX_NATIVE é o SL MÁXIMO (proteção contra ATR inflado ou sl_atr_mult muito alto):
    - BIT:    500 pts nativos (com mult 0.5 = R$ 250 de risco máximo)
    - IND:    600 pts nativos (com mult 1.0 = R$ 600 de risco)
    - WDO/DOL: 80 pts nativos (com mult 10/50 = R$ 800/4000 de risco)
    - WIN:    800 pts nativos (com mult 0.2 = R$ 160 de risco)
    - WSP:   300 pts nativos (com mult 2.5 = R$ 750 de risco)
    """
    _root = "WIN" if "WIN" in symbol else "WDO" if "WDO" in symbol else \
            "BIT" if "BIT" in symbol else "DOL" if "DOL" in symbol else \
            "IND" if "IND" in symbol else "WSP" if "WSP" in symbol else "WIN"

    if params is None:
        params = CONFIG.get(_root.lower(), CONFIG.get("win", {}))

    # Specs: min/max_native em pontos do preço, point_mult = 1/mt5_point
    # max_native calibrado para limitar loss máximo POR TRADE em ~R$150-250
    _specs = {
        "WIN": {"min_native": 150, "max_native": 800,  "point_mult": 1},      # R$160 max loss
        "WDO": {"min_native": 3,   "max_native": 12,   "point_mult": 1000},    # R$120 max loss
        "BIT": {"min_native": 30,  "max_native": 500,  "point_mult": 100},     # R$500 max (ATR grande)
        "DOL": {"min_native": 3,   "max_native": 200,  "point_mult": 1000},    # R$200 max loss
        "IND": {"min_native": 150, "max_native": 350,  "point_mult": 1},       # R$350 max loss
        "WSP": {"min_native": 5,   "max_native": 200,  "point_mult": 100},     # R$200 max loss
    }
    spec = _specs.get(_root, {"min_native": 100, "max_native": 500, "point_mult": 1})

    # SL em pontos nativos (= distância em preço)
    sl_native = int(atr * params.get("sl_atr_mult", 1.5))
    # Aplicar limites min/max (CRÍTICO: max protege contra losses catastróficos)
    sl_native = max(spec["min_native"], min(sl_native, spec["max_native"]))

    # Converter pra unidades do executor
    sl_pts = sl_native * spec["point_mult"]

    # Arredondar pra múltiplo de 5
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

    # Defesa 4: cooldown por (symbol, tf, direction) — evita reversão rápida
    dir_key = f"{symbol}_{tf}_{direction}"
    last_dir_time = state.last_trade_time.get(dir_key)
    if last_dir_time:
        _root = symbol[:3] if len(symbol) >= 3 else symbol
        _params = CONFIG.get(_root.lower(), CONFIG.get("win", {}))
        cd = _params.get("cooldown_seconds", 300)
        if (datetime.now() - last_dir_time).total_seconds() < cd:
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

    # Ordem com auto-recuperação
    _vol_by_sym = CONFIG.get("volume_by_symbol", {})
    _root_vol = ""
    for r in ["WIN", "WDO", "BIT", "DOL", "IND", "WSP"]:
        if r in symbol:
            _root_vol = r
            break
    _vol = _vol_by_sym.get(_root_vol, CONFIG["volume"])
    if direction == "BUY":
        result = safe_buy(symbol, _vol, sl_pts=sl_pts, strategy=strategy)
    else:
        result = safe_sell(symbol, _vol, sl_pts=sl_pts, strategy=strategy)

    if result.get("status") == "FILLED":
        ticket = result.get("ticket", "?")
        exec_price = result.get("price", price)

        # ===== VALIDAÇÃO PÓS-ENVIO =====
        try:
            order_data = {
                "symbol": symbol,
                "direction": direction,
                "entry_price": exec_price,
                "sl_pts": sl_pts,
                "atr": atr,
                "strategy": strategy,
                "volume": _vol,
                "ticket": ticket,
            }
            use_llm = CONFIG.get("validate_with_llm", False)
            validation = validate_order(order_data, use_llm=use_llm)

            # Se LLM sugeriu correção de SL (SEMPRE aplicar)
            if validation.get("suggested_action") and validation["suggested_action"].get("type") == "MODIFY_SL":
                action = validation["suggested_action"]
                new_sl = action["suggested_sl"]
                reason = action.get("reason", "")
                risco = action.get("risco", "")

                # Notificar Bruno no Telegram
                _tf_msg = f"{symbol} {direction} @ {exec_price} | SL: {sl_pts}pts → {new_sl}pts"
                _rag = f"\n⚠️ Risco: {risco}" if risco else ""
                notify_telegram(f"🤖 [VALIDATOR] SL sugerido:\n{_tf_msg}\n📝 {reason}{_rag}")

                # Bounds check: garantir SL dentro dos limites seguros
                _root = "WIN" if "WIN" in symbol else "WDO" if "WDO" in symbol else \
                        "BIT" if "BIT" in symbol else "DOL" if "DOL" in symbol else \
                        "IND" if "IND" in symbol else "WSP" if "WSP" in symbol else "WIN"
                _limits = {"WDO": {"min": 3000, "max": 300000}, "WIN": {"min": 200, "max": 3000},
                           "BIT": {"min": 3000, "max": 500000}, "DOL": {"min": 3000, "max": 300000},
                           "IND": {"min": 200, "max": 3000}, "WSP": {"min": 500, "max": 30000}
                          }.get(_root, {"min": 200, "max": 50000})
                if isinstance(new_sl, (int, float)) and _limits["min"] <= new_sl <= _limits["max"]:
                    # Pitfall fix: re-aplicar max_native DEPOIS da correção do validator.
                    # Sem isso, validator pode amplificar SL além do risco máximo por trade.
                    _specs = {
                        "WIN": {"max_native": 800,  "point_mult": 1},
                        "WDO": {"max_native": 12,   "point_mult": 1000},
                        "BIT": {"max_native": 500,  "point_mult": 100},
                        "DOL": {"max_native": 200,  "point_mult": 1000},
                        "IND": {"max_native": 350,  "point_mult": 1},
                        "WSP": {"max_native": 200,  "point_mult": 100},
                    }
                    _spec = _specs.get(_root, {"max_native": 500, "point_mult": 1})
                    _sl_native = new_sl / _spec["point_mult"]
                    _max_exec = _spec["max_native"] * _spec["point_mult"]
                    if new_sl > _max_exec:
                        log(f"[VALIDATOR] SL {int(new_sl)}pts ({_sl_native:.0f} nativos) excede max_native {_spec['max_native']}pts → clampado para {_max_exec}pts")
                        new_sl = _max_exec
                    log(f"[VALIDATOR] Corrigindo SL: {sl_pts}pts → {int(new_sl)}pts ({reason})")
                    fix_result = safe_modify_sl(symbol, ticket, int(new_sl), exec_price, direction)
                    if fix_result.get("status") == "ok":
                        sl_pts = int(new_sl)
                        log(f"[VALIDATOR] SL corrigido com sucesso para {sl_pts}pts")
                        notify_telegram(f"✅ SL aplicado: {symbol} ticket={ticket} → {sl_pts}pts")
                    else:
                        log(f"[VALIDATOR] Falha ao corrigir SL: {fix_result}")
                        notify_telegram(f"❌ Falha ao aplicar SL: {fix_result}")
                else:
                    log(f"[VALIDATOR] LLM sugeriu SL fora dos limites ({new_sl}pts [{_limits['min']}-{_limits['max']}]), ignorado")
            elif validation.get("llm_analysis"):
                # LLM analisou e não sugeriu mudança — loga resumo
                log(f"[VALIDATOR] LLM OK: {validation['llm_analysis'][:150]}")
            elif not validation.get("llm_analysis") and validation.get("alerts"):
                # LLM falhou mas há alertas locais — aplicar correção local
                for alert in validation["alerts"]:
                    if "suggestion" in alert:
                        # Extrair valor sugerido da sugestão
                        import re
                        match = re.search(r'(\d+)pts', alert["suggestion"])
                        if match:
                            suggested_pts = int(match.group(1))
                            if suggested_pts != sl_pts:
                                log(f"[VALIDATOR] LLM falhou, aplicando correção local: {sl_pts}pts → {suggested_pts}pts")
                                fix_result = safe_modify_sl(symbol, ticket, suggested_pts, exec_price, direction)
                                if fix_result.get("status") == "ok":
                                    sl_pts = suggested_pts
                                    log(f"[VALIDATOR] SL corrigido localmente para {sl_pts}pts")
                                    notify_telegram(f"✅ SL aplicado (local): {symbol} ticket={ticket} → {sl_pts}pts")
                                break
        except Exception as e:
            log(f"[VALIDATOR] Erro na validação (não bloqueante): {e}")

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
        _point_map = {"WIN": 1.0, "WDO": 0.001, "BIT": 0.01, "DOL": 0.001, "IND": 1.0, "WSP": 0.01}
        _root_pv = "WIN" if "WIN" in symbol else "WDO" if "WDO" in symbol else \
                   "BIT" if "BIT" in symbol else "DOL" if "DOL" in symbol else \
                   "IND" if "IND" in symbol else "WSP" if "WSP" in symbol else "WIN"
        point_val = _point_map.get(_root_pv, 1.0)
        entry_sl_price = exec_price - sl_pts * point_val if direction == "BUY" else exec_price + sl_pts * point_val
        trade_id = log_entry(
            symbol=symbol, direction=direction,
            volume=_vol,
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
            "entry_time": datetime.now(),
        }

        # Cooldown (por symbol, tf, direction — evita reversões rápidas)
        now = datetime.now()
        state.last_trade_time[symbol] = now
        state.last_trade_time[f"{symbol}_{tf}"] = now
        state.last_trade_time[f"{symbol}_{tf}_{direction}"] = now
        state.daily_trade_count += 1
        state.daily_trade_by_symbol[symbol] = state.daily_trade_by_symbol.get(symbol, 0) + 1
        state.save()  # persistir estado

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
    """Gerencia trailing stop e verifica saídas.

    Proteções anti-drawdown:
    1. Breakeven: após breakeven_minutes sem trailing, move SL pra entry + custo
    2. Time trailing: após time_trail_minutes, aperta trailing mesmo sem trail_activate
    3. Max position: após max_position_minutes, trailing agressivo (0.3x ATR)
    """
    if params is None:
        _root = "WIN" if "WIN" in symbol else "WDO" if "WDO" in symbol else \
                "BIT" if "BIT" in symbol else "DOL" if "DOL" in symbol else \
                "IND" if "IND" in symbol else "WSP" if "WSP" in symbol else "WIN"
        params = CONFIG.get(_root.lower(), CONFIG.get("win", {}))
    key = f"{symbol}_{tf}"
    direction = pos["direction"]
    entry_price = pos["entry_price"]
    atr = pos["atr"]
    sl_pts = pos["sl_pts"]
    best = pos["best_price"]
    trail_on = pos["trail_on"]
    bar_count = pos["bar_count"]
    trade_log_id = pos["trade_log_id"]
    # Point value per symbol
    _point_map = {"WIN": 1.0, "WDO": 0.001, "BIT": 0.01, "DOL": 0.001, "IND": 1.0, "WSP": 0.01}
    _root_pv = "WIN" if "WIN" in symbol else "WDO" if "WDO" in symbol else \
               "BIT" if "BIT" in symbol else "DOL" if "DOL" in symbol else \
               "IND" if "IND" in symbol else "WSP" if "WSP" in symbol else "WIN"
    point_val = _point_map.get(_root_pv, 1.0)

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

    # Tempo de posição em minutos (check_interval = 30s por padrão)
    check_interval = CONFIG.get("check_interval", 30)
    pos_minutes = bar_count * check_interval / 60

    # Parâmetros de proteção temporal
    breakeven_min = params.get("breakeven_minutes", 10)
    time_trail_min = params.get("time_trail_minutes", 20)
    max_pos_min = params.get("max_position_minutes", 60)
    trail_act = params.get("trail_activate", 1.0)
    trail_dist_cfg = params.get("trail_distance", 0.4)
    hard_exit_min = params.get("hard_exit_minutes", 45)  # FORÇA exit a mercado após X min

    # ===== FORCED EXIT — fecha posição a mercado após hard_exit_min =====
    # Previne desastres como #66 (WDO -R$566 em 375min) e #104 (BIT -R$901 em 104min)
    if pos_minutes >= hard_exit_min:
        log(f"[HARD_EXIT] {symbol} {direction} — {pos_minutes:.0f}min >= {hard_exit_min}min. Fechando a mercado.")
        try:
            close_result = safe_close(symbol)
            if close_result and close_result.get("status") == "ok":
                # PnL será calculado no próximo ciclo quando servidor fechar
                notify_telegram(
                    f"⏱️ *HARD EXIT* {symbol}\n"
                    f"• {direction} | {pos_minutes:.0f}min\n"
                    f"• Fechado por tempo máximo"
                )
                return  # posição será detectada como fechada no próximo ciclo
            else:
                log(f"[HARD_EXIT] Falha ao fechar {symbol}: {close_result}")
        except Exception as e:
            log(f"[HARD_EXIT] Erro: {e}")

    # ===== TRAILING POR LUCRO (original) =====
    if not trail_on and atr > 0 and profit_pts >= trail_act * atr:
        trail_on = True
        pos["trail_on"] = True
        log(f"[TRAIL] Ativado trailing {symbol} | Lucro: {profit_pts:.0f} pts ({profit_pts/atr:.1f}x ATR)")

    # ===== PROTEÇÃO 1: BREAKEVEN =====
    # Após X minutos sem trailing, move SL pra entry + custo mínimo
    # sl_pts é ALWAYS POSITIVO (distância). cmd_modify converte pra preço.
    be_applied = False
    if not trail_on and pos_minutes >= breakeven_min and atr > 0:
        cost_pts = int(5 / point_val)  # custo aprox (comissão + slippage) em pontos
        if direction == "BUY":
            # BUY: breakeven = SL no entry + custo (SL = entry + custo*point)
            be_sl_pts = cost_pts  # positivo → SL = entry - cost_pts*point_val (abaixo de entry mas perto)
            if be_sl_pts < abs(sl_pts):  # menor distância = SL mais apertado = melhor
                result = safe_modify_sl(symbol, pos["entry_ticket"], be_sl_pts, entry_price, direction)
                if result.get("status") == "ok":
                    pos["sl_pts"] = be_sl_pts
                    sl_pts = be_sl_pts  # CRITICAL: refresh local para trailing não afrouxar
                    be_price = entry_price + cost_pts * point_val
                    log(f"[BREAKEVEN] {symbol} BUY após {pos_minutes:.0f}min | SL → {be_price:.2f} ({be_sl_pts}pts)")
                    be_applied = True
        else:
            # SELL: breakeven = SL no entry - custo (SL = entry + cost_pts*point_val)
            be_sl_pts = cost_pts
            if be_sl_pts < abs(sl_pts):
                result = safe_modify_sl(symbol, pos["entry_ticket"], be_sl_pts, entry_price, direction)
                if result.get("status") == "ok":
                    pos["sl_pts"] = be_sl_pts
                    sl_pts = be_sl_pts  # CRITICAL: refresh local para trailing não afrouxar
                    be_price = entry_price - cost_pts * point_val
                    log(f"[BREAKEVEN] {symbol} SELL após {pos_minutes:.0f}min | SL → {be_price:.2f} ({be_sl_pts}pts)")
                    be_applied = True

    # ===== PROTEÇÃO 2: TIME-BASED TRAILING =====
    # Após Y minutos, ativa trailing mesmo sem atingir trail_activate
    if not trail_on and pos_minutes >= time_trail_min and profit_pts > 0:
        trail_on = True
        pos["trail_on"] = True
        log(f"[TIME_TRAIL] Ativado por tempo {symbol} após {pos_minutes:.0f}min | Lucro: {profit_pts:.0f}pts")

    # ===== TRAILING STOP =====
    # Calcula novo SL mas NÃO aplica no state até MT5 confirmar.
    # Convenção: sl_pts é ALWAYS POSITIVO (distância em executor units).
    # cmd_modify: BUY sl = entry - sl_pts*point, SELL sl = entry + sl_pts*point
    new_sl_pts = None  # candidato (só aplica se MT5 confirmar)
    if trail_on and atr > 0:
        # Proteção 3: após max_position_minutes, trailing mais agressivo
        if pos_minutes >= max_pos_min:
            trail_dist = 0.3 * atr  # agressivo
        else:
            trail_dist = trail_dist_cfg * atr

        if direction == "BUY":
            new_sl_price = best - trail_dist
            old_sl_price = entry_price - abs(sl_pts) * point_val
            if new_sl_price > old_sl_price and new_sl_price > 0:
                # sl_pts SIGNED: positivo=abaixo entry (loss), negativo=acima entry (profit lock)
                # cmd_modify: BUY SL = entry - sl_pts*point → sl_pts negativo = SL acima entry ✓
                new_sl_pts = int((entry_price - new_sl_price) / point_val)
        else:
            new_sl_price = best + trail_dist
            old_sl_price = entry_price + abs(sl_pts) * point_val
            if new_sl_price < old_sl_price and new_sl_price > 0:
                # SELL: sl_pts signed. cmd_modify: SELL SL = entry + sl_pts*point
                # sl_pts negativo = SL abaixo entry (profit lock) ✓
                new_sl_pts = int((new_sl_price - entry_price) / point_val)

    # ===== BOLLINGER: Tight trailing na banda oposta =====
    if strategy == "BOLLINGER":
        bb_mid = pos.get("bb_mid", 0)
        if bb_mid > 0:
            if direction == "BUY" and current_price >= bb_mid and profit_pts > 0:
                tight_dist = 0.3 * atr
                tight_sl_price = best - tight_dist
                old_sl_price = entry_price - abs(sl_pts) * point_val
                if tight_sl_price > old_sl_price and tight_sl_price > 0:
                    tight_pts = int((entry_price - tight_sl_price) / point_val)
                    if tight_pts != 0 and (new_sl_pts is None or tight_pts < new_sl_pts):
                        new_sl_pts = tight_pts
            elif direction == "SELL" and current_price <= bb_mid and profit_pts > 0:
                tight_dist = 0.3 * atr
                tight_sl_price = best + tight_dist
                old_sl_price = entry_price + abs(sl_pts) * point_val
                if tight_sl_price < old_sl_price and tight_sl_price > 0:
                    tight_pts = int((tight_sl_price - entry_price) / point_val)
                    if tight_pts != 0 and (new_sl_pts is None or tight_pts < new_sl_pts):
                        new_sl_pts = tight_pts

    # Enviar modify SL pro MT5 — só atualiza state se MT5 confirmar
    # sl_pts pode ser NEGATIVO (profit-lock). cmd_modify já suporta:
    #   BUY: SL = entry - pts*point (pts<0 → SL acima entry ✓)
    #   SELL: SL = entry + pts*point (pts<0 → SL abaixo entry ✓)
    if new_sl_pts is not None and new_sl_pts != 0 and new_sl_pts != sl_pts:
        try:
            result = safe_modify_sl(symbol, pos["entry_ticket"], new_sl_pts, entry_price, direction)
            if result.get("status") == "ok":
                pos["sl_pts"] = new_sl_pts
                log(f"[TRAIL] SL atualizado no MT5: {symbol} ticket={pos['entry_ticket']} → SL={new_sl_pts} pts")
            else:
                log(f"[TRAIL] Falha modify SL: {result.get('error', '?')} (mantido {abs(sl_pts)}pts)")
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
            notes=f"Posição fechada pelo servidor MT5. PnL estimado: R${profit:.2f}. Fees serão sincronizados no EOD.",
        )
        pnl = 0  # default para quando exit_result falha
        if exit_result:
            pnl = exit_result.get("net_pnl", 0)
            state.daily_pnl += pnl
            state.trade_count += 1
            if pnl > 0:
                state.wins += 1
                state.consecutive_losses[symbol] = 0  # reset streak per symbol
                state.halt_until.pop(symbol, None)  # clear halt on win
            else:
                state.losses += 1
                state.consecutive_losses[symbol] = state.consecutive_losses.get(symbol, 0) + 1
                if state.consecutive_losses[symbol] >= state.max_consecutive_losses:
                    from datetime import timedelta
                    state.halt_until[symbol] = datetime.now() + timedelta(hours=1)
                    log(f"[HALT] {symbol}: {state.consecutive_losses[symbol]} perdas consecutivas! Pausado até {state.halt_until[symbol].strftime('%H:%M')}")
                    notify_telegram(
                        f"🛑 *HALT TRADING*\n"
                        f"{symbol}: {state.consecutive_losses.get(symbol, 0)} perdas consecutivas\n"
                        f"PnL diário: R$ {state.daily_pnl:+.2f}\n"
                        f"Aguardando reset (próximo dia)"
                    )

        log(f"[FECHADO] {symbol} {tf} — PnL estimado R\${pnl:+.2f}, notificando Telegram...")
        notify_telegram(
            f"⚡ *Fechou {symbol} {tf}*\n"
            f"• {direction} | R$ {pnl:+.2f}\n"
            f"• SL atingido no servidor"
        )

        del state.positions[key]
        state.save()  # persistir após fechamento
        return


def close_all_and_report():
    """Fecha todas posições e gera relatório diário."""
    log("=== FECHANDO TUDO 16:45 ===")

    for key, pos in list(state.positions.items()):
        parts = key.rsplit("_", 1)
        symbol = parts[0]
        tf = parts[1] if len(parts) > 1 else "M5"

        result = safe_close(symbol)
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
    # Importar deals reais do MT5 e sincronizar taxas
    try:
        hist_result = _run_wine(EXECUTOR_WIN, "history")
        if isinstance(hist_result, dict) and "history" in hist_result:
            n_imported = import_mt5_history(hist_result["history"])
            log(f"MT5 history: {n_imported} deals importados")
            # Sync fees/swap reais do MT5 para os trades do dia
            n_synced = sync_fees_from_mt5()
            log(f"Fees sync: {n_synced} trades atualizados com taxas reais")
        else:
            log(f"MT5 history: resposta inválida: {hist_result}")
    except Exception as e:
        log(f"[WARN] import_mt5_history falhou: {e}")

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
        symbol_root = "WIN" if "WIN" in symbol else "WDO" if "WDO" in symbol else \
                         "BIT" if "BIT" in symbol else "DOL" if "DOL" in symbol else \
                         "IND" if "IND" in symbol else "WSP" if "WSP" in symbol else None
        if symbol_root not in CONFIG["symbols"]:
            continue

        ticket = str(p.get("ticket", ""))
        comment = p.get("comment", "")
        if comment != "VibeTrading":
            continue

        already_managed = any(str(v.get("entry_ticket")) == ticket for v in state.positions.values())
        if already_managed:
            continue

        db_trade = open_in_db.get(ticket) or (open_in_db.get(int(ticket)) if str(ticket).isdigit() else None)
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
            direction = "BUY" if p["type"] in (0, "BUY") else "SELL"
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

        # Estimar bar_count real a partir do tempo de abertura da posição
        # (999 causava max_pos_min imediato → fechava posições recuperadas injustamente)
        check_interval = CONFIG.get("check_interval", 30)
        _entry_ts = None
        if db_trade and db_trade.get("entry_time"):
            try:
                _dt = datetime.strptime(db_trade["entry_time"], "%Y-%m-%d %H:%M:%S")
                _entry_ts = _dt.timestamp()
            except Exception:
                pass
        if _entry_ts is None and p.get("time"):
            _entry_ts = float(p["time"])
        if _entry_ts:
            _age_min = max(0, (datetime.now().timestamp() - _entry_ts) / 60)
            _est_bar_count = int(_age_min / (check_interval / 60))
        else:
            _est_bar_count = 1  # fallback conservador

        state.positions[f"{symbol}_{tf}"] = {
            "direction": direction,
            "entry_price": entry_price,
            "entry_ticket": ticket,
            "sl_pts": sl_pts,
            "atr": atr,
            "trail_on": trail_on,
            "best_price": best,
            "bar_count": _est_bar_count,
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

    # ─── Verificação de dia útil + feriados ───
    ok, motivo = is_trading_day()
    if not ok:
        log(f"⛔ Hoje NÃO é dia de trading: {motivo}")
        notify_telegram(f"⛔ *Mercado fechado hoje*\n📋 Motivo: {motivo}\n💤 Bot aguardando próximo dia útil...")
    else:
        log(f"✅ Hoje é dia de trading ({motivo})")

    # ─── Auto-resolução de vencimento de contratos ───
    log("📅 Verificando vencimentos dos contratos...")
    resolved = resolve_all_symbols()
    CONFIG = load_config()  # Recarregar com eventuais atualizações
    for root, contract in resolved.items():
        _, month, year = _parse_contract_code(contract)
        if month:
            expiry = get_contract_expiry(root, month, year)
            days = 0
            check = date.today()
            while check < expiry:
                if is_trading_day(check)[0]:
                    days += 1
                check += timedelta(days=1)
            log(f"  {root} → {contract} (vence {expiry.strftime('%d/%m/%Y')}, {days} dias úteis)")
        else:
            log(f"  {root} → {contract}")

    # Log das estratégias
    strat_info = []
    for sym, strat in CONFIG["strategy"].items():
        strat_info.append(f"{sym}={strat}")
    strat_str = " | ".join(strat_info)

    log("=" * 60)
    log("Vibe-Trading Autotrader SPLIT INICIADO")
    log(f"Símbolos: {CONFIG['symbols']}")
    log(f"Estratégias: {strat_str}")
    for _s in CONFIG["symbols"]:
        _p = CONFIG.get(_s.lower(), {})
        log(f"{_s}: SL {_p.get('sl_atr_mult', 1.5)}x ATR | Trail {_p.get('trail_activate', 1.5)}x/{_p.get('trail_distance', 0.5)}x ATR")
    log(f"WDO: Cooldown({CONFIG['wdo']['cooldown_seconds']}s) | Max({CONFIG['wdo']['max_daily_trades']}/dia)")
    log(f"WIN: Cooldown({CONFIG['win']['cooldown_seconds']}s) | Max({CONFIG['win']['max_daily_trades']}/dia)")
    log(f"Volume: {CONFIG['volume']} contrato(s)")
    log("=" * 60)

    _syms = ", ".join(CONFIG["symbols"])
    notify_telegram(
        f"🚀 *Vibe-Trading Autotrader*\n"
        f"⏰ {datetime.now().strftime('%d/%m/%Y %H:%M')}\n"
        f"📊 {strat_str}\n"
        f"🎯 Ativos: {_syms}\n"
        f"⏱️ Timeframes: {', '.join(CONFIG.get('timeframes', []))}"
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
