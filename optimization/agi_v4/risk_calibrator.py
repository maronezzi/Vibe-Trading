"""
risk_calibrator.py — o AGI calibra os próprios parâmetros de risco por
SIMULAÇÃO CONTRAFACTUAL nos dados reais (Wave AGI-super, Bruno 2026-08-13).

Filosofia: nenhum valor de risco fica fixo "na mão". A cada rodada pós-mercado
o AGI replay-os dias reais do DB e escolhe o valor que teria PRESERVADO mais
PnL, com evidência mínima para mexer (anti-churn):

  1. max_daily_loss_by_symbol (stop diário por ativo)
     Grid de candidatos; para cada dia histórico, simula: "se o stop fosse S,
     entradas após o PnL diário cruzar S seriam bloqueadas". Escolhe o S que
     maximiza o PnL acumulado (perdas evitadas vs oportunidades perdidas).

  2. profit_lock_min_target (alvo de lucro diário da conta)
     Grid de alvos; simula: "ao cruzar o alvo, o lock bloqueia novas entradas".
     Escolhe o alvo que maximiza o PnL preservado (trava no pico certo vs
     deixa o dia continuar rendendo).

  3. execution_guards.max_slippage_pts_by_symbol (tolerância de entrada)
     Estatística dos gaps inter-bar do perpétuo M15 (movimento "normal" da
     fita) + ATR de referência: slip = clamp(p90(gaps)×1.25, 0.35×ATR, 1.2×ATR).
     Entrada cujo preço andou além disso é anômala — melhor perder o setup.

Critérios de aplicação (evita ficar trocando valor todo dia):
  - mínimo MIN_DAYS dias com ≥ MIN_TRADES trades;
  - ganho do candidato sobre o valor atual ≥ MIN_GAIN_R (R$ acumulado);
  - slippage só muda se fora da banda [0.7×, 1.4×] do calculado (histerese).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("agi_v4.risk_calibrator")

STOP_GRID = [-100, -125, -150, -175, -200, -250, -300, -400, -500]
TARGET_GRID = [100, 150, 200, 250, 300, 400, 500, 650, 800, 1000]
MIN_DAYS = 5          # dias mínimos de histórico p/ calibrar
MIN_TRADES_DAY = 3    # dias com menos trades não contam
MIN_GAIN_R = 15.0     # ganho mínimo acumulado (R$) p/ trocar o valor
LOOKBACK_DAYS = 21    # janela de calibração (3 semanas)

# Conversão preço → pontos por ativo (espelho de _point_map do autotrader)
_POINT_VAL = {"WIN": 1.0, "WDO": 0.001, "BIT": 0.01, "WSP": 0.01, "IND": 1.0}


def _db_path(config: dict) -> Path | None:
    try:
        from .stage1_collect import _resolve_db_path
        return _resolve_db_path(config)
    except Exception:
        p = Path("/home/bruno/Projects/Vibe-Trading/vt_trades.db")
        return p if p.exists() else None


def _load_trades(config: dict) -> list[dict]:
    """Trades reais da janela (sem GHOST), ordenadas por entry_time."""
    db = _db_path(config)
    if not db or not Path(db).exists():
        return []
    cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    try:
        conn = sqlite3.connect(str(db))
        rows = conn.execute(
            """SELECT symbol, timeframe, net_pnl, entry_time, exit_time
               FROM trades
               WHERE entry_time >= ? AND exit_time IS NOT NULL
                 AND exit_reason != 'GHOST'
               ORDER BY entry_time""",
            (cutoff,),
        ).fetchall()
        conn.close()
        return [
            {"root": r[0][:3], "tf": r[1], "pnl": float(r[2] or 0),
             "day": str(r[3])[:10]}
            for r in rows
        ]
    except Exception as e:
        log.warning(f"risk_calibrator: carga do DB falhou: {e}")
        return []


def _sim_with_stop(pnls: list[float], stop: float) -> float:
    cum = 0.0
    for p in pnls:
        if cum <= stop:      # já sangrou: entradas subsequentes bloqueadas
            break
        cum += p
    return cum


def _sim_with_target(pnls: list[float], target: float) -> float:
    cum = 0.0
    for p in pnls:
        if cum >= target:    # lock armado: novas entradas bloqueadas
            break
        cum += p
    return cum


def calibrate_daily_stops(config: dict, trades: list[dict]) -> dict:
    """Stop diário ótimo por símbolo via counterfactual."""
    out = {}
    current = config.get("max_daily_loss_by_symbol", {}) or {}
    for root in sorted({t["root"] for t in trades} | set(current.keys())):
        days: dict[str, list[float]] = {}
        for t in trades:
            if t["root"] != root:
                continue
            days.setdefault(t["day"], []).append(t["pnl"])
        days = {d: v for d, v in days.items() if len(v) >= MIN_TRADES_DAY}
        if len(days) < MIN_DAYS:
            out[root] = {"status": "dados_insuficientes",
                         "days": len(days), "keep": current.get(root, 0)}
            continue
        scores = {}
        for s in STOP_GRID:
            scores[s] = sum(_sim_with_stop(v, s) for v in days.values())
        no_stop = sum(sum(v) for v in days.values())
        best = max(scores, key=lambda s: (scores[s], s))  # empate → menos restritivo
        cur = float(current.get(root, 0) or 0)
        cur_score = scores.get(int(cur)) if cur in STOP_GRID else None
        gain = scores[best] - (cur_score if cur_score is not None else no_stop)
        apply = (cur not in STOP_GRID) or (gain >= MIN_GAIN_R and best != cur)
        out[root] = {
            "status": "calibrado",
            "days": len(days),
            "best": best,
            "score_best": round(scores[best], 2),
            "score_current": round(cur_score, 2) if cur_score is not None else None,
            "score_no_stop": round(no_stop, 2),
            "gain": round(gain, 2),
            "current": cur,
            "apply": bool(apply and best != cur),
            "grid": {str(k): round(v, 2) for k, v in scores.items()},
        }
    return out


def calibrate_profit_target(config: dict, trades: list[dict]) -> dict:
    """Alvo de lucro diário da conta (profit_lock_min_target) via counterfactual."""
    days: dict[str, list[float]] = {}
    for t in trades:  # conta inteira: todos os símbolos, ordem de entrada
        days.setdefault(t["day"], []).append(t["pnl"])
    days = {d: v for d, v in days.items() if len(v) >= MIN_TRADES_DAY}
    cur = float(config.get("profit_lock_min_target", 250) or 250)
    if len(days) < MIN_DAYS:
        return {"status": "dados_insuficientes", "days": len(days), "keep": cur}
    scores = {}
    for tg in TARGET_GRID:
        scores[tg] = sum(_sim_with_target(v, tg) for v in days.values())
    no_lock = sum(sum(v) for v in days.values())
    best = max(scores, key=lambda t: (scores[t], -t))  # empate → alvo menor (trava cedo)
    cur_score = scores.get(int(cur)) if cur in TARGET_GRID else None
    gain = scores[best] - (cur_score if cur_score is not None else no_lock)
    apply = (cur not in TARGET_GRID) or (gain >= MIN_GAIN_R and best != cur)
    return {
        "status": "calibrado",
        "days": len(days),
        "best": best,
        "score_best": round(scores[best], 2),
        "score_current": round(cur_score, 2) if cur_score is not None else None,
        "score_no_lock": round(no_lock, 2),
        "gain": round(gain, 2),
        "current": cur,
        "apply": bool(apply and best != cur),
        "grid": {str(k): round(v, 2) for k, v in scores.items()},
    }


def calibrate_slippage(config: dict) -> dict:
    """Tolerância de slippage por símbolo: p90 dos gaps M15 vs ATR (perpétua)."""
    out = {}
    guards = config.get("execution_guards", {}) or {}
    current = guards.get("max_slippage_pts_by_symbol", {}) or {}
    try:
        from backtest import backtest_v944 as bt
    except Exception as e:
        return {"error": f"backtest indisponível: {e}"}
    for root in ["WIN", "WDO", "WSP", "BIT"]:
        pv = _POINT_VAL.get(root, 1.0)
        try:
            path = bt.fetch(f"{root}$", "M15", 900)
            df = bt.load_csv(path) if path else None
            if df is None or len(df) < 100:
                out[root] = {"status": "sem_barras"}
                continue
            chrono = df.sort_index()
            gaps, atrs = [], []
            prev_day = None
            closes = list(chrono["close"])
            highs = list(chrono["high"])
            lows = list(chrono["low"])
            for i in range(1, len(chrono)):
                d = chrono.index[i].date()
                # gap entre barras do MESMO dia (evita overnight/fim de sessão)
                if d == prev_day:
                    g = abs(float(chrono["open"].iloc[i]) - closes[i - 1]) / pv
                    gaps.append(g)
                prev_day = d
                if i >= 15:
                    trs = []
                    for j in range(i - 14, i + 1):
                        trs.append(max(highs[j] - lows[j],
                                       abs(highs[j] - closes[j - 1]),
                                       abs(lows[j] - closes[j - 1])))
                    atrs.append(sum(trs) / len(trs) / pv)
            gaps.sort()
            p90 = gaps[int(len(gaps) * 0.90)] if gaps else 0
            atrs.sort()
            atr_ref = atrs[len(atrs) // 2] if atrs else 0
            if atr_ref <= 0:
                out[root] = {"status": "atr_invalido"}
                continue
            slip = max(min(p90 * 1.25, 1.2 * atr_ref), 0.35 * atr_ref)
            slip = int(round(slip / 5.0) * 5)  # arredonda p/ múltiplo de 5
            cur = float(current.get(root, 0) or 0)
            # histerese: só troca se o atual está fora da banda [0.7×, 1.4×]
            apply = (cur <= 0) or not (0.7 * slip <= cur <= 1.4 * slip)
            out[root] = {
                "status": "calibrado",
                "p90_gap_pts": round(p90, 1),
                "atr_m15_pts": round(atr_ref, 1),
                "best": slip,
                "current": cur,
                "apply": bool(apply),
            }
        except Exception as e:
            out[root] = {"status": "erro", "error": str(e)[:120]}
    return out


def run(ctx: dict) -> dict:
    """Stage de calibração de risco. Fail-safe: nunca derruba o pipeline."""
    config = ctx.get("config", {}) or {}
    dry_run = ctx.get("dry_run", True)
    trades = _load_trades(config)
    if not trades:
        log.warning("risk_calibrator: sem trades na janela — nada a calibrar")
        ctx["risk_calibration"] = {"error": "sem trades"}
        return {"summary": "sem trades na janela"}

    stops = calibrate_daily_stops(config, trades)
    target = calibrate_profit_target(config, trades)
    slips = calibrate_slippage(config)

    # ── Log humano ──
    for root, r in stops.items():
        if r.get("status") == "calibrado":
            log.info(f"risk_calibrator: STOP {root}: atual {r['current']} → "
                     f"ótimo {r['best']} (ganho R${r['gain']:+.2f}, "
                     f"{r['days']} dias) {'APLICA' if r['apply'] else 'mantém'}")
        else:
            log.info(f"risk_calibrator: STOP {root}: {r.get('status')} "
                     f"({r.get('days', 0)} dias) — mantém {r.get('keep')}")
    if target.get("status") == "calibrado":
        log.info(f"risk_calibrator: TARGET conta: atual {target['current']} → "
                 f"ótimo {target['best']} (ganho R${target['gain']:+.2f}, "
                 f"{target['days']} dias) "
                 f"{'APLICA' if target['apply'] else 'mantém'}")
    for root, r in slips.items():
        if r.get("status") == "calibrado":
            log.info(f"risk_calibrator: SLIP {root}: p90_gap={r['p90_gap_pts']}pts "
                     f"ATR={r['atr_m15_pts']}pts → ótimo {r['best']}pts "
                     f"(atual {r['current']}) {'APLICA' if r['apply'] else 'mantém'}")

    result = {"daily_stops": stops, "profit_target": target, "slippage": slips}
    ctx["risk_calibration"] = result

    # ── Aplicação (só produção, só com evidência) ──
    changes = []
    if not dry_run:
        try:
            from core.vt_config_loader import load_config, save_full_config
            cfg = load_config(force=True)
            mdls = dict(cfg.get("max_daily_loss_by_symbol", {}) or {})
            for root, r in stops.items():
                if r.get("apply") and r.get("status") == "calibrado":
                    mdls[root] = r["best"]
                    changes.append(f"stop {root}: {r['current']}→{r['best']}")
            if mdls:
                cfg["max_daily_loss_by_symbol"] = mdls
            if target.get("apply") and target.get("status") == "calibrado":
                cfg["profit_lock_min_target"] = float(target["best"])
                changes.append(f"target: {target['current']}→{target['best']}")
            guards = dict(cfg.get("execution_guards", {}) or {})
            slips_cfg = dict(guards.get("max_slippage_pts_by_symbol", {}) or {})
            for root, r in slips.items():
                if r.get("apply") and r.get("status") == "calibrado":
                    slips_cfg[root] = r["best"]
                    changes.append(f"slip {root}: {r.get('current') or 0}→{r['best']}")
            if slips_cfg:
                guards["max_slippage_pts_by_symbol"] = slips_cfg
                cfg["execution_guards"] = guards
            if changes:
                save_full_config(cfg, updated_by="agi_v4_risk_calibrator")
                # sincroniza ctx
                try:
                    config.clear()
                    config.update(load_config(force=True))
                except Exception:
                    pass
                log.info(f"risk_calibrator: APLICADO — {', '.join(changes)}")
            else:
                log.info("risk_calibrator: nada a aplicar (valores atuais já ótimos/sem evidência)")
        except Exception as e:
            log.error(f"risk_calibrator: aplicação falhou: {e}")
            result["apply_error"] = str(e)[:200]

    result["applied"] = changes
    summary = (f"stops: {sum(1 for r in stops.values() if r.get('status') == 'calibrado')} calibrados, "
               f"target {'ok' if target.get('status') == 'calibrado' else 'insuficiente'}, "
               f"slips: {sum(1 for r in slips.values() if r.get('status') == 'calibrado')} calibrados"
               f"{f', aplicou: ' + '; '.join(changes) if changes else ', nada aplicado'}")
    return {"summary": summary}
