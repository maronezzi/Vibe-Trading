"""
backtest_evaluator.py — Avalia candidatos por SIMULAÇÃO bar-by-bar, nunca
por trades passados do DB.

Princípio (correção fundamental 2026-07-04):
  Trades no vt_trades.db foram executados pela estratégia ANTIGA. Eles NÃO
  são referência válida para julgar um candidato novo (estratégia/params
  diferentes). A única avaliação honesta é SIMULAR o candidato bar-by-bar
  sobre barras reais de mercado do MT5.

Pipeline:
  1. Buscar ~30 dias de barras do MT5 (contrato vigente + TF) via Wine
  2. backtest_combo(): roda o plugin do candidato bar-by-bar, fiel ao
     autotrader (SL, trailing, breakeven, time-trail, max-position, sessão)
  3. Computar métricas (PF, Sharpe, WR, max_dd, n_trades) dos trades
     SIMULADOS — sem ler nada do DB
  4. Walk-forward: dividir os 30d em N janelas, simular em cada uma,
     exigir consistência (anti-overfit)

Uso:
    from optimization.agi_v4.backtest_evaluator import evaluate_candidate
    result = evaluate_candidate("WIN", "M5", "BOLLINGER", params, config)
    if result["passed"]:
        # candidato aprovado por simulação walk-forward
"""
from __future__ import annotations

import logging
import math
from typing import Any

log = logging.getLogger("agi_v4.evaluator")

# Barras por TF para cobrir ~30 dias (mesma tabela do backtest_v944.py:510)
BARS_FOR_30D = {"M5": 2500, "M15": 900, "M30": 500, "H1": 260}

# Janelas de walk-forward (divisão dos 30d). 4 janelas ≈ 7.5 dias cada.
N_WALK_FORWARD_WINDOWS = 4


def evaluate_candidate(
    sym_root: str,
    tf: str,
    strategy_name: str,
    params: dict,
    config: dict,
    *,
    thresholds: dict | None = None,
) -> dict:
    """Avalia um candidato por simulação bar-by-bar em 30 dias + walk-forward.

    Args:
        sym_root: root do símbolo (WIN, WDO, BIT, WSP).
        tf: timeframe (M5, M15, M30, H1).
        strategy_name: nome da estratégia (do STRATEGY_NAME do plugin).
        params: dict de parâmetros para o backtest.
        config: config (para resolved_symbols, contract_specs).
        thresholds: thresholds opcionais (default: _DEFAULTS).

    Returns:
        dict com:
          "passed": bool (passou full + walk-forward)
          "full": métricas do backtest completo 30d
          "walk_forward": lista de métricas por janela
          "reason": motivo se rejeitado
    """
    th = thresholds or _DEFAULT_THRESHOLDS

    # 1. Buscar 30d de barras reais do MT5
    df = _fetch_30d_bars(sym_root, tf, config)
    if df is None or len(df) < 100:
        return _reject("no_market_data", f"sem barras MT5 para {sym_root}_{tf}")

    # 2. Backtest completo 30d
    full_trades = _run_backtest(df, sym_root, tf, strategy_name, params)
    full_metrics = _compute_metrics(full_trades)
    full_metrics["n_bars"] = len(df)
    # Wave fix-contract (01/08): se backtest_combo reportou erros de runtime
    # no check_entry (bug de código, ex: TypeError indexando float), anexamos
    # ao diagnóstico — para o operador distinguir bug de código de falta de
    # edge. A telemetria vem anexada à lista (TradesList) pelo backtest_v944.
    perr = getattr(full_trades, "plugin_errors", 0) or 0
    if perr:
        full_metrics["plugin_errors"] = perr
        full_metrics["plugin_first_error"] = getattr(full_trades, "plugin_first_error", None)

    # Gate de profitability no full 30d
    prof = _check_profitability(full_metrics, th)
    if not prof["ok"]:
        return _reject("profitability_full", prof["reason"], full_metrics, [])

    # 3. Walk-forward: dividir em janelas e simular cada uma
    wf_metrics = []
    wf_slices = _split_into_windows(df, N_WALK_FORWARD_WINDOWS)
    for i, slice_df in enumerate(wf_slices):
        if len(slice_df) < 50:
            continue
        trades = _run_backtest(slice_df, sym_root, tf, strategy_name, params)
        m = _compute_metrics(trades)
        m["window"] = i + 1
        m["n_bars"] = len(slice_df)
        wf_metrics.append(m)

    # Gate de walk-forward: maioria das janelas lucrativas (anti-overfit)
    wf = _check_walk_forward(wf_metrics, th)
    if not wf["ok"]:
        return _reject("walk_forward", wf["reason"], full_metrics, wf_metrics)

    return {
        "passed": True,
        "full": full_metrics,
        "walk_forward": wf_metrics,
        "reason": "",
    }


