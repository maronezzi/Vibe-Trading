"""
vt_calendar.py — Calendário B3 + Auto-resolução de vencimento de contratos.

Responsabilidades:
1. Verificar se é dia útil de trading (seg-sex, excluindo feriados)
2. Resolver automaticamente o contrato vigente (ex: WIN → WINM26)
3. Detectar rolagem de contrato (próximo vencimento quando o atual expira)

Feriados B3 2025-2027: feriados nacionais + feriados da bolsa.
Contratos B3: código mês + ano. WIN/IND bimestral (G/J/M/Q/V/Z, quarta ~dia 15);
WDO/DOL mensal (1º dia útil do mês); BIT/WSP mensal (último dia útil do mês).
"""
import logging
import re
import sys
from datetime import date, datetime, timedelta

log = logging.getLogger("vt_calendar")

# ─── Feriados B3 (nacionais + bolsa) ───
# Fonte: B3 oficial — atualizar anualmente
B3_HOLIDAYS = {
    # 2025
    2025: [
        "01-01",  # Confraternização Universal
        "03-03",  # Carnaval (segunda)
        "03-04",  # Carnaval (terça)
        "03-05",  # Cinza (quarta) — B3 fecha meio dia, tratamos como feriado
        "04-18",  # Sexta-feira Santa
        "04-21",  # Tiradentes
        "05-01",  # Dia do Trabalho
        "06-19",  # Corpus Christi
        "09-07",  # Independência (dia útil nacional, mas B3 opera normalmente)
        "10-12",  # Nossa Senhora Aparecida
        "11-02",  # Finados
        "11-15",  # Proclamação da República
        "11-20",  # Consciência Negra (feriado nacional desde 2024)
        "12-24",  # Véspera de Natal (B3 fecha)
        "12-25",  # Natal
        "12-31",  # Véspera de Ano Novo (B3 fecha)
    ],
    # 2026
    2026: [
        "01-01",  # Confraternização Universal
        "02-16",  # Carnaval (segunda)
        "02-17",  # Carnaval (terça)
        "02-18",  # Cinza (quarta)
        "04-03",  # Sexta-feira Santa
        "04-21",  # Tiradentes
        "05-01",  # Dia do Trabalho
        "06-04",  # Corpus Christi
        "09-07",  # Independência
        "10-12",  # Nossa Senhora Aparecida
        "11-02",  # Finados
        "11-15",  # Proclamação da República
        "11-20",  # Consciência Negra
        "12-24",  # Véspera de Natal
        "12-25",  # Natal
        "12-31",  # Véspera de Ano Novo
    ],
    # 2027
    2027: [
        "01-01",  # Confraternização Universal
        "02-08",  # Carnaval (segunda)
        "02-09",  # Carnaval (terça)
        "02-10",  # Cinza (quarta)
        "03-26",  # Sexta-feira Santa
        "04-21",  # Tiradentes
        "05-01",  # Dia do Trabalho
        "05-27",  # Corpus Christi
        "09-07",  # Independência
        "10-12",  # Nossa Senhora Aparecida
        "11-02",  # Finados
        "11-15",  # Proclamação da República
        "11-20",  # Consciência Negra
        "12-24",  # Véspera de Natal
        "12-25",  # Natal
        "12-31",  # Véspera de Ano Novo
    ],
}

# ─── Códigos de mês B3 para contratos ───
# Índice: H=março, M=junho, U=setembro, Z=dezembro
# Mini: F=janeiro, G=fevereiro, H=março, J=abril, K=maio, M=junho,
#        N=julho, Q=agosto, U=setembro, V=outubro, X=novembro, Z=dezembro
MONTH_CODES = {
    1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
    7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z"
}

# Contratos trimestrais (legado — ver EXPIRY_RULES para regras vigentes)
QUARTERLY_MONTHS = {3: "H", 6: "M", 9: "U", 12: "Z"}

