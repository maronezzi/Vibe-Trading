"""
backtest_v944.py — Backtest customizado que usa os plugins reais de strategies/.

Replica a lógica do autotrader (mesmo vt_strategy_loader) com gestão de posição
completa: SL / Trailing / Breakeven / Time-trail / Max-position / 16:45.

Lê vt_config.json, busca candles M5/M15/M30/H1 dos últimos 30d via Wine+mt5_fetch,
roda as combinações ativas (symbols × timeframes × strategy_by_tf) e reporta.

Uso:
    PYTHONPATH=. python backtest/backtest_v944.py
"""
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ─── Paths ──────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.vt_strategy_loader import load_strategies, get_strategy_func  # noqa: E402

# ─── MT5 fetch ──────────────────────────────────────────────────────────────
WINE_PYTHON = os.path.expanduser("~/.wine/drive_c/Python311/python.exe")
FETCH_SCRIPT = ROOT / "backtest" / "mt5_fetch.py"

# W872 (2026-07-06): mult calibrado por broker-truth MT5. Antes WIN=0.20/WDO=10.0
# estavam errados e faziam a simulação subestimar perdas WIN em 5x (gap sim↔real).
# Prova: WIN 400pts × 1.0 = R$400 = broker truth confirmado em 25 trades.
# W872.2 (2026-07-07): slip_r recalibrado = slippage_pts × mult (1-2 ticks).
# Antes slip_r era R$5-10 fixo — absurdo quando mult baixo (WDO break-even 4133pts).
# Agora slip_r escala com mult, refletindo slippage real (empírico ≈ R$0).
#
# Wave custo-real (Bruno 01/08): WDO e WSP estavam com mult COPIADO/ERRADO,
# matando todo backtest com PF=0 (custo fixo R$1.20 superava lucro micro-penny).
# Calibrado pelas especificações OFICIAIS B3:
#   WDO (Mini Dólar): cada ponto = R$10.00, tick 0.5pt, 1 tick = R$5.00.
#     Fonte: B3/Bora Investir/CM Capital — "cada ponto vale R$10".
#     ANTES mult=0.0015 (erro 6667x): 8pts move = R$0.012, custo R$1.20 → PF=0.
#   WSP (Micro S&P 500): mult=2.5 R$/ponto (NÃO é R$13.50 nem R$0.01).
#     Fonte: broker-truth MT5 symbol_info WSPU26 (Bruno 11/08/2026) —
#     trade_tick_value=0.625 BRL / trade_tick_size=0.25 = 2.5 R$/ponto.
#     (WSP$ perpétuo bate no mesmo 2.5: tick_value=0.025/tick_size=0.01.)
#     ANTES mult=0.01 (cópia do BIT, erro 250x): break-even exigia 700pts
#     favoráveis só p/ cobrar fee R$7 → toda trade WSP dava PF=0 no backtest,
#     o AGI nunca aprovava estratégia WSP (8 pares failing perpetuamente).
#     O palpite anterior "USD 2.50/pt ≈ R$13.50" tb estava errado — na real
#     é ~USD 0.50/pt (Micro, não full S&P). Valor confirmado, não hardcode.
# Wave 880.B-AGI (Bruno 2026-08-05): mult alinhado com vt_config.json
# contract_specs (a config real é a verdade). Antes o backtest usava WIN
# mult=1.0 mas a real é mult=0.2 — superestimava o PnL do WIN em 5×, o ativo
# que mais operou (18/22 trades em 05/08). O AGI via "lucro" onde a real perdia.
# fee_r (R$) = custo total por trade (corretagem + emolumento + B3). Calibrado
# pelo gap real de 05/08: -R$400 (DB) vs -R$552,88 (broker) em 22 trades ≈
# R$7/trade de custo não-itemizado. Antes era hardcoded 1.20 (subestimado).
# slip_r mantido (2 ticks conservador).
CONTRACT_SPECS = {
    "WIN$": {"mult": 0.2,  "tick": 5,    "slip_r": 5.0,    "fee_r": 7.0},
    "WDO$": {"mult": 10.0, "tick": 0.5,  "slip_r": 10.0,   "fee_r": 7.0},
    "BIT$": {"mult": 0.01, "tick": 0.01, "slip_r": 0.0002, "fee_r": 7.0},
    "WSP$": {"mult": 2.5,  "tick": 0.01, "slip_r": 1.25,  "fee_r": 7.0},
    "DOL$": {"mult": 1.0,  "tick": 0.5,  "slip_r": 0.0018, "fee_r": 7.0},
    "IND$": {"mult": 1.0,  "tick": 5,    "slip_r": 5.0,    "fee_r": 7.0},
}

