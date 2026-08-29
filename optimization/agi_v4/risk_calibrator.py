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

  2b. trailing_activation_pct (Wave 883.B3, Bruno 29/08: "a trava fica, o AGI
     sintoniza — número sem chute"). Nível em que o ratchet diário arma e
     bloqueia novas entradas, como fração do trailing_target_per_lot. Mesmo
     contrafactual do alvo; empate → trava mais cedo (o motivo da trava
     existir é lucro de manhã devolvido à tarde).

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
# Wave 883.B3 (Bruno 29/08): sintonia da TRAVA de lucro (ratchet diário do
# trailing_profit_lock). A trava EXISTE porque o sistema repetidamente
# lucrava de manhã e devolvia tudo (Bruno 29/08: "mantemos, mas o AGI sintoniza,
# número sem chute"). O grid é a fração do trailing_target_per_lot em que a
# trava arma e bloqueia novas entradas do dia.
ACTIVATION_GRID = [0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
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


def _load_shadow_trades(config: dict) -> list[dict]:
    """Trades do forward_walker (forward_sim_trades) na janela.

    Wave 880.I (Bruno 19/08): o walker replica a entrada do daemon MAS NÃO
    arma o profit lock diário — logo sua sequência por dia é NÃO-CENSURADA.
    É a "super informação" que faltava à calibração do alvo: hoje (19/08) o
    live travou em +R$90 às 10:06 (target 100) enquanto o shadow fez +R$395
    — calibrar o target só no live é viesado para BAIXO (o lock corta os
    trades que provariam que um alvo maior renderia mais).

    Limitação conhecida (norma §11): o walker usa multiplier uniforme
    (escala relativa ≠ escala live). A calibração reescala por dia (ver
    _merge_with_shadow) antes de usar.
    """
    db = _db_path(config)
    if not db or not Path(db).exists():
        return []
    cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    try:
        conn = sqlite3.connect(str(db))
        rows = conn.execute(
            """SELECT symbol, timeframe, net_pnl_brl, entry_time
               FROM forward_sim_trades
               WHERE entry_time >= ?
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
        log.warning(f"risk_calibrator: carga do shadow falhou: {e}")
        return []


def _merge_with_shadow(trades: list[dict], shadow: list[dict],
                       cur_target: float) -> tuple[dict[str, list[float]], dict]:
    """Dias para a calibração do alvo, reconstruindo dias censurados pelo lock.

    Método (Wave 880.I):
      1. Razão de escala dia-a-dia live/shadow nos dias com ambos (clamp
         [0.2, 2.0]); fator global = mediana. Sem pares suficientes →
         live puro (status shadow_sem_escala).
      2. Dia "censurado" = PnL live do dia cruzou o alvo atual (lock armado
         e sequência truncada). Para esses dias usa-se a sequência do
         shadow REESCALADA (contrafactual em escala live do que teria
         acontecido sem travar).
      3. Dias não-censurados ficam com a sequência live real.

    Returns:
        (days: {dia: [pnls em ordem]}, meta: {"ratio", "n_shadow_days", ...})
    """
    live_days: dict[str, list[dict]] = {}
    for t in trades:
        live_days.setdefault(t["day"], []).append(t)
    shadow_days: dict[str, list[dict]] = {}
    for t in shadow:
        shadow_days.setdefault(t["day"], []).append(t)

    ratios = []
    for d, sh in shadow_days.items():
        lv = live_days.get(d)
        if not lv or len(sh) < MIN_TRADES_DAY:
            continue
        s_sum = sum(t["pnl"] for t in sh)
        l_sum = sum(t["pnl"] for t in lv)
        if abs(s_sum) < 1.0:
            continue
        ratios.append(min(max(l_sum / s_sum, 0.2), 2.0))
    ratios.sort()
    ratio = ratios[len(ratios) // 2] if ratios else None

    days: dict[str, list[float]] = {}
    n_reconstructed = 0
    for d in sorted(set(live_days) | set(shadow_days)):
        lv = live_days.get(d, [])
        sh = shadow_days.get(d, [])
        cum = 0.0
        peak = 0.0
        for t in lv:
            cum += t["pnl"]
            peak = max(peak, cum)
        censored = bool(lv) and ratio is not None and len(sh) >= MIN_TRADES_DAY \
            and peak >= cur_target * 0.95
        if censored:
            days[d] = [t["pnl"] * ratio for t in sh]
            n_reconstructed += 1
        elif lv:
            days[d] = [t["pnl"] for t in lv]
        else:
            # dia só-shadow (live não operou): reescala também — shadow está
            # em escala do walker, não em escala live da conta
            days[d] = [t["pnl"] * (ratio or 1.0) for t in sh]
    meta = {"ratio": round(ratio, 3) if ratio else None,
            "n_ratio_days": len(ratios),
            "n_reconstructed_days": n_reconstructed,
            "n_live_days": len(live_days),
            "n_shadow_days": len(shadow_days)}
    return days, meta


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


def calibrate_profit_target(config: dict, trades: list[dict],
                            shadow: list[dict] | None = None) -> dict:
    """Alvo de lucro diário da conta (profit_lock_min_target) via counterfactual.

    Wave 880.I (Bruno 19/08 — "profit lock variável, ajustável no AGI"):
    além do grid contrafactual, agora (1) reconstrói dias censurados pelo
    lock com o shadow NÃO-CENSURADO do forward_walker reescalado (ver
    _merge_with_shadow), e (2) aplica histerese de movimento — o alvo novo
    fica clamped em [0.5x, 2.0x] do atual (variável sem salto, anti-churn
    de risco).
    """
    cur = float(config.get("profit_lock_min_target", 250) or 250)
    days, meta = _merge_with_shadow(trades, shadow or [], cur)
    days = {d: v for d, v in days.items() if len(v) >= MIN_TRADES_DAY}
    meta["n_days_used"] = len(days)
    if len(days) < MIN_DAYS:
        return {"status": "dados_insuficientes", "days": len(days), "keep": cur,
                "shadow_meta": meta}
    scores = {}
    for tg in TARGET_GRID:
        scores[tg] = sum(_sim_with_target(v, tg) for v in days.values())
    no_lock = sum(sum(v) for v in days.values())
    best = max(scores, key=lambda t: (scores[t], -t))  # empate → alvo menor (trava cedo)
    # Histerese: alvo é VARIÁVEL, mas move no máximo ±50%/2x por ajuste —
    # nunca salta de 100 p/ 1000 numa sessão só (régua W880: blast radius).
    best_clamped = int(min(max(best, round(cur * 0.5)), round(cur * 2.0)))
    if best_clamped not in scores:
        best_clamped = min(TARGET_GRID, key=lambda t: abs(t - best_clamped))
    best = best_clamped
    cur_score = scores.get(int(cur)) if cur in TARGET_GRID else None
    gain = scores[best] - (cur_score if cur_score is not None else no_lock)
    apply = (cur not in TARGET_GRID) or (gain >= MIN_GAIN_R and best != cur)
    return {
        "status": "calibrado",
        "days": len(days),
        "best": best,
        "best_raw": max(scores, key=lambda t: (scores[t], -t)),
        "score_best": round(scores[best], 2),
        "score_current": round(cur_score, 2) if cur_score is not None else None,
        "score_no_lock": round(no_lock, 2),
        "gain": round(gain, 2),
        "current": cur,
        "apply": bool(apply and best != cur),
        "shadow_meta": meta,
        "grid": {str(k): round(v, 2) for k, v in scores.items()},
    }


def calibrate_lock_activation(config: dict, trades: list[dict],
                              shadow: list[dict] | None = None) -> dict:
    """Wave 883.B3 (Bruno 29/08): sintonia da trava de lucro diária
    (trailing_activation_pct) via counterfactual — "número sem chute".

    A trava arma em activation × trailing_target_per_lot e bloqueia novas
    entradas no resto do dia (ratchet + floor protegem o acumulado). O
    contrafactual reusa o simulador do alvo: _sim_with_target(dia, nível de
    armação) aproxima o dia travado no nível dado. A favor da aproximação
    ser conservadora: ignorar o piso do ratchet SUBESTIMA o valor de travar
    cedo (o modelo não credita o floor segurando o pico), então o ótimo
    encontrado tende ao lado de proteger primeiro.

    Desempate: score igual → ativação MENOR (trava cedo), alinhado ao motivo
    da trava existir ("lucro de manhã devolvido à tarde").
    """
    per_lot = float(config.get("trailing_target_per_lot", 250.0) or 250.0)
    cur_pct = float(config.get("trailing_activation_pct", 0.5) or 0.5)
    cur_level = cur_pct * per_lot
    days, meta = _merge_with_shadow(trades, shadow or [], cur_level)
    days = {d: v for d, v in days.items() if len(v) >= MIN_TRADES_DAY}
    if len(days) < MIN_DAYS:
        return {"status": "dados_insuficientes", "days": len(days), "keep": cur_pct,
                "shadow_meta": meta}
    scores = {}
    for a in ACTIVATION_GRID:
        scores[a] = sum(_sim_with_target(v, a * per_lot) for v in days.values())
    no_lock = sum(sum(v) for v in days.values())
    best_raw = max(ACTIVATION_GRID, key=lambda a: (scores[a], -a))
    # Histerese: um ajuste move no máximo [0.7x, 1.3x] do valor atual —
    # régua W880 de blast-battery (variável sem salto, anti-churn).
    best = min(max(best_raw, round(cur_pct * 0.7, 2)), round(cur_pct * 1.3, 2))
    best = min(ACTIVATION_GRID, key=lambda a: abs(a - best))
    cur_in_grid = any(abs(a - cur_pct) < 1e-9 for a in ACTIVATION_GRID)
    cur_score = scores.get(cur_pct) if cur_in_grid else None
    gain = scores[best] - (cur_score if cur_score is not None else no_lock)
    apply = (not cur_in_grid) or (gain >= MIN_GAIN_R and best != cur_pct)
    return {
        "status": "calibrado",
        "days": len(days),
        "best": best,
        "best_raw": best_raw,
        "level_best": round(best * per_lot, 2),
        "level_current": round(cur_level, 2),
        "score_best": round(scores[best], 2),
        "score_current": round(cur_score, 2) if cur_score is not None else None,
        "score_no_lock": round(no_lock, 2),
        "gain": round(gain, 2),
        "current": cur_pct,
        "apply": bool(apply and best != cur_pct),
        "shadow_meta": meta,
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

    # Wave 880.I: shadow do forward_walker (não-censurado pelo lock) para a
    # calibração VARIÁVEL do alvo diário.
    shadow = _load_shadow_trades(config)

    stops = calibrate_daily_stops(config, trades)
    target = calibrate_profit_target(config, trades, shadow)
    # Wave 883.B3: sintonia da trava de lucro (ratchet diário) — mesma
    # janela/shadow contrafactual do alvo.
    lock_act = calibrate_lock_activation(config, trades, shadow)
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
        sm = target.get("shadow_meta", {}) or {}
        log.info(f"risk_calibrator: TARGET conta: atual {target['current']} → "
                 f"ótimo {target['best']} (bruto {target.get('best_raw')}, "
                 f"ganho R${target['gain']:+.2f}, {target['days']} dias, "
                 f"shadow ratio {sm.get('ratio')} com "
                 f"{sm.get('n_reconstructed_days', 0)} dia(s) reconstruído(s)) "
                 f"{'APLICA' if target['apply'] else 'mantém'}")
    if lock_act.get("status") == "calibrado":
        log.info(f"risk_calibrator: TRAVA lucro: ativação {lock_act['current']:.2f} "
                 f"→ {lock_act['best']:.2f} (nível R${lock_act['level_current']:.0f}→"
                 f"R${lock_act['level_best']:.0f}, ganho R${lock_act['gain']:+.2f}, "
                 f"{lock_act['days']} dias) "
                 f"{'APLICA' if lock_act['apply'] else 'mantém'}")
    else:
        log.info(f"risk_calibrator: TRAVA lucro: {lock_act.get('status')} "
                 f"({lock_act.get('days', 0)} dias) — mantém {lock_act.get('keep')}")
    for root, r in slips.items():
        if r.get("status") == "calibrado":
            log.info(f"risk_calibrator: SLIP {root}: p90_gap={r['p90_gap_pts']}pts "
                     f"ATR={r['atr_m15_pts']}pts → ótimo {r['best']}pts "
                     f"(atual {r['current']}) {'APLICA' if r['apply'] else 'mantém'}")

    result = {"daily_stops": stops, "profit_target": target,
              "lock_activation": lock_act, "slippage": slips}
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
            if lock_act.get("apply") and lock_act.get("status") == "calibrado":
                cfg["trailing_activation_pct"] = float(lock_act["best"])
                changes.append(f"trava ativação: {lock_act['current']:.2f}→{lock_act['best']:.2f}")
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
               f"trava {'ok' if lock_act.get('status') == 'calibrado' else 'insuficiente'}, "
               f"slips: {sum(1 for r in slips.values() if r.get('status') == 'calibrado')} calibrados"
               f"{', aplicou: ' + '; '.join(changes) if changes else ', nada aplicado'}")
    return {"summary": summary}