# WIN/IND: bimestral, apenas meses PARES (G=fev, J=abr, M=jun, Q=ago, V=out, Z=dez).
# Correção 2026-08-13 (bug histórico): o código tratava WIN como trimestral
# H/M/U/Z com 3ª sexta, calculando vencimento do WINQ26 como 21/08 quando o
# real é 12/08 (quarta-feira mais próxima do dia 15). O bot operou o contrato
# no próprio dia do vencimento (12/08) e o pre-flight reportava "6 dias úteis".
WIN_MONTHS = {2: "G", 4: "J", 6: "M", 8: "Q", 10: "V", 12: "Z"}

# Vencimentos por ativo (regras oficiais B3):
#   bimonthly  → WIN/IND: meses pares, quarta-feira mais próxima do dia 15
#   first_bday → WDO/DOL: 1º dia útil do mês de vencimento (contrato mensal)
#   monthly    → BIT/WSP: último dia útil do mês de vencimento
#   quarterly  → legado (3ª sexta H/M/U/Z) — nenhum ativo usa desde 13/08
EXPIRY_RULES = {
    "WIN": "bimonthly",
    "WDO": "first_bday",
    "IND": "bimonthly",
    "DOL": "first_bday",
    "BIT": "monthly",     # último dia útil do mês
    "WSP": "monthly",     # último dia útil do mês
}


def is_trading_day(d: date = None) -> tuple[bool, str]:
    """
    Verifica se é dia útil de trading na B3.
    Retorna (True/False, motivo).
    """
    if d is None:
        d = date.today()

    # Fim de semana
    if d.weekday() >= 5:
        dia = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"][d.weekday()]
        return False, f"Fim de semana ({dia})"

    # Feriado
    holidays = B3_HOLIDAYS.get(d.year, [])
    date_str = d.strftime("%m-%d")
    if date_str in holidays:
        return False, f"Feriado B3 ({date_str})"

    return True, "Dia útil"


def _third_friday(year: int, month: int) -> date:
    """Retorna a 3ª sexta-feira do mês (regra LEGADA — nenhum ativo usa desde 13/08)."""
    # Primeiro dia do mês
    first = date(year, month, 1)
    # Dia da semana do primeiro dia (0=seg, 4=sex)
    first_weekday = first.weekday()
    # Dias até a primeira sexta
    days_to_friday = (4 - first_weekday) % 7
    first_friday = first + timedelta(days=days_to_friday)
    # Terceira sexta
    third_friday = first_friday + timedelta(weeks=2)
    return third_friday


def _nearest_wednesday_15(year: int, month: int) -> date:
    """Quarta-feira mais próxima do dia 15 (regra B3 oficial de vencimento WIN/IND).

    Ex: ago/2026 → 12/08 (15/08 é sábado; 12/08 está a 3 dias, 19/08 a 4).
    Ex: out/2026 → 14/10 (15/10 é quinta; 14/10 está a 1 dia, 21/10 a 6).
    """
    fifteenth = date(year, month, 15)
    wd = fifteenth.weekday()  # 0=seg, 2=qua, 6=dom
    before = fifteenth - timedelta(days=(wd - 2) % 7)  # quarta anterior/igual
    after = fifteenth + timedelta(days=(2 - wd) % 7)   # quarta posterior/igual
    if abs((after - fifteenth).days) < abs((fifteenth - before).days):
        return after
    return before


def _first_business_day(year: int, month: int) -> date:
    """1º dia útil do mês (regra B3 oficial de vencimento WDO/DOL)."""
    d = date(year, month, 1)
    while d.weekday() >= 5 or not is_trading_day(d)[0]:
        d += timedelta(days=1)
    return d


def _last_business_day(year: int, month: int) -> date:
    """Último dia útil do mês."""
    # Último dia do mês
    if month == 12:
        last = date(year, 12, 31)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)

    # Voltar até achar dia útil
    while last.weekday() >= 5 or not is_trading_day(last)[0]:
        last -= timedelta(days=1)
    return last


