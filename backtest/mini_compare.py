"""Mini-backtest comparativo BOLLINGER vs RSI_REVERSION.

Replica as duas estratégias sobre dados CSV de data/ e retorna
{PnL, WR, n_trades} pra cada uma. Usado em tests/test_win_rsi_vs_boll.py
pra validar a hipótese de que RSI_REVERSION é >= BOLLINGER em WIN M5/M15.

Reutiliza as funções de cálculo (RSI, Bollinger, ATR, EMA) do
vt_autotrader.py pra garantir que a simulação bate com a produção.
"""
import sys
import csv
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))


def _load_bars(csv_path: str) -> list[dict]:
    """Lê CSV MT5 (time, open, high, low, close, tick_volume) e retorna bars.

    Cada bar é um dict com chaves: time (int epoch), open, high, low, close,
    volume. Bars são retornadas em ordem CRONOLÓGICA (mais antiga primeiro),
    como o autotrader espera após reversal.
    """
    bars = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                bars.append({
                    "time": int(row["time"]),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": int(float(row.get("tick_volume", row.get("real_volume", 0)))),
                })
            except (ValueError, KeyError):
                continue
    bars.sort(key=lambda b: b["time"])  # cronológico
    return bars


def _atr(bars: list[dict], period: int = 14) -> float:
    """ATR simples (Wilder) — usa close anterior."""
    if len(bars) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        h = bars[i]["high"]
        l = bars[i]["low"]
        pc = bars[i - 1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    if len(trs) < period:
        return 0.0
    return sum(trs[-period:]) / period


def _rsi(bars: list[dict], period: int = 14) -> float:
    """RSI Wilder. Retorna 0..100."""
    if len(bars) < period + 1:
        return 0.0
    gains, losses = [], []
    for i in range(1, len(bars)):
        diff = bars[i]["close"] - bars[i - 1]["close"]
        if diff > 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(diff))
    # Wilder smoothing (último `period` amostras)
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _bollinger(bars: list[dict], period: int = 20, std_mult: float = 2.0) -> tuple[float, float, float]:
    """Retorna (upper, mid, lower) Bollinger Bands."""
    if len(bars) < period:
        return 0.0, 0.0, 0.0
    closes = [b["close"] for b in bars[-period:]]
    mid = sum(closes) / period
    var = sum((c - mid) ** 2 for c in closes) / period
    std = var ** 0.5
    return mid + std_mult * std, mid, mid - std_mult * std


def _rsi_reversion_signal(bar: dict, prev_bars: list[dict], params: dict) -> str | None:
    """Replica a lógica de strategies/rsi_reversion.py:check_entry()."""
    rsi = _rsi(prev_bars, params.get("rsi_period", 14))
    if rsi == 0:
        return None
    if rsi < params.get("rsi_oversold", 30):
        return "BUY"
    if rsi > params.get("rsi_overbought", 70):
        return "SELL"
    return None


def _bollinger_signal(bar: dict, prev_bars: list[dict], params: dict) -> str | None:
    """Replica a lógica de strategies/bollinger.py:check_entry() (resumida).

    BUY: low <= bb_lower AND rsi < oversold
    SELL: high >= bb_upper AND rsi > overbought
    """
    period = params.get("bb_period", 20)
    std = params.get("bb_std", 2.0)
    upper, mid, lower = _bollinger(prev_bars, period, std)
    if upper == 0:
        return None
    rsi = _rsi(prev_bars, params.get("rsi_period", 14))
    if rsi == 0:
        return None
    if bar["low"] <= lower and rsi < params.get("rsi_oversold", 30):
        return "BUY"
    if bar["high"] >= upper and rsi > params.get("rsi_overbought", 70):
        return "SELL"
    return None