# ─── Cópia local das calculate_* (mesmo comportamento que core/vt_autotrader) ─
# Porque o plugin recebe um dict de utils e aqui vamos passar funções que
# operam sobre uma lista newest-first de candles.


def _bars_newest_first(df: pd.DataFrame) -> list:
    """Converte DataFrame (cronológico) em lista newest-first de candles."""
    out = []
    for _, r in df.iloc[::-1].iterrows():
        out.append({
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": float(r.get("tick_volume", 0) or 0),
        })
    return out


def calculate_atr(bars, period=14):
    if not bars or len(bars) < period + 1:
        return 0
    data = bars[: period + 1]
    s = 0
    for i in range(period):
        h, l, c_prev = data[i]["high"], data[i]["low"], data[i + 1]["close"]
        s += max(h - l, abs(h - c_prev), abs(l - c_prev))
    return s / period


def calculate_ema(bars, period):
    if not bars or len(bars) < period:
        return 0
    chrono = list(reversed(bars))
    seed = sum(b["close"] for b in chrono[:period]) / period
    ema = seed
    mult = 2 / (period + 1)
    for b in chrono[period:]:
        ema = b["close"] * mult + ema * (1 - mult)
    return ema


def calculate_rsi(bars, period=14):
    if not bars or len(bars) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(min(period, len(bars) - 1)):
        d = bars[i]["close"] - bars[i + 1]["close"]
        if d > 0:
            gains.append(d)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(d))
    n = max(len(gains), 1)
    avg_g = sum(gains) / n if gains else 0
    avg_l = sum(losses) / n if losses else 0.001
    rs = avg_g / avg_l if avg_l > 0 else 100
    return 100 - (100 / (1 + rs))


