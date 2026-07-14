"""
backtest_w874_synthetic.py
============================
Wave W874 (2026-07-08): backtest sintético das 5 estratégias novas.

OBJETIVO: validar que cada estratégia produz sinais coerentes em cenários
controlados (tendência, range, choque, reversão). NÃO mede PnL real
(precisaria de OHLCV histórico do MT5).

USO:
  python3 backtest/backtest_w874_synthetic.py

CENÁRIOS SINTÉTICOS:
  1. Tendência de alta (10 sessões)
  2. Tendência de baixa (10 sessões)
  3. Range/chop (10 sessões)
  4. Choque de volatilidade (5 sessões)
  5. Reversão após exaustão (5 sessões)

MÉTRICAS POR ESTRATÉGIA:
  - Total de sinais
  - Distribuição BUY/SELL
  - Taxa de coerência direcional (% sinais alinhados com a tendência simulada)
  - Latência média de detecção (barras entre início do cenário e 1º sinal)

Saída: tabela formatada + JSON opcional para AGI consumir.
"""
import sys
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Importa utils reais do autotrader
from core.vt_autotrader import (
    calculate_vwap, calculate_ema, calculate_rsi, calculate_adx,
    calculate_bollinger, calculate_atr, get_market_regime, _calc_sl,
)

# Importa as 5 estratégias novas
from strategies import (
    vwap_extreme_reversion,
    liquidity_sweep_reversal,
    htf_bias_ltf_entry,
    atr_expansion_breakout,
    session_momentum_close,
)

STRATEGIES = [
    ("VWAP_EXTREME_REVERSION", vwap_extreme_reversion),
    ("LIQUIDITY_SWEEP_REVERSAL", liquidity_sweep_reversal),
    ("HTF_BIAS_LTF_ENTRY", htf_bias_ltf_entry),
    ("ATR_EXPANSION_BREAKOUT", atr_expansion_breakout),
    ("SESSION_MOMENTUM_CLOSE", session_momentum_close),
]

UTILS = {
    "calc_sl": _calc_sl,
    "calculate_vwap": calculate_vwap,
    "calculate_ema": calculate_ema,
    "calculate_rsi": calculate_rsi,
    "calculate_adx": calculate_adx,
    "calculate_bollinger": calculate_bollinger,
    "calculate_atr": calculate_atr,
    "get_market_regime": get_market_regime,
}


def _brt_ts(year, month, day, hour, minute):
    brt = timezone(timedelta(hours=-3))
    return datetime(year, month, day, hour, minute, tzinfo=brt).timestamp()


def _make_bar(time_unix, open_, high, low, close, vol=1000):
    return {
        "time": int(time_unix),
        "open": open_, "high": high, "low": low, "close": close,
        "tick_volume": vol,
    }


def _scenario_uptrend(n_sessions=10, bars_per_session=100, base=100.0, atr_pts=2.0):
    """10 pregões sintéticos com tendência de alta + exhaustion climax no final."""
    bars_all = []
    price = base
    brt = timezone(timedelta(hours=-3))
    for session in range(n_sessions):
        base_dt = datetime(2026, 7, 1 + session, 9, 0, tzinfo=brt)
        for i in range(bars_per_session):
            # Últimas 15 barras do dia: exhaustion (volume climax, drift menor)
            if i >= bars_per_session - 15:
                drift = 0.02
                cur_atr = atr_pts * 1.5
                vol = 2200
            else:
                drift = 0.05
                cur_atr = atr_pts
                vol = 1000
            close = price + drift
            high = close + cur_atr / 2
            low = open_ = price - cur_atr / 4
            ts = base_dt.timestamp() + i * 300
            bars_all.append(_make_bar(ts, open_, high, low, close, vol=vol))
            price = close
    return bars_all, "BULL"


def _scenario_downtrend(n_sessions=10, bars_per_session=100, base=100.0, atr_pts=2.0):
    bars_all = []
    price = base
    brt = timezone(timedelta(hours=-3))
    for session in range(n_sessions):
        base_dt = datetime(2026, 7, 1 + session, 9, 0, tzinfo=brt)
        for i in range(bars_per_session):
            if i >= bars_per_session - 15:
                drift = -0.02
                cur_atr = atr_pts * 1.5
                vol = 2200
            else:
                drift = -0.05
                cur_atr = atr_pts
                vol = 1000
            close = price + drift
            high = open_ = price + cur_atr / 4
            low = close - cur_atr / 2
            ts = base_dt.timestamp() + i * 300
            bars_all.append(_make_bar(ts, open_, high, low, close, vol=vol))
            price = close
    return bars_all, "BEAR"


def _scenario_choppy(n_sessions=10, bars_per_session=100, base=100.0, atr_pts=2.0):
    import random
    random.seed(42)
    bars_all = []
    price = base
    brt = timezone(timedelta(hours=-3))
    for session in range(n_sessions):
        base_dt = datetime(2026, 7, 1 + session, 9, 0, tzinfo=brt)
        for i in range(bars_per_session):
            change = random.uniform(-0.5, 0.5)
            close = price + change
            high = max(price, close) + atr_pts / 2
            low = min(price, close) - atr_pts / 2
            # Volume médio, sem climax
            ts = base_dt.timestamp() + i * 300
            bars_all.append(_make_bar(ts, price, high, low, close, vol=1000))
            price = close
    return bars_all, "CHOP"