# ═══════════════════════════════════════════════════════════════════
# Fetch de barras MT5 (contrato vigente) — reusa backtest_v944
# ═══════════════════════════════════════════════════════════════════

def _fetch_30d_bars(sym_root: str, tf: str, config: dict):
    """Busca ~30d de barras do MT5 via Wine. Retorna DataFrame pandas ou None."""
    try:
        from backtest import backtest_v944 as bt
    except ImportError:
        log.error("backtest_v944 não importável")
        return None

    n_bars = BARS_FOR_30D.get(tf, 500)
    # resolved_symbols: WIN -> WINQ26 (contrato vigente). Fallback sintético.
    resolved = config.get("resolved_symbols", {})
    symbol = resolved.get(sym_root, f"{sym_root}$")

    try:
        path = bt.fetch(symbol, tf, n_bars)
        if not path:
            return None
        return bt.load_csv(path)
    except Exception as e:
        log.warning(f"fetch {symbol} {tf} falhou: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════
# Backtest bar-by-bar — reusa backtest_v944.backtest_combo
# ═══════════════════════════════════════════════════════════════════

def _run_backtest(df, sym_root: str, tf: str, strategy_name: str, params: dict) -> list:
    """Roda backtest_combo do backtest_v944 e retorna a lista de trades.

    backtest_combo é FIEL ao autotrader: SL/trailing/breakeven/time-trail/
    max-position/sessão 9h05-16h45/cooldown/limite diário.
    """
    try:
        from backtest import backtest_v944 as bt
        # Garante que estratégias estão carregadas (o plugin precisa existir)
        bt.load_strategies()
        return bt.backtest_combo(df, sym_root, tf, strategy_name, params)
    except Exception as e:
        log.warning(f"backtest_combo {sym_root}_{tf} {strategy_name} falhou: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════
# Métricas — computadas dos trades SIMULADOS (sem tocar no DB)
# ═══════════════════════════════════════════════════════════════════

def _compute_metrics(trades: list) -> dict:
    """Computa PF, Sharpe, WR, max_dd, n_trades de uma lista de trades.

    Cada trade é o dict retornado por backtest_combo: tem "pnl", "entry_dt",
    etc. (chave é "pnl" no backtest_v944).
    """
    if not trades:
        return _empty_metrics()

    pnls = [float(t.get("pnl", 0)) for t in trades]
    n = len(pnls)
    total_pnl = sum(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)

    wr = len(wins) / n * 100 if n else 0.0

    # Sharpe por trade (annualizado × sqrt(252))
    if n >= 2:
        mean_p = total_pnl / n
        var = sum((p - mean_p) ** 2 for p in pnls) / (n - 1)
        std_p = math.sqrt(var)
        sharpe = (mean_p / std_p * math.sqrt(252)) if std_p > 0 else 0.0
    else:
        sharpe = 0.0

    # Max drawdown sobre curva de equity acumulada
    max_dd = _max_drawdown(pnls)

    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0

    # ── PnL do pregão de hoje (Wave hoje-conta-mais) ──
    # As barras são buscadas copy_rates_from_pos(0, N) → a última barra é a
    # mais recente (inclui o pregão atual até agora). "Hoje" = data da última
    # trade; assim evitamos qualquer manipulação de fuso (backtest_v944 usa
    # timestamps do broker, sem tzinfo). today_pnl é um sinal SEPARADO: só é
    # aplicado como bônus no comparativo do stage5, nunca substitui total_pnl.
    today_pnl, today_n_trades = _today_pnl(trades)

    return {
        "n_trades": n,
        "total_pnl": round(total_pnl, 2),
        "pf": round(pf, 3),
        "wr": round(wr, 1),
        "sharpe": round(sharpe, 3),
        "max_dd": round(max_dd, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "today_pnl": round(today_pnl, 2),
        "today_n_trades": today_n_trades,
    }


def _today_pnl(trades: list) -> tuple[float, int]:
    """Soma o PnL das trades do 'dia de hoje' (data da última trade).

    Usado pelo stage5 como tiebreaker/bônus, não como driver (poucas trades
    numa meia-sessão → risco de overfit se substituísse total_pnl).

    entry_dt pode ser datetime, pandas.Timestamp, date ou None; tratamos todos.
    """
    if not trades:
        return 0.0, 0

    def _d(t):
        dt = t.get("entry_dt")
        if dt is None:
            return None
        # datetime/Timestamp têm .date(); date já é date.
        return dt.date() if hasattr(dt, "date") else dt

    dates = [d for d in (_d(t) for t in trades) if d is not None]
    if not dates:
        return 0.0, 0

    today = max(dates)
    pnl = 0.0
    n = 0
    for t in trades:
        if _d(t) == today:
            pnl += float(t.get("pnl", 0))
            n += 1
    return pnl, n


def _max_drawdown(pnls: list) -> float:
    """Máximo drawdown em R$ da curva de equity acumulada."""
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        dd = equity - peak
        if dd < max_dd:
            max_dd = dd
    return max_dd


def _empty_metrics() -> dict:
    return {
        "n_trades": 0, "total_pnl": 0.0, "pf": 0.0, "wr": 0.0,
        "sharpe": 0.0, "max_dd": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
        "today_pnl": 0.0, "today_n_trades": 0,
    }


# ═══════════════════════════════════════════════════════════════════
# Walk-forward — split por janelas de tempo (anti-overfit)
# ═══════════════════════════════════════════════════════════════════

def _split_into_windows(df, n_windows: int) -> list:
    """Divide o DataFrame em N janelas contíguas (preserva ordem temporal).

    Walk-forward exige que cada janela seja um bloco contíguo de tempo —
    nunca randomiza a ordem das barras (senão vira in-sample leakage).
    """
    if df is None or len(df) == 0:
        return []
    total = len(df)
    size = total // n_windows
    slices = []
    for i in range(n_windows):
        start = i * size
        # última janela pega o restante
        end = total if i == n_windows - 1 else (i + 1) * size
        slices.append(df.iloc[start:end])
    return slices


def _check_walk_forward(wf_metrics: list, th: dict) -> dict:
    """Valida consistência walk-forward.

    Critério: >= min_walk_forward_consistency das janelas com trades devem
    ter total_pnl > 0. Janelas sem trades são ignoradas (não há sinal).
    """
    if not wf_metrics:
        return {"ok": False, "reason": "nenhuma janela walk-forward gerou dados"}

    judged = [m for m in wf_metrics if m["n_trades"] > 0]
    if not judged:
        return {"ok": False, "reason": "estratégia não gerou trades em nenhuma janela"}

    positive = sum(1 for m in judged if m["total_pnl"] > 0)
    consistency = positive / len(judged)
    min_cons = th.get("min_walk_forward_consistency", 0.6)

    if consistency < min_cons:
        return {
            "ok": False,
            "reason": f"walk_forward: {positive}/{len(judged)} janelas positivas "
                      f"(consistência {consistency:.0%} < {min_cons:.0%})",
        }
    return {"ok": True, "reason": f"{positive}/{len(judged)} janelas positivas"}


# ═══════════════════════════════════════════════════════════════════
# Gate de profitability (no backtest completo 30d)
# ═══════════════════════════════════════════════════════════════════

_DEFAULT_THRESHOLDS = {
    "min_profit_factor": 1.15,   # Wave 880.A2: 1.05→1.15 (spread+comissão em B3)
    "min_win_rate": 0.35,        # Wave 880.A4: fração 0-1 (unificado com gates.py). _check_profitability converte.
    "min_trades": 20,
    "max_drawdown_pct": -25.0,   # Wave 880.C1: agora CHECADO em _check_profitability
    "min_walk_forward_consistency": 0.65,  # Wave 880.C3: 0.6→0.65
    "min_sharpe": 0.5,           # Wave 880.C2: Sharpe por trade annualizado (já computado)
}


def _check_profitability(metrics: dict, th: dict) -> dict:
    """Gate de profitability nos trades simulados 30d.

    Wave 880: max_dd e sharpe agora são checados (antes eram só computados).
    max_dd do metrics vem em R$ negativo; comparamos contra max_drawdown_pct
    interpretado como floor de percentual da maior perda individual média.
    """
    failures = []
    if metrics["pf"] < th["min_profit_factor"]:
        failures.append(f"PF={metrics['pf']:.2f}<{th['min_profit_factor']}")
    # WR: metrics em percent (0-100), threshold em fração (0-1) — Wave 880.A4.
    wr_frac = metrics["wr"] / 100.0
    if wr_frac < th["min_win_rate"]:
        failures.append(f"WR={metrics['wr']:.1f}%<{th['min_win_rate']*100:.0f}%")
    if metrics["n_trades"] < th["min_trades"]:
        failures.append(f"n_trades={metrics['n_trades']}<{th['min_trades']}")
        # Wave fix-contract (01/08): se houve erros de runtime no check_entry,
        # os 0 trades são BUG DE CÓDIGO, não falta de edge. Sinaliza explícito.
        if metrics.get("plugin_errors"):
            failures.append(
                f"⚠️ BUG DE CÓDIGO: check_entry lançou {metrics['plugin_errors']}x "
                f"exceção ({metrics.get('plugin_first_error', '?')}) — "
                f"corrija o plugin antes de reavaliar"
            )
    # Wave 880.C1: max_dd gate. max_dd está em R$ (negativo); floor em % do
    # maior entre avg_loss absoluto — evita candidatos com drawdown mordaz
    # mesmo com PF ok. Se avg_loss==0 (sem losses), não há o que checar.
    max_dd = metrics.get("max_dd", 0.0)
    avg_loss = abs(metrics.get("avg_loss", 0.0))
    if th.get("max_drawdown_pct") and avg_loss > 0:
        # Razão max_dd/avg_loss como proxy de "ruína" — >2.5× perda média = suspeito.
        dd_ratio = abs(max_dd) / avg_loss if avg_loss > 0 else 0.0
        max_dd_ratio = abs(th["max_drawdown_pct"]) / 10.0  # -25% → 2.5
        if dd_ratio > max_dd_ratio:
            failures.append(f"max_dd_ratio={dd_ratio:.1f}>{max_dd_ratio:.1f} (drawdown={max_dd:.0f}/avg_loss={avg_loss:.0f})")
    # Wave 880.C2: Sharpe gate. Só falha se temos trades suficientes p/ significância.
    if metrics.get("n_trades", 0) >= th["min_trades"] and metrics.get("sharpe", 0.0) < th.get("min_sharpe", 0.0):
        failures.append(f"sharpe={metrics['sharpe']:.2f}<{th.get('min_sharpe', 0.0)}")
    if failures:
        return {"ok": False, "reason": "; ".join(failures)}
    return {"ok": True, "reason": ""}


# ═══════════════════════════════════════════════════════════════════
# Baseline honesto: simular a estratégia ATUAL do config (não DB)
# ═══════════════════════════════════════════════════════════════════

def evaluate_baseline(sym_root: str, tf: str, config: dict) -> dict:
    """Simula a estratégia ATUAL do config (baseline honesto).

    Antes, o baseline vinha do DB (trades da estratégia antiga). Isso é
    desonesto para julgar um candidato novo. O baseline correto é SIMULAR
    a estratégia atual do config nas MESMAS 30d de mercado.

    Returns:
        métricas (mesmo formato de _compute_metrics) ou _empty_metrics().
    """
    pair = f"{sym_root}_{tf}"
    strategy_name = config.get("strategy_by_tf", {}).get(pair, "")
    if not strategy_name:
        strategy_name = config.get("strategy", {}).get(sym_root, "VWAP")
    params = config.get("params_by_tf", {}).get(pair, {})
    if not isinstance(params, dict):
        params = {}

    df = _fetch_30d_bars(sym_root, tf, config)
    if df is None or len(df) < 100:
        log.warning(f"baseline {pair}: sem barras MT5")
        return _empty_metrics()

    trades = _run_backtest(df, sym_root, tf, strategy_name, params)
    m = _compute_metrics(trades)
    m["strategy"] = strategy_name
    log.info(f"baseline {pair} ({strategy_name}): {m['n_trades']}t "
             f"PF={m['pf']:.2f} PnL=R${m['total_pnl']:.2f}")
    return m


# ═══════════════════════════════════════════════════════════════════
# Helper de rejeição
# ═══════════════════════════════════════════════════════════════════

def _reject(gate: str, reason: str, full: dict | None = None, wf: list | None = None) -> dict:
    log.info(f"REJEITADO gate={gate}: {reason}")
    return {
        "passed": False,
        "full": full or _empty_metrics(),
        "walk_forward": wf or [],
        "reason": f"{gate}: {reason}",
    }