def calculate_adx(bars, period=14):
    if not bars or len(bars) < period * 2:
        return 0, 0, 0
    chrono = list(reversed(bars[: period * 2]))
    highs = [b["high"] for b in chrono]
    lows = [b["low"] for b in chrono]
    closes = [b["close"] for b in chrono]
    plus_dm, minus_dm = [], []
    for i in range(1, len(highs)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)
    tr_list = []
    for i in range(1, len(highs)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr_list.append(tr)
    if len(tr_list) < period:
        return 0, 0, 0
    atr_v = sum(tr_list[:period]) / period
    p_dm = sum(plus_dm[:period]) / period
    m_dm = sum(minus_dm[:period]) / period
    for i in range(period, len(tr_list)):
        atr_v = (atr_v * (period - 1) + tr_list[i]) / period
        p_dm = (p_dm * (period - 1) + plus_dm[i]) / period
        m_dm = (m_dm * (period - 1) + minus_dm[i]) / period
    if atr_v == 0:
        return 0, 0, 0
    p_di = 100 * p_dm / atr_v
    m_di = 100 * m_dm / atr_v
    s = p_di + m_di
    dx = 100 * abs(p_di - m_di) / s if s > 0 else 0
    return dx, p_di, m_di


def calculate_bollinger(bars, period=20, num_std=2.0):
    if not bars or len(bars) < period:
        return 0, 0, 0
    closes = [b["close"] for b in bars[:period]]
    mid = sum(closes) / period
    var = sum((c - mid) ** 2 for c in closes) / period
    std = var ** 0.5
    return mid + num_std * std, mid, mid - num_std * std


def calculate_vwap(bars, period=20):
    if not bars or len(bars) < period:
        return 0
    data = bars[:period]
    spv, sv = 0, 0
    for b in data:
        tp = (b["high"] + b["low"] + b["close"]) / 3
        v = max(b["volume"], 1)
        spv += tp * v
        sv += v
    return spv / sv if sv > 0 else 0


def calc_sl(symbol, atr, params):
    """Replica _calc_sl do autotrader."""
    if not isinstance(params, dict):
        return int(atr * 1.0)
    return int(atr * params.get("sl_atr_mult", 1.0))


# ─── Fetch helpers ──────────────────────────────────────────────────────────

def fetch(symbol, tf, n_bars, cache_dir="/tmp"):
    cache = Path(cache_dir) / f"{symbol}_{tf}_{n_bars}.csv"
    if cache.exists() and (datetime.now() - datetime.fromtimestamp(cache.stat().st_mtime)).seconds < 600:
        return cache
    cmd = ["wine", WINE_PYTHON, str(FETCH_SCRIPT), "rates", symbol, tf, str(n_bars)]
    env = {**os.environ, "WINEDEBUG": "-all"}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    except Exception as e:
        print(f"  ❌ fetch {symbol} {tf} falhou: {e}")
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    cache.write_text(r.stdout)
    return cache


def load_csv(path):
    if not path or not Path(path).exists():
        return None
    df = pd.read_csv(path)
    if df.empty:
        return None
    for c in ["open", "high", "low", "close", "tick_volume", "real_volume"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["time"] = pd.to_datetime(df["time"].astype(int), unit="s")
    df = df.set_index("time").sort_index()
    df["hour"] = df.index.hour
    df["minute"] = df.index.minute
    df["date"] = df.index.date
    return df.dropna(subset=["close"])


# ─── Backtest de uma combinação ────────────────────────────────────────────

class TradesList(list):
    """Lista de trades com metadados de telemetria do backtest.

    Wave fix-contract (Bruno 01/08): expõe plugin_errors/plugin_first_error
    para o evaluator distinguir "0 trades por bug de código" de "0 trades por
    falta de edge". Comporta-se como list em tudo (iterável, indexável).
    """

    plugin_errors = 0
    plugin_first_error = None


def backtest_combo(df, sym_root, tf, strategy_name, params, *, debug=False):
    """Roda backtest de 1 (symbol, tf) e retorna lista de trades."""
    symbol = f"{sym_root}$"
    spec = CONTRACT_SPECS[symbol]
    mult = spec["mult"]
    slip_r = spec["slip_r"]
    # Wave 880.B-AGI: fee_r (R$ de custo por trade). Default 1.20 (histórico)
    # se ausente; calibrado para 7.0 no CONTRACT_SPECS acima (gap real 05/08).
    fee_r = spec.get("fee_r", 1.20)
    is_wdo = sym_root == "WDO"
    is_win = sym_root == "WIN"
    is_bit = sym_root == "BIT"
    # Wave 880.B-AGI-PARIDADE: stop_level simulado (pontos de preço) para o
    # backtest respeitar o mesmo constraint da conta real. Antes, o backtest
    # aceitava SLs a 1 tick do entry (breakeven/profit-lock), que a real
    # rejeita ("Invalid stops" ×255 em 05/08). Agora, se um SL apertado cai
    # dentro do stop_level, o backtest NÃO o aplica (mantém SL anterior) —
    # fiel ao comportamento real. Valores da literatura/XP (confirmar c/ XP).
    _STOP_LEVEL = {"WIN": 300.0, "WDO": 200.0, "BIT": 500.0, "WSP": 200.0,
                   "IND": 300.0, "DOL": 200.0}
    sim_stops_level = _STOP_LEVEL.get(sym_root, 0.0)

    # Escala para SL min
    sl_min = 100 if is_win else (200 if is_wdo else (50000 if is_bit else 100))
    tick_size = spec["tick"]

    sl_atr_mult = params.get("sl_atr_mult", 1.0)
    trail_activate = params.get("trail_activate", 1.5)
    trail_distance = params.get("trail_distance", 0.5)
    cooldown = params.get("cooldown_seconds", 600)
    max_daily = params.get("max_daily_trades", 999)
    breakeven_min = params.get("breakeven_minutes", 0)
    time_trail_min = params.get("time_trail_minutes", 0)
    max_pos_min = params.get("max_position_minutes", 999)
    hard_exit_min = params.get("hard_exit_minutes", 999)
    # Wave Melhoria 2 (Bruno 12/07): profit-lock por R — quando lucro atinge
    # profit_lock_r × risco inicial (1R = distância do SL), move SL pro entry
    # (zero-loss). Default 0.0 = desligado (AGI otimiza o valor ótimo).
    profit_lock_r = params.get("profit_lock_r", 0.0)
    # Wave Melhoria 1 (Bruno 12/07): circuit breaker per-(sym,tf) — após
    # max_consecutive_losses seguidas, pausa halt_duration_minutes no slot.
    # Defaults conservadores (999/60) = efetivamente desligado até o AGI afinar.
    max_consec = params.get("max_consecutive_losses", 999)
    halt_min = params.get("halt_duration_minutes", 60)

    # Carrega plugin real
    func = get_strategy_func(strategy_name)
    if not func:
        return []

    utils = {
        "calculate_ema": calculate_ema,
        "calculate_rsi": calculate_rsi,
        "calculate_adx": calculate_adx,
        "calculate_bollinger": calculate_bollinger,
        "calculate_vwap": calculate_vwap,
        "calculate_atr": calculate_atr,
        "calc_sl": calc_sl,
    }

    tf_minutes = {"M5": 5, "M15": 15, "M30": 30, "H1": 60}[tf]

    pos = 0
    ep = 0.0
    e_dt = None
    e_idx = 0
    e_atr = 0.0
    best = 0.0
    sl_price = 0.0
    sl_pts = 0
    trail_on = False
    be_done = False
    bars_in_trade = 0
    last_trade_dt = None
    daily_count = defaultdict(int)
    trades = TradesList()
    # Wave fix-contract (Bruno 01/08): contabiliza exceções do plugin check_entry.
    # Antes, um TypeError (ex: indexar float de calculate_rsi) era engolido em
    # silêncio pelo except abaixo e a combinação aparecia como "0 trades / sem
    # edge" — confundindo bug de código com falta de alpha. Agora contamos e
    # guardamos a 1ª exceção real para o diagnóstico subir no resultado.
    plugin_errors = 0
    plugin_first_error = None
    # Wave Melhoria 1: estado do circuit breaker (per-(sym,tf) dentro deste combo).
    consec_losses = 0          # contador de perdas consecutivas
    halt_until_dt = None       # datetime até o qual novas entradas estão bloqueadas
    # Wave Melhoria 2: flag de profit-lock aplicado (reseta a cada trade).
    # Reusamos be_done como flag one-shot do profit-lock também — BE temporal e
    # profit-lock são mutuamente exclusivos (o que disparar primeiro sela o SL).
    # Wave 880.B4: TP ladder state (reseta a cada trade em _close).
    remaining = 1.0     # fração da posição ainda aberta (1.0 = nada fechado)
    tp1_done = False
    tp2_done = False

    def _close(price, dt, reason):
        nonlocal pos, ep, e_dt, e_idx, e_atr, best, sl_price, sl_pts, trail_on, be_done, bars_in_trade
        nonlocal consec_losses, halt_until_dt, remaining, tp1_done, tp2_done
        if pos == 0:
            return None
        # Wave 880.B4: PnL final escala por `remaining` (fração ainda aberta
        # após TP1/TP2). Se remaining < 1.0, parte já foi realizada como
        # trade separado no bloco TP1/TP2 acima.
        if pos == 1:
            pnl = ((price - ep) * mult - slip_r - fee_r) * remaining
        else:
            pnl = ((ep - price) * mult - slip_r - fee_r) * remaining
        trades.append({
            "side": "BUY" if pos == 1 else "SELL",
            "entry_dt": e_dt,
            "exit_dt": dt,
            "ep": ep,
            "xp": price,
            "pnl": pnl,
            "reason": reason,
            "sl_pts": sl_pts,
            "strategy": strategy_name,
            "sym": sym_root,
            "tf": tf,
        })
        # Wave Melhoria 1: bookkeeping do circuit breaker.
        # Loss incrementa o contador; se atingir threshold, ativa halt.
        # Win reseta o contador. Robusta a todos os exits (SL/HARD_EXIT/1645/FORCE).
        if pnl < 0:
            consec_losses += 1
            if consec_losses >= max_consec:
                halt_until_dt = dt + timedelta(minutes=halt_min)
        elif pnl > 0:
            consec_losses = 0
        pos = 0
        ep = 0.0
        e_dt = None
        best = 0.0
        sl_price = 0.0
        sl_pts = 0
        trail_on = False
        be_done = False
        bars_in_trade = 0
        # Wave 880.B4: reset TP ladder state
        remaining = 1.0
        tp1_done = False
        tp2_done = False
        return pnl

    # Pré-calcula janela de candles (rolling)
    # bars_list[i] = lista newest-first no momento i (precisa de pelo menos 50 candles para EMA 30)
    min_window = 60
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    volumes = df["tick_volume"].values
    hours = df["hour"].values
    minutes = df["minute"].values
    dates = df["date"].values
    n = len(df)

    for i in range(n):
        dt = df.index[i]
        price = closes[i]
        high = highs[i]
        low = lows[i]
        hour = int(hours[i])
        minute = int(minutes[i])

        # Janela newest-first
        win_start = max(0, i - min_window)
        bars_nf = []
        for j in range(i, win_start - 1, -1):
            bars_nf.append({
                "high": highs[j],
                "low": lows[j],
                "close": closes[j],
                "volume": volumes[j],
            })

        cur_atr = calculate_atr(bars_nf, 14)
        cur_date = dates[i]

        # Horário de pregão 9:05–16:45
        if hour < 9 or (hour == 9 and minute < 5):
            continue
        if hour > 16 or (hour == 16 and minute >= 45):
            # Fecha posições
            if pos != 0:
                _close(price, dt, "1645")
            continue

        # ─── Gestão de posição ───
        if pos != 0:
            bars_in_trade += 1
            pos_min = bars_in_trade * tf_minutes

            if pos == 1:
                best = max(best, high)
            else:
                best = min(best, low) if best > 0 else low

            profit_pts = (best - ep) if pos == 1 else (ep - best)

            # ═══ Wave 880.B4 (2026-07-19): TP1 + TP2 ladder — port do live ═══
            # Antes o backtest só simulava trailing; agora modela parcial-close
            # igual ao autotrader (vt_autotrader.py:2515-2630). Resolve a
            # divergência live↔backtest: AGI agora otimiza contra um backtest
            # fiel ao comportamento real.
            tp1_r = params.get("tp1_r", 1.0)
            tp1_pct = params.get("tp1_pct", 0.5)
            tp2_r = params.get("tp2_r", 2.0)
            tp2_pct = params.get("tp2_pct", 0.5)
            # tp_done[1]=TP1, tp_done[2]=TP2; remaining = fração da posição ainda aberta
            if not tp1_done and e_atr > 0 and profit_pts >= tp1_r * e_atr and remaining > 0 and 0 < tp1_pct < 1:
                close_frac = min(tp1_pct, remaining)
                # Registra PnL parcial (fechado a mercado no melhor preço)
                partial_pnl = close_frac * profit_pts * mult - slip_r * close_frac - fee_r * close_frac
                trades.append({
                    "side": "BUY" if pos == 1 else "SELL",
                    "entry_dt": e_dt, "exit_dt": dt,
                    "ep": ep, "xp": best,
                    "pnl": partial_pnl,
                    "reason": "TP1",
                    "sl_pts": sl_pts, "strategy": strategy_name,
                    "sym": sym_root, "tf": tf,
                })
                remaining -= close_frac
                tp1_done = True
                # Wave 880.B1: tighter trail pós-TP1 (atr_trail_mult se setado)
                if params.get("atr_trail_mult") is not None:
                    trail_distance = params.get("atr_trail_mult", trail_distance)
            if tp1_done and not tp2_done and e_atr > 0 and profit_pts >= tp2_r * e_atr and remaining > 0 and 0 < tp2_pct < 1:
                close_frac = min(tp2_pct, remaining)
                partial_pnl = close_frac * profit_pts * mult - slip_r * close_frac - fee_r * close_frac
                trades.append({
                    "side": "BUY" if pos == 1 else "SELL",
                    "entry_dt": e_dt, "exit_dt": dt,
                    "ep": ep, "xp": best,
                    "pnl": partial_pnl,
                    "reason": "TP2",
                    "sl_pts": sl_pts, "strategy": strategy_name,
                    "sym": sym_root, "tf": tf,
                })
                remaining -= close_frac
                tp2_done = True

            # Trailing
            if not trail_on and e_atr > 0 and profit_pts >= trail_activate * e_atr:
                trail_on = True

            # Wave Melhoria 2: profit-lock por R.
            # Quando lucro atinge profit_lock_r × risco inicial, move SL pro
            # entry (zero-loss). Usa sl_pts inicial (distância absoluta)
            # como 1R. be_done evita re-disparar; BE temporal abaixo também
            # reusa be_done (mutuamente exclusivos — quem disparar primeiro sela).
            # Wave 880.B-AGI-PARIDADE: respeita stop_level do broker. Replica o
            # live (vt_autotrader.py PROFIT_LOCK): o lock NÃO é 1 tick do entry
            # (sempre rejeitado "Invalid stops"), mas sim a distância segura
            # stops_level×1.1+1 acima do entry — igual ao cmd_modify real.
            if not trail_on and not be_done and profit_lock_r > 0 and e_atr > 0 and sl_pts > 0:
                if profit_pts >= profit_lock_r * sl_pts:
                    if sim_stops_level > 0:
                        _min_lock_pts = max(int(sim_stops_level * 1.1) + 1, tick_size)
                    else:
                        _min_lock_pts = tick_size
                    _lock_sl = ep + _min_lock_pts if pos == 1 else ep - _min_lock_pts
                    # Replica o gate do live: se o lock ainda cai dentro do
                    # stop_level (não deveria, mas degradado), mantém SL anterior.
                    _lock_dist = abs(ep - _lock_sl)
                    if sim_stops_level <= 0 or _lock_dist >= sim_stops_level:
                        be_done = True
                        sl_price = _lock_sl

            # Breakeven (temporal — dispara só se profit-lock ainda não selou)
            # Wave 880.B-AGI-PARIDADE: mesmo gate de stop_level.
            if not trail_on and not be_done and breakeven_min > 0 and pos_min >= breakeven_min and e_atr > 0:
                _be_sl = ep + tick_size if pos == 1 else ep - tick_size
                _be_dist = abs(ep - _be_sl)
                if sim_stops_level <= 0 or _be_dist >= sim_stops_level:
                    be_done = True
                    sl_price = _be_sl

            # Time-trail
            if not trail_on and time_trail_min > 0 and pos_min >= time_trail_min and profit_pts > 0:
                trail_on = True

            # Trailing stop update
            # Wave 880.B-AGI-PARIDADE: trailing também respeita stop_level.
            # Se o new_sl apertado fica dentro do stop_level do preço atual, não
            # aplica (mantém sl_price anterior) — fiel à real.
            if trail_on and e_atr > 0:
                if pos_min >= max_pos_min:
                    dist = 0.3 * e_atr
                else:
                    dist = trail_distance * e_atr
                if pos == 1:
                    new_sl = best - dist
                    _trail_dist = price - new_sl  # dist do SL ao preço atual
                    if new_sl > sl_price and (sim_stops_level <= 0 or _trail_dist >= sim_stops_level):
                        sl_price = new_sl
                else:
                    new_sl = best + dist
                    _trail_dist = new_sl - price
                    if new_sl < sl_price and (sim_stops_level <= 0 or _trail_dist >= sim_stops_level):
                        sl_price = new_sl

            # Hard exit
            if hard_exit_min < 900 and pos_min >= hard_exit_min and profit_pts > 0:
                _close(price, dt, "HARD_EXIT")
                continue

            # SL check
            if sl_price > 0:
                if pos == 1 and low <= sl_price:
                    _close(sl_price, dt, "SL")
                    continue
                if pos == -1 and high >= sl_price:
                    _close(sl_price, dt, "SL")
                    continue

            continue

        # ─── Verifica entrada ───
        if cur_atr <= 0 or len(bars_nf) < 30:
            continue

        # Wave Melhoria 1: circuit breaker — se o slot está em halt, não entra.
        # Robusto a None (sem halt ainda). Reseta naturalmente quando dt passa.
        if halt_until_dt is not None and dt < halt_until_dt:
            continue

        # Cooldown
        if last_trade_dt is not None:
            if (dt - last_trade_dt).total_seconds() < cooldown:
                continue

        # Daily limit
        if daily_count[cur_date] >= max_daily:
            continue

        try:
            sig = func(symbol, tf, price, cur_atr, dt, bars_nf, params, utils)
        except Exception as ex:
            # Wave fix-contract (01/08): registra a exceção real do plugin.
            # Se n_trades=0 E plugin_errors>0, o problema é BUG DE CÓDIGO no
            # check_entry (não falta de edge). A telemetria vai na instância
            # trades (TradesList) para o evaluator ler.
            plugin_errors += 1
            if plugin_first_error is None:
                plugin_first_error = f"{type(ex).__name__}: {ex}"
            if debug and i < 5:
                import traceback
                traceback.print_exc()
            if debug:
                print(f"  ⚠️  plugin {strategy_name} erro: {ex}")
            continue

        if not sig or "direction" not in sig:
            if debug and i % 200 == 0:
                print(f"    [{dt}] no signal (atr={cur_atr:.1f} cooldown_ok={last_trade_dt is None or (dt - last_trade_dt).total_seconds() >= cooldown} daily={daily_count[cur_date]}/{max_daily})")
            continue

        direction = sig["direction"]
        # SL plugin
        raw_sl = int(sig.get("sl_pts", cur_atr * sl_atr_mult))
        if raw_sl < sl_min:
            raw_sl = sl_min
        # Arredonda múltiplo de 5 (WIN) ou 50 (WDO), mas o backtest mantém exato
        if raw_sl <= 0:
            continue

        pos = 1 if direction == "BUY" else -1
        ep = price
        e_dt = dt
        e_idx = i
        e_atr = cur_atr
        best = price
        sl_pts = raw_sl
        sl_price = ep - sl_pts if pos == 1 else ep + sl_pts
        trail_on = False
        be_done = False
        bars_in_trade = 0
        last_trade_dt = dt
        daily_count[cur_date] += 1
        # Wave 880.B4: TP ladder state reset na entrada
        remaining = 1.0
        tp1_done = False
        tp2_done = False

    # Force close no fim
    if pos != 0:
        _close(float(closes[-1]), df.index[-1], "FORCE")

    # Wave fix-contract (01/08): anexa telemetria de erros do plugin na lista
    # retornada, para o evaluator distinguir bug de código de falta de edge.
    trades.plugin_errors = plugin_errors
    trades.plugin_first_error = plugin_first_error
    return trades


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    config_path = ROOT / "vt_config.json"
    with open(config_path) as f:
        config = json.load(f)

    version = config.get("_version", "?")
    print("═" * 80)
    print(f"  🧪 BACKTEST v944 — config v{version} (30 dias, plugins reais)")
    print("  " + "─" * 76)

    # Carrega plugins
    load_strategies()
    print(f"  Estratégias carregadas: {len(load_strategies())}")

    # Combinações ativas
    symbols = config.get("symbols", [])
    disabled_syms = set(config.get("disabled_symbols", []))
    disabled_tfs = set(config.get("disabled_timeframes", []))
    tfs_by_sym = config.get("timeframes_by_symbol", {})
    strat_by_tf = config.get("strategy_by_tf", {})

    combos = []
    for sym in symbols:
        if sym in disabled_syms:
            continue
        for tf in tfs_by_sym.get(sym, []):
            if tf in disabled_tfs:
                continue
            key = f"{sym}_{tf}"
            strat = strat_by_tf.get(key, config.get("strategy", {}).get(sym, "VWAP"))
            combos.append((sym, tf, strat))

    print(f"  Combinações ativas: {len(combos)}")
    for s, t, st in combos:
        print(f"    {s}_{t} → {st}")
    print("═" * 80)

    # Resolve params (igual ao autotrader)
    def get_params(sym, tf):
        base = config.get(sym.lower(), {})
        by_tf = config.get("params_by_tf", {}).get(f"{sym}_{tf}", {})
        merged = {**base, **by_tf}
        merged.pop("strategy", None)
        merged.pop("buy_enabled", None)
        return merged

    all_trades = []
    for sym, tf, strat in combos:
        # Fetch (cache de 30d)
        n_bars = {"M5": 2500, "M15": 900, "M30": 500, "H1": 260}[tf]
        path = fetch(f"{sym}$", tf, n_bars)
        if not path:
            print(f"  ❌ {sym} {tf}: sem dados")
            continue
        df = load_csv(path)
        if df is None or len(df) < 50:
            print(f"  ❌ {sym} {tf}: dados insuficientes")
            continue

        params = get_params(sym, tf)
        trades = backtest_combo(df, sym, tf, strat, params)
        all_trades.extend(trades)
        print(f"  {sym}_{tf} ({strat}): {len(trades)} trades, "
              f"R$ {sum(t['pnl'] for t in trades):+.2f}")

    # ─── Relatório ─────────────────────────────────────────────────────────
    print()
    print("═" * 80)
    print("  📊 RELATÓRIO FINAL (30 dias)")
    print("  " + "─" * 76)

    if not all_trades:
        print("  ❌ Nenhum trade gerado")
        return

    # Por dia
    by_day = defaultdict(list)
    for t in all_trades:
        d = t["entry_dt"].date() if isinstance(t["entry_dt"], datetime) else t["entry_dt"]
        by_day[d].append(t)

    days = sorted(by_day.keys())
    n_days = max(1, (max(days) - min(days)).days + 1)
    n_trades = len(all_trades)
    total_pnl = sum(t["pnl"] for t in all_trades)
    wins = [t for t in all_trades if t["pnl"] > 0]
    losses = [t for t in all_trades if t["pnl"] < 0]
    wr = len(wins) / n_trades * 100 if n_trades else 0
    avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0
    avg_loss = np.mean([t["pnl"] for t in losses]) if losses else 0
    avg_trade = total_pnl / n_trades if n_trades else 0

    trades_per_day = n_trades / n_days
    pnl_per_day = total_pnl / n_days

    print(f"  Período: {min(days)} → {max(days)} ({n_days} dias)")
    print(f"  Trades totais: {n_trades}    Trades/dia: {trades_per_day:.1f}")
    print(f"  PnL total:    R$ {total_pnl:+,.2f}    PnL/dia: R$ {pnl_per_day:+,.2f}")
    print(f"  PnL/trade:    R$ {avg_trade:+,.2f}")
    print(f"  Win rate:     {wr:.1f}% ({len(wins)}W / {len(losses)}L)")
    print(f"  Avg win:      R$ {avg_win:+,.2f}    Avg loss: R$ {avg_loss:+,.2f}")
    if avg_loss != 0:
        print(f"  Payoff ratio: {abs(avg_win/avg_loss):.2f}")

    print()
    print(f"  {'DIA':<12} {'TRADES':>7} {'PNL':>11} {'WR':>6}")
    for d in days:
        day_trades = by_day[d]
        n = len(day_trades)
        p = sum(t["pnl"] for t in day_trades)
        w = sum(1 for t in day_trades if t["pnl"] > 0)
        wr_d = w / n * 100 if n else 0
        print(f"  {str(d):<12} {n:>7} {p:>+11,.2f} {wr_d:>5.1f}%")

    # Por estratégia
    print()
    print("  Por estratégia:")
    by_strat = defaultdict(list)
    for t in all_trades:
        by_strat[t["strategy"]].append(t)
    for s, lst in sorted(by_strat.items(), key=lambda x: -sum(t["pnl"] for t in x[1])):
        n = len(lst)
        p = sum(t["pnl"] for t in lst)
        w = sum(1 for t in lst if t["pnl"] > 0)
        print(f"    {s:<25} {n:>4} trades  R$ {p:+10,.2f}  WR {w/n*100:.1f}%")

    # Por símbolo+TF
    print()
    print("  Por símbolo_TF:")
    by_combo = defaultdict(list)
    for t in all_trades:
        by_combo[f"{t['sym']}_{t['tf']}"].append(t)
    for k, lst in sorted(by_combo.items(), key=lambda x: -sum(t["pnl"] for t in x[1])):
        n = len(lst)
        p = sum(t["pnl"] for t in lst)
        w = sum(1 for t in lst if t["pnl"] > 0)
        print(f"    {k:<10} {n:>4} trades  R$ {p:+10,.2f}  WR {w/n*100:.1f}%")

    print("═" * 80)

    return {
        "n_trades": n_trades,
        "trades_per_day": trades_per_day,
        "pnl_total": total_pnl,
        "pnl_per_day": pnl_per_day,
        "pnl_per_trade": avg_trade,
        "win_rate": wr,
        "n_days": n_days,
    }


if __name__ == "__main__":
    main()