def get_contract_expiry(symbol_root: str, contract_month: int, contract_year: int) -> date:
    """
    Retorna a data de vencimento de um contrato.
    symbol_root: WIN, WDO, DOL, BIT, etc.
    """
    rule = EXPIRY_RULES.get(symbol_root, "monthly")

    if rule == "bimonthly":
        return _nearest_wednesday_15(contract_year, contract_month)
    if rule == "first_bday":
        return _first_business_day(contract_year, contract_month)
    if rule == "quarterly":  # legado
        return _third_friday(contract_year, contract_month)
    else:  # monthly (BIT/WSP): último dia útil do mês
        return _last_business_day(contract_year, contract_month)


def _parse_contract_code(symbol: str) -> tuple[str, int, int]:
    """
    Parse contrato: WINM26 → (WIN, M→6, 26→2026)
    Retorna (root, month, year)

    Uses regex to properly separate root from month_code+year.
    The old loop-based parser was broken: it consumed the month letter
    (M, N, U, Z...) as part of the root because they're uppercase too.
    """
    # Letter set must include ALL month codes: F,G,H,J,K,M,N,Q,U,V,X,Z
    m = re.match(r'^([A-Z]+?)([FGHJKMNQUVXZ])(\d{2})$', symbol)
    if not m:
        return symbol, 0, 0
    root, month_char, yy = m.groups()
    year = 2000 + int(yy)
    month = 0
    for m_num, c in MONTH_CODES.items():
        if c == month_char:
            month = m_num
            break
    return root, month, year


def _make_contract_code(symbol_root: str, month: int, year: int) -> str:
    """Cria código do contrato: WIN + mês=6 + ano=2026 → WINM26"""
    month_char = MONTH_CODES.get(month, "Z")
    year_short = year % 100
    return f"{symbol_root}{month_char}{year_short:02d}"


# Sufixos de rollover automático que o MT5 pode retornar quando o contrato
# vigente está prestes a vencer (XP/B3 usa N99, N00, N98, N97 como rollover
# sintético). Esses contratos NÃO têm liquidez real e geram -R$256/30d de
# loss histórico (ver análise DB 2026-06-25: BITM26N99 12t + DOLN26N99 20t).
# Fail-closed: callers devem chamar is_rollover_contract() antes de operar.
ROLLOVER_SUFFIX_PATTERN = re.compile(r"N(99|00|98|97)$")


def is_rollover_contract(symbol: str) -> bool:
    """Retorna True se o symbol é um rollover automático (N99/N00/N98/N97).

    Esses contratos aparecem quando o MT5 retorna o ticker do próximo
    vencimento antes da virada oficial. Não têm liquidez real. Operá-los
    gera fills fantasma + PnL não-realizado. Use em check_and_trade()
    para fail-closed: rejeitar antes de chamar log_entry.
    """
    if not symbol:
        return False
    return bool(ROLLOVER_SUFFIX_PATTERN.search(symbol))