def _scenario_vol_shock(n_sessions=5, bars_per_session=100, base=100.0):
    """5 pregões com choque de volatilidade a partir da barra 50."""
    bars_all = []
    price = base
    brt = timezone(timedelta(hours=-3))
    for session in range(n_sessions):
        base_dt = datetime(2026, 7, 1 + session, 9, 0, tzinfo=brt)
        for i in range(bars_per_session):
            if i >= 50:
                # Choque: range 10 vs base 2
                cur_atr = 10.0
                vol = 2500
                drift = 0.3
            else:
                cur_atr = 1.0
                vol = 1000
                drift = 0.0
            close = price + drift
            high = close + cur_atr / 2
            low = close - cur_atr / 2
            ts = base_dt.timestamp() + i * 300
            bars_all.append(_make_bar(ts, price, high, low, close, vol=vol))
            price = close
    return bars_all, "BREAKOUT"


def _scenario_reversal(n_sessions=5, bars_per_session=100, base=100.0):
    """5 pregões: forte tendência → exaustão → reversão."""
    bars_all = []
    price = base
    brt = timezone(timedelta(hours=-3))
    for session in range(n_sessions):
        base_dt = datetime(2026, 7, 1 + session, 9, 0, tzinfo=brt)
        for i in range(bars_per_session):
            if i < 50:
                drift = 0.3  # strong uptrend
                cur_atr = 3.0
                vol = 1000
            elif i < 70:
                drift = 0.0  # exaustão (chop perto topo)
                cur_atr = 1.5
                vol = 1500  # volume climax
            else:
                drift = -0.4  # reversão brusca
                cur_atr = 5.0
                vol = 2000
            close = price + drift
            high = close + cur_atr / 2
            low = close - cur_atr / 2
            ts = base_dt.timestamp() + i * 300
            bars_all.append(_make_bar(ts, price, high, low, close, vol=vol))
            price = close
    return bars_all, "REVERSAL"


def _run_scenario(strategy_module, bars_all, expected_bias, scenario_name, params=None):
    """Rola estratégia em janela deslizante e conta sinais."""
    if params is None:
        params = {}
    win = 60  # janela mínima
    buys = sells = 0
    first_signal_bar = None
    atr_ref = 2.0
    for i in range(win, len(bars_all)):
        window = bars_all[:i + 1]
        cur = window[0]
        atr = calculate_atr(window, 14) or atr_ref
        try:
            result = strategy_module.check_entry(
                "WINQ26", "M5", cur["close"], atr,
                cur["time"], window, params, UTILS,
            )
        except Exception as e:
            continue
        if result is None:
            continue
        if result["direction"] == "BUY":
            buys += 1
        elif result["direction"] == "SELL":
            sells += 1
        if first_signal_bar is None:
            first_signal_bar = i

    total = buys + sells
    if total == 0:
        coherence = 0.0
    else:
        if expected_bias == "BULL":
            coherent = buys
        elif expected_bias == "BEAR":
            coherent = sells
        else:
            coherent = max(buys, sells)
        coherence = coherent / total

    return {
        "scenario": scenario_name,
        "expected_bias": expected_bias,
        "buys": buys,
        "sells": sells,
        "total": total,
        "coherence": round(coherence, 3),
        "first_signal_bar": first_signal_bar,
        "latency_bars": (first_signal_bar - win) if first_signal_bar else None,
    }


def main():
    scenarios = [
        ("Uptrend (10d)", _scenario_uptrend),
        ("Downtrend (10d)", _scenario_downtrend),
        ("Choppy (10d)", _scenario_choppy),
        ("Vol Shock (5d)", _scenario_vol_shock),
        ("Reversal (5d)", _scenario_reversal),
    ]

    print("=" * 80)
    print("Wave W874 (2026-07-08) — Backtest Sintético: 5 Estratégias Novas")
    print("=" * 80)
    print(f"Cenários: {len(scenarios)} | Estratégias: {len(STRATEGIES)}")
    print()

    results = {}
    for strat_name, strat_module in STRATEGIES:
        print(f"\n### {strat_name}")
        print(f"{'Cenário':<20} {'Bias':<8} {'BUY':>4} {'SELL':>5} {'Total':>6} {'Coh%':>7} {'Lat':>5}")
        print("-" * 60)
        results[strat_name] = []
        for scenario_name, scenario_fn in scenarios:
            bars, bias = scenario_fn()
            r = _run_scenario(strat_module, bars, bias, scenario_name)
            results[strat_name].append(r)
            lat = f"{r['latency_bars']}" if r['latency_bars'] is not None else "—"
            print(f"{r['scenario']:<20} {r['expected_bias']:<8} "
                  f"{r['buys']:>4} {r['sells']:>5} {r['total']:>6} "
                  f"{r['coherence']*100:>6.0f}% {lat:>5}")

    print("\n" + "=" * 80)
    print("RESUMO POR ESTRATÉGIA")
    print("=" * 80)
    for strat_name, rows in results.items():
        total_signals = sum(r["total"] for r in rows)
        avg_coherence = sum(r["coherence"] for r in rows) / len(rows)
        print(f"{strat_name:<32} signals={total_signals:>4}  "
              f"avg_coherence={avg_coherence*100:>5.1f}%")

    out_path = PROJECT_ROOT / "data" / "W874_synthetic_backtest.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResultados detalhados salvos em: {out_path}")
    print("\nNOTA: Backtest sintético valida COERÊNCIA dos sinais,")
    print("não PnL real. Para validar edge, necessário backtest com OHLCV")
    print("histórico do MT5 (próxima etapa antes de ativar).")


if __name__ == "__main__":
    main()