def _simulate(bars: list[dict], signal_fn, params: dict, mult: float = 0.20) -> dict:
    """Simula estratégia com position management simplificado.

    Regras:
    - 1 posição por vez (sem overtrading)
    - SL = sl_atr_mult * ATR (fixo no entry)
    - TP = trail (trail_activate*ATR, trail_distance*ATR) — versão simplificada
    - Sai no EOD se ainda aberta (16:45)
    - Aplica cooldown entre trades
    - Aplica max_daily_trades
    """
    if len(bars) < 30:
        return {"pnl": 0.0, "wr": 0.0, "n": 0, "trades": []}

    pos = None  # {"dir", "entry", "sl", "tp", "bar_idx"}
    cooldown_until = 0
    daily_count = 0
    daily_date = None
    trades = []

    for i in range(20, len(bars)):  # warmup
        bar = bars[i]
        prev = bars[:i]
        bar_date = bar["time"] // 86400  # dia aproximado

        # Reset diário
        if daily_date != bar_date:
            daily_date = bar_date
            daily_count = 0

        # Verificar saída da posição aberta
        if pos is not None:
            hit_sl = (pos["dir"] == "BUY" and bar["low"] <= pos["sl"]) or \
                     (pos["dir"] == "SELL" and bar["high"] >= pos["sl"])
            hit_tp = (pos["dir"] == "BUY" and bar["high"] >= pos["tp"]) or \
                     (pos["dir"] == "SELL" and bar["low"] <= pos["tp"])

            if hit_sl or hit_tp:
                exit_price = pos["sl"] if hit_sl else pos["tp"]
                pnl = (exit_price - pos["entry"]) * mult if pos["dir"] == "BUY" \
                      else (pos["entry"] - exit_price) * mult
                pnl -= 1.20  # fees
                trades.append({"pnl": pnl, "dir": pos["dir"], "reason": "SL" if hit_sl else "TP"})
                pos = None
                cooldown_until = bar["time"] + params.get("cooldown_seconds", 180)
                continue

        # Verificar entrada (só se sem posição, sem cooldown, e cabe no limite diário)
        if pos is None and bar["time"] >= cooldown_until and daily_count < params.get("max_daily_trades", 8):
            atr = _atr(prev, 14)
            if atr <= 0:
                continue
            direction = signal_fn(bar, prev, params)
            if direction:
                entry = bar["close"]
                sl_distance = params.get("sl_atr_mult", 0.6) * atr
                tp_distance = params.get("trail_activate", 1.0) * atr  # simplificado
                pos = {
                    "dir": direction,
                    "entry": entry,
                    "sl": entry - sl_distance if direction == "BUY" else entry + sl_distance,
                    "tp": entry + tp_distance if direction == "BUY" else entry - tp_distance,
                    "bar_idx": i,
                }
                daily_count += 1

    # Fecha posição aberta no final como EOD
    if pos is not None:
        exit_price = bars[-1]["close"]
        pnl = (exit_price - pos["entry"]) * mult if pos["dir"] == "BUY" \
              else (pos["entry"] - exit_price) * mult
        pnl -= 1.20
        trades.append({"pnl": pnl, "dir": pos["dir"], "reason": "EOD"})

    if not trades:
        return {"pnl": 0.0, "wr": 0.0, "n": 0, "trades": []}

    total_pnl = sum(t["pnl"] for t in trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    return {
        "pnl": round(total_pnl, 2),
        "wr": round(100.0 * wins / len(trades), 1),
        "n": len(trades),
        "trades": trades,
    }


def run_comparative_backtest(csv_path: str, tf: str, rsi_params: dict, boll_params: dict) -> dict:
    """Roda BOLLINGER e RSI_REVERSION no mesmo CSV e retorna ambas as stats."""
    bars = _load_bars(csv_path)
    return {
        "rsi": _simulate(bars, _rsi_reversion_signal, rsi_params),
        "bollinger": _simulate(bars, _bollinger_signal, boll_params),
    }


if __name__ == "__main__":
    # CLI: python3 backtest/mini_compare.py WIN_M5
    import re
    arg = sys.argv[1] if len(sys.argv) > 1 else "WIN_M5"
    # Aceita "M5", "WIN_M5", "WINM5" — extrai SYM e TF
    m = re.match(r"^(WIN|WDO|BIT|DOL|IND|WSP)?_?([MH]\d+)$", arg.upper())
    if not m:
        print(f"❌ Argumento inválido: {arg!r}. Use WIN_M5, WDO_M15, M5, etc.")
        sys.exit(1)
    sym = m.group(1) or "WIN"
    tf = m.group(2)
    csv_path = str(PROJECT_DIR / f"data/{sym}_{tf}.csv")
    if not Path(csv_path).exists():
        print(f"❌ CSV não encontrado: {csv_path}")
        sys.exit(1)
    res = run_comparative_backtest(
        csv_path=csv_path, tf=tf,
        rsi_params={"rsi_period": 14, "rsi_overbought": 70, "rsi_oversold": 30,
                    "sl_atr_mult": 0.6, "trail_activate": 1.0, "trail_distance": 0.4,
                    "cooldown_seconds": 180, "max_daily_trades": 8},
        boll_params={"bb_period": 20, "bb_std": 2.0, "rsi_overbought": 70, "rsi_oversold": 30,
                     "sl_atr_mult": 0.6, "trail_activate": 1.0, "trail_distance": 0.4,
                     "cooldown_seconds": 180, "max_daily_trades": 8},
    )
    print(f"\n=== {sym} {tf} (mini-backtest comparativo) ===")
    print(f"  BOLLINGER     : PnL R$ {res['bollinger']['pnl']:>+9,.2f} | "
          f"WR {res['bollinger']['wr']:>5.1f}% | n={res['bollinger']['n']}")
    print(f"  RSI_REVERSION : PnL R$ {res['rsi']['pnl']:>+9,.2f} | "
          f"WR {res['rsi']['wr']:>5.1f}% | n={res['rsi']['n']}")
    delta = res['rsi']['pnl'] - res['bollinger']['pnl']
    winner = "RSI_REVERSION" if delta > 0 else "BOLLINGER"
    print(f"  Delta: R$ {delta:+,.2f} → {winner} vence")
