"""
rollover_guard.py — Consciência de rolagem + choque de realidade live (AGI v4).

Wave AGI-rollover (Bruno 2026-08-13): criado após o incidente de rolagem
WINQ26→WINV26 (12-13/08), em que o AGI otimizava na série perpétua WIN$
(= WINV26) enquanto o bot operava WINQ26 no dia do vencimento, e trocou
estratégia de WIN_M5 no meio do primeiro pregão do contrato novo baseado
em 3 trades simulados da manhã.

Novas funções do AGI (gates do Stage 5):

  1. ROLLOVER_FREEZE — contrato resolvido a ≤ FREEZE_DAYS dias úteis do
     vencimento: nenhuma mudança de estratégia/params para o símbolo.
     Perto do vencimento o tape do contrato velho degrada (liquidez migra,
     convergência de preço) e a simulação não representa o que é operado.

  2. ROLLOVER_GRACE — contrato trocado há ≤ GRACE_DAYS dias: nenhuma mudança.
     Não existe histórico live honesto do contrato novo ainda, e a série
     de backtest pode não representar o contrato (perpétua costurada).
     Fonte primária: config["_rollover_log"][root].changed_at (escrito pelo
     vt_calendar.resolve_all_symbols na rolagem). Fallback: primeira trade
     live do contrato no DB.

  3. LIVE_BLEEDING (intraday) — par perdendo ≤ LIVE_BLEED_LIMIT R$ no pregão
     corrente: congela mudanças durante o pregão (anti-churn). Reagir a dia
     ruim no meio do dia é overfit de ruído (filosofia Wave 882).

  4. SIM_LIVE_DIVERGENCE — live do par ≤ LIVE_BLEED_LIMIT com ≥ MIN_TRADES
     trades e simulação "hoje" do candidato ≥ 0: a simulação NÃO representa
     a execução real (série/regime divergente). Mudança baseada nela é lixo.

Tudo fail-safe: qualquer erro de calendário/DB libera a mudança (nunca
bloqueia o AGI por defeito do próprio guard) — mas loga o erro.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger("agi_v4.rollover_guard")

# Contrato vencendo em ≤ N dias úteis → congela mudanças do símbolo
FREEZE_DAYS = int(os.environ.get("VT_AGI_ROLLOVER_FREEZE_DAYS", "3"))
# Rolagem feita há ≤ N dias corridos → congela mudanças do símbolo
GRACE_DAYS = int(os.environ.get("VT_AGI_ROLLOVER_GRACE_DAYS", "3"))
# PnL live do par hoje (R$) que caracteriza "sangrando"
LIVE_BLEED_LIMIT = float(os.environ.get("VT_AGI_LIVE_BLEED_LIMIT", "-150"))
# Mínimo de trades live hoje p/ o choque de realidade valer
MIN_TRADES = int(os.environ.get("VT_AGI_REALITY_MIN_TRADES", "3"))


def pair_root(pair: str) -> str:
    """'WIN_M5' → 'WIN'."""
    return pair.split("_", 1)[0] if "_" in pair else pair


def pair_tf(pair: str) -> str:
    """'WIN_M5' → 'M5'."""
    return pair.split("_", 1)[1] if "_" in pair else ""


def _db_path(config: dict) -> Path | None:
    """Mesma resolução de DB do stage1_collect."""
    try:
        from .stage1_collect import _resolve_db_path
        return _resolve_db_path(config)
    except Exception:
        p = Path("/home/bruno/Projects/Vibe-Trading/vt_trades.db")
        return p if p.exists() else None


def _days_util_until(d: date) -> int:
    """Dias úteis de hoje até d (exclusive hoje, inclusive d se útil)."""
    try:
        from core.vt_calendar import is_trading_day
    except Exception:
        return max((d - date.today()).days, 0)
    n = 0
    check = date.today()
    while check < d:
        try:
            ok = is_trading_day(check)[0]
        except Exception:
            ok = check.weekday() < 5
        if ok:
            n += 1
        check += timedelta(days=1)
    return n


def contract_state(sym_root: str, config: dict) -> dict:
    """Estado de rolagem do símbolo: contrato, vencimento, freeze/grace.

    Returns:
        {"symbol", "contract", "expiry", "days_util", "days_since_rollover",
         "rolled_from", "freeze", "grace", "reason"}
    """
    st: dict = {
        "symbol": sym_root, "contract": "", "expiry": None,
        "days_util": None, "days_since_rollover": None, "rolled_from": None,
        "freeze": False, "grace": False, "reason": "",
    }
    resolved = (config.get("resolved_symbols") or {}) if config else {}
    contract = resolved.get(sym_root, "")
    st["contract"] = contract
    if not contract:
        return st

    # ── Vencimento + freeze window ──
    try:
        from core.vt_calendar import get_contract_expiry, _parse_contract_code
        _, m, y = _parse_contract_code(contract)
        if m:
            expiry = get_contract_expiry(sym_root, m, y)
            st["expiry"] = expiry.isoformat()
            st["days_util"] = _days_util_until(expiry)
            if st["days_util"] <= FREEZE_DAYS:
                st["freeze"] = True
                st["reason"] = (f"vencimento {expiry.strftime('%d/%m')} em "
                                f"{st['days_util']} dia(s) útil(eis) (≤ {FREEZE_DAYS})")
    except Exception as e:
        log.warning(f"rollover_guard: calendário falhou p/ {contract}: {e}")
        return st  # fail-safe: sem freeze

    # ── Dias desde a rolagem + grace window ──
    try:
        rlog = (config.get("_rollover_log") or {}).get(sym_root) or {}
        if rlog.get("changed_at"):
            changed = datetime.fromisoformat(str(rlog["changed_at"])[:19]).date()
            st["days_since_rollover"] = (date.today() - changed).days
            st["rolled_from"] = rlog.get("from")
        else:
            # Fallback: primeira trade live do contrato no DB
            db = _db_path(config)
            if db and Path(db).exists():
                conn = sqlite3.connect(str(db))
                try:
                    row = conn.execute(
                        "SELECT min(entry_time) FROM trades WHERE symbol = ?",
                        (contract,),
                    ).fetchone()
                finally:
                    conn.close()
                if row and row[0]:
                    first = datetime.strptime(str(row[0])[:10], "%Y-%m-%d").date()
                    st["days_since_rollover"] = (date.today() - first).days
        dsr = st["days_since_rollover"]
        if dsr is not None and dsr <= GRACE_DAYS:
            st["grace"] = True
            if not st["reason"]:
                origem = f" (de {st['rolled_from']})" if st.get("rolled_from") else ""
                st["reason"] = (f"rolagem{origem} há {dsr} dia(s) — grace "
                                f"{GRACE_DAYS}d sem histórico live do contrato")
    except Exception as e:
        log.warning(f"rollover_guard: grace check falhou p/ {contract}: {e}")

    return st


_series_cache: dict = {}


def allow_changes(pair: str, config: dict) -> tuple[bool, str]:
    """Gate de rolagem do par. Returns (permitido, motivo_do_bloqueio).

    Bloqueios (nesta ordem):
      1. freeze — contrato a ≤ FREEZE_DAYS dias úteis do vencimento
      2. grace — rolagem há ≤ GRACE_DAYS dias (sem histórico live)
      3. series_divergence — MOVIMENTO da série perpétua (base do backtest)
         diverge do contrato live (retorno diário mediano >1%): a simulação
         NÃO representa o que é operado. Wave 883.B5: antes comparava NÍVEL
         e blockava o WIN eternamente pelo basis de rolagem (estrutural);
         agora compara movimento — basis constante some da conta.
    """
    try:
        st = contract_state(pair_root(pair), config)
        if st["freeze"]:
            return False, (f"rollover_freeze: {st['symbol']} {st['contract']} — "
                           f"{st['reason']}")
        if st["grace"]:
            return False, (f"rollover_grace: {st['symbol']} {st['contract']} — "
                           f"{st['reason']}")
        # Sanidade da série (cache por run — fetch MT5 é caro)
        root = pair_root(pair)
        if root not in _series_cache:
            try:
                _series_cache[root] = series_sanity(config).get(root, {})
            except Exception:
                _series_cache[root] = {}
        se = _series_cache.get(root) or {}
        if se.get("divergent"):
            return False, (f"series_divergence: {root} movimento divergente — "
                           f"retorno diário mediano {se.get('ret_diff_med_pct')}% "
                           f"({se.get('n_days')}d comuns, perpétua {se.get('perp_last', 0):.0f} "
                           f"vs {se.get('live_contract', '?')} {se.get('live_last', 0):.0f}) — "
                           f"backtest não representa o contrato operado")
        return True, ""
    except Exception as e:
        log.warning(f"rollover_guard: allow_changes falhou ({e}) — fail-safe libera")
        return True, ""


def live_today_pnl(pair: str, config: dict) -> tuple[float, int]:
    """PnL live de HOJE do par (contrato resolvido + TF), via DB trades.

    Returns (pnl_r$, n_trades). Exclui GHOST. Fail-safe → (0.0, 0).
    """
    root, tf = pair_root(pair), pair_tf(pair)
    resolved = ((config.get("resolved_symbols") or {}).get(root, "")) if config else ""
    if not resolved or not tf:
        return 0.0, 0
    db = _db_path(config)
    if not db or not Path(db).exists():
        return 0.0, 0
    try:
        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute(
                """SELECT COALESCE(sum(net_pnl), 0), count(*) FROM trades
                   WHERE symbol = ? AND timeframe = ?
                     AND date(entry_time) = date('now', 'localtime')
                     AND exit_time IS NOT NULL AND exit_reason != 'GHOST'""",
                (resolved, tf),
            ).fetchone()
        finally:
            conn.close()
        return float(row[0] or 0.0), int(row[1] or 0)
    except Exception as e:
        log.warning(f"rollover_guard: live_today_pnl falhou ({e})")
        return 0.0, 0


def _is_intraday() -> bool:
    """True dentro do horário de decisão intraday (seg-sex 08:55–17:30)."""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return 8 * 60 + 55 <= hm <= 17 * 60 + 30


def reality_check(pair: str, config: dict, cand_today_pnl: float = 0.0,
                  cand_today_n: int = 0) -> tuple[bool, str]:
    """Choque de realidade live do par. Returns (permitido, motivo).

    Bloqueios:
      A. LIVE_BLEEDING — intraday, par perdendo forte hoje: congela churn.
      B. SIM_LIVE_DIVERGENCE — live péssimo com trades suficientes enquanto a
         sim do dia do candidato é ≥ 0: simulação não representa a execução.
    """
    try:
        pnl, n = live_today_pnl(pair, config)
        if n == 0:
            return True, ""
        if pnl <= LIVE_BLEED_LIMIT:
            if _is_intraday():
                return False, (f"live_bleeding: {pair} está R$ {pnl:.2f} hoje "
                               f"({n}t live) — mudanças congeladas durante o pregão")
            if n >= MIN_TRADES and (cand_today_pnl or 0) >= 0 and cand_today_n:
                return False, (f"sim_live_divergence: {pair} live hoje R$ {pnl:.2f} "
                               f"({n}t) vs sim hoje R$ {cand_today_pnl:.2f} "
                               f"({cand_today_n}t) — simulação não representa a "
                               f"execução real; candidato rejeitado")
        return True, ""
    except Exception as e:
        log.warning(f"rollover_guard: reality_check falhou ({e}) — fail-safe libera")
        return True, ""


def all_symbols_state(config: dict) -> dict:
    """Estado de rolagem de todos os símbolos do config (p/ pipeline/report)."""
    out = {}
    for root in (config.get("symbols") or []):
        try:
            out[root] = contract_state(root, config)
        except Exception as e:
            out[root] = {"symbol": root, "error": str(e)}
    return out


# ── Sanidade da série perpétua vs contrato live ──
# Incidente 05-12/08: o AGI otimizava WIN na WIN$ (= WINV26) enquanto o bot
# operava WINQ26, 2.500-4.000 pts abaixo (carry). A simulação não representava
# a execução.
#
# Wave 883.B5 (Bruno 30/08): a comparação por NÍVEL de preço blockava o WIN
# PARA SEMPRE — perpétua costurada carrega basis de rolagem estrutural
# (WIN$ 177.805 vs WINZ26 181.790 = Δ2,19% > 1%), então o gate rejeitava
# candidato bom 5×/run mesmo com as séries descrevendo o MESMO mercado.
# Correção: comparar MOVIMENTO (retorno diário close-a-close), não nível.
# Séries do mesmo ativo com basis constante têm retornos idênticos — o basis
# some da conta. Divergência real (fonte de dados errada, ativo trocado)
# aparece como retorno diário médio diferente → continua bloqueando.
# Freeze e grace (vencimento/rolagem) permanecem intocados.
# Calibração com dado real (24d, 30/08): legítimos ≤0,085% (WIN 0,085;
# BIT/WSP/WDO 0,0) vs ativo ERRADO ≥0,694% (WIN$×WDOU26 0,694; WIN$×BITQ26
# 1,875). Default 0,25% = 3x folga acima do pior legítimo e 2,8x abaixo do
# ativo errado mais sutil. Env sobrepõe.
SERIES_DIVERGENCE_PCT = float(os.environ.get("VT_AGI_SERIES_DIVERGENCE_PCT", "0.0025"))
# Mínimo de dias comuns p/ julgar movimento (menos que isso = sem opinião)
_SERIES_MIN_DAYS = 3


def _daily_returns(df) -> dict:
    """Retorno diário (close-a-close, fração) por data: {date: ret}.

    Usa o ÚLTIMO close de cada dia (série intradiária M15). Sort primeiro —
    bt.load_csv não garante ordem.
    """
    chrono = df.sort_index()
    last_close: dict = {}
    for ts, row in zip(chrono.index, chrono["close"].values):
        last_close[ts.date()] = float(row)
    dates = sorted(last_close)
    out: dict = {}
    for prev, cur in zip(dates, dates[1:]):
        if last_close[prev] > 0:
            out[cur] = last_close[cur] / last_close[prev] - 1.0
    return out


def _series_comparison(dperp, dlive, contract: str) -> dict:
    """Compara perpétua vs contrato: nível (informativo) + movimento (gate).

    divergent = mediana do |Δretorno diário| > SERIES_DIVERGENCE_PCT, com
    >= _SERIES_MIN_DAYS dias comuns. Menos dias → sem opinião (fail-open).
    """
    e: dict = {"perp_last": None, "live_last": None, "live_contract": contract,
               "diff_pts": None, "diff_pct": None, "n_days": None,
               "ret_diff_med_pct": None, "ret_diff_max_pct": None,
               "divergent": False}
    if dperp is None or dlive is None or not len(dperp) or not len(dlive):
        return e
    e["perp_last"] = float(dperp["close"].iloc[-1])
    e["live_last"] = float(dlive["close"].iloc[-1])
    # Nível: informativo (basis de rolagem é ESPERADO, não é divergência)
    diff = e["perp_last"] - e["live_last"]
    if e["live_last"]:
        e["diff_pts"] = round(diff, 1)
        e["diff_pct"] = round(abs(diff) / e["live_last"] * 100, 2)
    # Movimento: o gate de verdade
    try:
        rp = _daily_returns(dperp)
        rl = _daily_returns(dlive)
        common = sorted(set(rp) & set(rl))
        if len(common) < _SERIES_MIN_DAYS:
            return e  # janela pequena (férias/feriados) → sem opinião
        diffs = sorted(abs(rp[d] - rl[d]) * 100 for d in common)
        e["n_days"] = len(common)
        e["ret_diff_med_pct"] = round(diffs[len(diffs) // 2], 3)
        e["ret_diff_max_pct"] = round(diffs[-1], 3)
        e["divergent"] = diffs[len(diffs) // 2] > SERIES_DIVERGENCE_PCT * 100
    except Exception as err:
        e["error_returns"] = str(err)[:120]
    return e


def series_sanity(config: dict) -> dict:
    """Compara perpétua ({root}$) vs contrato resolvido para cada símbolo.

    Wave 883.B5: gate por MOVIMENTO (retorno diário mediano); nível fica
    como diagnóstico. Barras M15 de ambas as séries (mesma fonte/TF) —
    sem tick, que misturava instantes diferentes.

    Returns: {root: entry de _series_comparison}. Fail-safe por símbolo.
    """
    out: dict = {}
    resolved = (config.get("resolved_symbols") or {}) if config else {}
    for root in (config.get("symbols") or []):
        try:
            from backtest import backtest_v944 as bt
            contract = resolved.get(root, "")
            dperp = dlive = None
            if contract:
                p1 = bt.fetch(f"{root}$", "M15", 900)
                dperp = bt.load_csv(p1) if p1 else None
                p2 = bt.fetch(contract, "M15", 900)
                dlive = bt.load_csv(p2) if p2 else None
            out[root] = {"symbol": root, **_series_comparison(dperp, dlive, contract)}
        except Exception as e:
            out[root] = {"symbol": root, "error": str(e)[:120], "divergent": False}
    return out


def format_series_line(entry: dict) -> str:
    """Linha humana do series sanity p/ log."""
    if entry.get("divergent"):
        return (f"  ⚠️ {entry['symbol']}: movimento DIVERGE — retorno diário "
                f"mediano {entry.get('ret_diff_med_pct')}% "
                f"(máx {entry.get('ret_diff_max_pct')}%, {entry.get('n_days')}d) "
                f"> {SERIES_DIVERGENCE_PCT*100:.0f}% — SIMULAÇÃO NÃO REPRESENTA O LIVE")
    if entry.get("ret_diff_med_pct") is not None:
        basis = f", basis {entry.get('diff_pct')}% (normal)" if entry.get("diff_pct") else ""
        return (f"  ✅ {entry['symbol']}: movimento alinhado "
                f"(Δret mediano {entry.get('ret_diff_med_pct')}%, "
                f"{entry.get('n_days')}d{basis})")
    return f"  ❓ {entry['symbol']}: sem dados para comparar série"


def format_state_line(st: dict) -> str:
    """Linha humana de estado p/ log/Telegram."""
    if not st.get("contract"):
        return f"  {st.get('symbol', '?')}: sem contrato resolvido"
    flags = []
    if st.get("freeze"):
        flags.append("🧊 FREEZE")
    if st.get("grace"):
        flags.append("⏳ GRACE")
    flag_s = (" " + " ".join(flags)) if flags else ""
    dsr = st.get("days_since_rollover")
    dsr_s = f", rolagem há {dsr}d" if dsr is not None else ""
    return (f"  {st['symbol']}: {st['contract']} (vence {st.get('expiry', '?')}, "
            f"{st.get('days_util', '?')}d úteis{dsr_s}){flag_s}")