def _get_next_expiry_month(symbol_root: str, after_date: date = None) -> tuple[int, int]:
    """
    Retorna o próximo mês de vencimento disponível para o ativo.
    (month, year)
    """
    if after_date is None:
        after_date = date.today()

    rule = EXPIRY_RULES.get(symbol_root, "monthly")
    months = sorted(WIN_MONTHS.keys()) if rule == "bimonthly" else list(range(1, 13))

    for i in range(24):
        m = ((after_date.month - 1 + i) % 12) + 1
        y = after_date.year + ((after_date.month - 1 + i) // 12)
        if m in months and get_contract_expiry(symbol_root, m, y) > after_date:
            return m, y
    return months[0], after_date.year + 1


def _check_contract_spread(symbol: str) -> float:
    """Retorna o spread atual do contrato. Quanto menor, melhor. Retorna 999 se falhar.
    Spread=0 indica contrato sem cotação (sem liquidez) — descartado.
    """
    try:
        from mt5_orchestrator import _run_wine, EXECUTOR_WIN
        result = _run_wine(EXECUTOR_WIN, "info", symbol, timeout=10)
        if isinstance(result, dict):
            spread = float(result.get("spread", 999))
            bid = float(result.get("bid", 0))
            ask = float(result.get("ask", 0))
            # Sem cotação real (bid/ask = 0) ou spread = 0 = sem liquidez
            if spread <= 0 or bid <= 0 or ask <= 0:
                return 999.0
            return spread
    except Exception:
        pass
    return 999.0


# Rolagem: quantos dias úteis antes do vencimento o contrato atual deixa de ser
# "viável" e deve ser trocado. Com ROLL_DAYS=2, o contrato atual é mantido
# enquanto tiver > 2 dias úteis até o vencimento (e mantiver liquidez) — elimina
# a rolagem prematura (ex: trocar um contrato com 44 dias de vida).
ROLL_DAYS = 2


def resolve_symbol(symbol_root: str, force_check: bool = False) -> str:
    """
    Resolve automaticamente o contrato vigente para o symbol_root.

    Hierarquia de decisão (determinística e estável — elimina a divergência
    config↔runtime e a rolagem prematura):

      1. ESTABILIDADE: se o contrato ATUAL está líquido (spread < 999 no MT5)
         com > ROLL_DAYS dias úteis até o vencimento, MANTÉM o atual.
      2. ROLAGEM: escolhe o candidato mais próximo com liquidez real.
         Meses candidatos dependem da regra do ativo (EXPIRY_RULES):
         - WIN/IND (bimonthly): meses pares G/J/M/Q/V/Z (quarta ~dia 15)
         - WDO/DOL (first_bday): todos os meses (1º dia útil do mês)
         - BIT/WSP (monthly): todos os meses (último dia útil do mês)
         Sempre o vencimento mais próximo (NÃO o de menor spread) —
         determinístico e respeita a progressão natural do contrato.
      3. Fallback: mantém o atual se nada líquido foi encontrado.

    Retorna o código do contrato (ex: WINV26, WDOU26, INDM26).
    """
    from vt_config_loader import load_config

    config = load_config()
    resolved = config.get("resolved_symbols", {})
    current = resolved.get(symbol_root, "")

    today = date.today()
    rule = EXPIRY_RULES.get(symbol_root, "monthly")

    # ─── Meses candidatos (ordem de vencimento) ───
    months = sorted(WIN_MONTHS.keys()) if rule == "bimonthly" else list(range(1, 13))
    max_candidates = 4 if rule == "bimonthly" else 6
    candidates: list[tuple[int, int]] = []
    for i in range(24):
        m = ((today.month - 1 + i) % 12) + 1
        y = today.year + ((today.month - 1 + i) // 12)
        if m in months:
            candidates.append((m, y))
        if len(candidates) >= max_candidates:
            break

    def _info(m: int, y: int) -> dict | None:
        """Contrato + vencimento + dias úteis + spread real (MT5) de (mês, ano)."""
        contract = _make_contract_code(symbol_root, m, y)
        try:
            expiry = get_contract_expiry(symbol_root, m, y)
        except Exception:
            return None
        days_util = 0
        check = today
        while check < expiry:
            check += timedelta(days=1)
            if is_trading_day(check)[0]:
                days_util += 1
        spread = _check_contract_spread(contract)
        return {
            "contract": contract, "month": m, "year": y,
            "expiry": expiry, "days_util": days_util, "spread": spread,
        }

    # ─── 1. ESTABILIDADE: honrar o config enquanto o contrato é viável ───
    # Só saímos do contrato atual quando ele está a ≤ ROLL_DAYS dias úteis do
    # vencimento OU perdeu liquidez (spread = 999). Isto elimina a divergência
    # config↔runtime e a rolagem prematura.
    if current:
        _, cur_m, cur_y = _parse_contract_code(current)
        if cur_m:
            cur = _info(cur_m, cur_y)
            if cur and cur["spread"] < 999 and cur["days_util"] > ROLL_DAYS:
                return current

    # ─── 2. ROLAGEM: candidato mais próximo com liquidez ───
    viable = [c for c in (_info(m, y) for m, y in candidates)
              if c and c["spread"] < 999 and c["days_util"] > 0]
    viable.sort(key=lambda c: c["expiry"])
    if viable:
        return viable[0]["contract"]

    # ─── 3. Fallback final: nada líquido encontrado ───
    if current:
        return current
    if candidates:
        m, y = candidates[0]
        return _make_contract_code(symbol_root, m, y)
    return current or symbol_root


def _check_contract_liquidity(symbol: str) -> bool:
    """Verifica no MT5 se o contrato existe e tem volume."""
    try:
        from mt5_orchestrator import _run_wine, EXECUTOR_WIN
        result = _run_wine(EXECUTOR_WIN, "symbols", symbol[:3], timeout=15)
        if isinstance(result, list):
            for s in result:
                if s.get("name") == symbol:
                    return True
        elif isinstance(result, dict) and "error" not in result:
            return True
    except Exception:
        pass
    return False


def resolve_all_symbols(persist: bool = False) -> dict:
    """
    Resolve todos os símbolos configurados.
    Retorna dict: {"WIN": "WINM26", ...}

    Args:
        persist: se True e houver mudança de contrato, escreve em
            vt_config.json via save_full_config (chamador precisa estar
            na whitelist de ALLOWED_WRITERS). Default False (read-only).

    Por design (Bruno 2026-07-01 — incidente 09h30 comeu 95% do config
    porque um caller reescreveu o JSON inteiro durante startup):
    DEFAULT READ-ONLY. Persistir em disco durante runtime do autotrader
    é PERIGOSO. Quem precisa persistir (pre-flight 8h55) chama com
    persist=True explicitamente — o módulo pre-flight já está na
    whitelist de ALLOWED_WRITERS.
    """
    from vt_config_loader import load_config

    config = load_config()
    symbols = config.get("symbols", [])
    current = config.get("resolved_symbols", {})
    updated = {}
    changed = []

    changed_map: dict[str, dict] = {}
    for root in symbols:
        resolved = resolve_symbol(root)
        updated[root] = resolved

        if resolved != current.get(root):
            changed.append(f"{root}: {current.get(root, '?')} → {resolved}")
            changed_map[root] = {"from": current.get(root, ""), "to": resolved}

    if changed:
        if persist:
            # Atualizar config (chamador precisa estar em ALLOWED_WRITERS)
            config["resolved_symbols"] = updated
            config["_notes"] = f"auto-resolve vencimento: {', '.join(changed)}"
            # Wave AGI-rollover (Bruno 2026-08-13): registrar a DATA da rolagem.
            # O AGI (optimization/agi_v4/rollover_guard) lê _rollover_log para
            # o grace period: não decidir sobre um símbolo cujo contrato trocou
            # há ≤ GRACE_DAYS dias (não há histórico live honesto ainda).
            try:
                from datetime import datetime as _dt
                _rlog = dict(config.get("_rollover_log") or {})
                for _root, _ch in changed_map.items():
                    if _ch.get("from"):  # só rolagem real (não o 1º resolve)
                        _rlog[_root] = {
                            "from": _ch["from"], "to": _ch["to"],
                            "changed_at": _dt.now().isoformat(timespec="seconds"),
                            "changed_by": "calendar_resolve",
                        }
                if _rlog:
                    config["_rollover_log"] = _rlog
            except Exception:
                pass
            _save_config(config)
            _notify("📅 Rolagem de contrato detectada!\n" + "\n".join(changed))
        else:
            # Read-only: apenas loga a mudança detectada, NÃO toca disco
            # (defesa contra regressões — incidente 2026-07-01 09h30).
            import logging
            _cal_log = logging.getLogger("vt_calendar")
            _cal_log.info(
                f"[resolve_all_symbols] mudanças detectadas (NÃO persistidas "
                f"porque persist=False): {', '.join(changed)}"
            )

    return updated


def _save_config(config: dict):
    """Salva config atualizado (escrita atômica via config_loader)."""
    from vt_config_loader import save_full_config
    save_full_config(config, updated_by="calendar_resolve")


def _notify(msg: str):
    """Notifica Telegram."""
    try:
        from vt_hermes_helper import hermes_send
        hermes_send("telegram:-1004284773048", msg, timeout=15)
    except Exception:
        pass


def get_trading_calendar(days: int = 10) -> list[dict]:
    """Retorna os próximos N dias com status de trading."""
    today = date.today()
    calendar = []
    for i in range(days):
        d = today + timedelta(days=i)
        ok, motivo = is_trading_day(d)

        # Verificar vencimentos nesse dia
        expiries = []
        for root in ["WIN", "WDO", "IND", "DOL", "BIT", "WSP"]:
            rule = EXPIRY_RULES.get(root, "monthly")
            months = sorted(WIN_MONTHS.keys()) if rule == "bimonthly" else list(range(1, 13))
            for m in months:
                try:
                    if get_contract_expiry(root, m, d.year) == d:
                        expiries.append(root)
                        break
                except Exception:
                    continue

        calendar.append({
            "date": d.strftime("%d/%m/%Y (%a)"),
            "trading": ok,
            "reason": motivo,
            "expiries": expiries if expiries else None,
        })
    return calendar


# ══════════════════════════════════════════════════════════════════════
# Wave N+4A (2026-07-08): aggregate_blackout — gate unificado.
# Substitui _is_blocked_day_direction + _is_blocked_time + events spread
# (estavam em core/vt_autotrader.py antes). Mais simples testar e evoluir.
# ══════════════════════════════════════════════════════════════════════

def _now_or_dt(ts):
    """Aceita None (agora), datetime, ISO string, ou epoch int/float.

    Retorna datetime NAIVE (sem timezone) para comparações internas.
    """
    if ts is None:
        return datetime.now()
    if isinstance(ts, str):
        return datetime.fromisoformat(ts)
    # MT5 retorna epoch int/float → converter pra datetime naive.
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts)
    # Se vier timezone-aware, strip timezone pra comparar naive.
    if isinstance(ts, datetime) and ts.tzinfo is not None:
        return ts.replace(tzinfo=None)
    return ts


def _symbol_root(symbol: str) -> str:
    for r in ("WIN", "WDO", "BIT", "DOL", "IND", "WSP"):
        if r in symbol:
            return r
    return symbol


def aggregate_blackout(
    symbol: str,
    side: str,
    *,
    config: dict | None = None,
    ts=None,
) -> tuple[bool, str]:
    """Wave N+4A (2026-07-08): gate único de blackout.

    Composição (todas as checks, primeiro hit vence):
      1. is_trading_day() — feriado nacional/weekend → bloqueia BUY+SELL.
      2. blocked_day_directions: [[weekday_int, "BUY"|"SELL"]] para dia da semana.
      3. time_blocks: por (symbol_root) + hora → [{start, end, strategy?, reason}].
      4. events: lista de news (IPCA, BCB, FOMC, payrolls) com janela ±window_min
         e side match.

    Args:
        symbol: contrato resolvido (ex: "WINQ26") ou root.
        side: "BUY" | "SELL".
        config: vt_config dict (snapshot).
        ts: datetime (default now) ou ISO string. None = agora.

    Returns:
        (is_blocked: bool, reason: str). reason composto (separado por ";").
        Se liberado, retorna (False, "").
    """
    config = config or {}
    dt = _now_or_dt(ts)
    root = _symbol_root(symbol)

    parts = []

    # 1. Trading day (feriados, fim de semana).
    try:
        is_business, td_reason = is_trading_day(dt.date())
        if not is_business:
            parts.append(f"trading_day:{td_reason or 'holiday'}")
    except Exception as exc:
        log.debug(f"aggregate_blackout: is_trading_day falhou: {exc!r}")

    # 2. blocked_day_directions.
    weekday = dt.weekday()  # 0=Mon
    for entry in (config.get("blocked_day_directions") or []):
        try:
            wd, blocked_side = entry[0], entry[1]
        except Exception:
            continue
        if wd == weekday and blocked_side == side:
            parts.append(f"day_dir:{wd}:{blocked_side}")
            break

    # 3. time_blocks.
    hour = dt.hour
    for tb in (config.get("time_blocks", {}).get(root) or []):
        try:
            start, end = tb["start"], tb["end"]
        except Exception:
            continue
        # Suporta wrap noturno (start > end = cruza meia-noite).
        if start <= end:
            in_window = start <= hour < end
        else:
            in_window = hour >= start or hour < end
        if in_window:
            reason_extra = tb.get("reason") or ""
            extra = f":{reason_extra}" if reason_extra else ""
            parts.append(f"time_block:{root}:{start}-{end}{extra}")
            break

    # 4. events (news).
    for ev in (config.get("events") or []):
        try:
            ev_ts_str = ev.get("ts", "")
            ev_symbol = ev.get("symbol", "")
            ev_side = ev.get("side")
            ev_window = int(ev.get("window_min", 30))
        except Exception:
            continue
        try:
            ev_dt = datetime.fromisoformat(ev_ts_str)
            if ev_dt.tzinfo is not None:
                ev_dt = ev_dt.replace(tzinfo=None)
        except Exception:
            continue
        # Match de symbol (ou ALL para todos).
        if ev_symbol and ev_symbol != root and ev_symbol.upper() != "ALL":
            continue
        # Match de side (None = ambos).
        if ev_side and ev_side != side:
            continue
        delta_min = abs((dt - ev_dt).total_seconds() / 60)
        if delta_min <= ev_window:
            sev = ev.get("severity", "")
            source = ev.get("source", "")
            parts.append(f"event:{source or 'unknown'}:{sev}+-{ev_window}min")
            break

    if parts:
        return True, ";".join(parts)
    return False, ""


if __name__ == "__main__":
    # Teste rápido
    import sys

    if "--calendar" in sys.argv:
        cal = get_trading_calendar(15)
        for d in cal:
            status = "✅" if d["trading"] else "❌"
            exp = f" 📅 Venc: {d['expiries']}" if d["expiries"] else ""
            print(f"{status} {d['date']} — {d['reason']}{exp}")

    elif "--resolve" in sys.argv:
        for root in ["WIN", "BIT", "DOL", "IND", "WSP"]:
            contract = resolve_symbol(root)
            print(f"{root} → {contract}")

    elif "--today" in sys.argv:
        ok, motivo = is_trading_day()
        print(f"Hoje: {'✅ Trading' if ok else '❌ ' + motivo}")
        if ok:
            for root in ["WIN", "BIT", "DOL", "IND", "WSP"]:
                contract = resolve_symbol(root)
                _, month, year = _parse_contract_code(contract)
                if month:
                    expiry = get_contract_expiry(root, month, year)
                    days = 0
                    check = date.today()
                    while check < expiry:
                        if is_trading_day(check)[0]:
                            days += 1
                        check += timedelta(days=1)
                    print(f"  {root} → {contract} (vence {expiry.strftime('%d/%m')}, {days} dias úteis)")
                else:
                    print(f"  {root} → {contract}")

    else:
        print("Uso: python vt_calendar.py --calendar | --resolve | --today")
