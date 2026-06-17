"""
vt_calendar.py — Calendário B3 + Auto-resolução de vencimento de contratos.

Responsabilidades:
1. Verificar se é dia útil de trading (seg-sex, excluindo feriados)
2. Resolver automaticamente o contrato vigente (ex: WIN → WINM26)
3. Detectar rolagem de contrato (próximo vencimento quando o atual expira)

Feriados B3 2025-2027: feriados nacionais + feriados da bolsa.
Contratos B3: código mês + ano (H=março, J=junho, M=setembro, Z=dezembro)
"""
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

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

# Contratos trimestrais (índice, dólar, etc.)
QUARTERLY_MONTHS = {3: "H", 6: "M", 9: "U", 12: "Z"}

# Vencimentos por ativo (dia do mês do vencimento)
# Índice/Dólar: 3ª sexta do mês de vencimento
# Mini índice/Mini dólar: mesmo dia
# BIT: último dia útil do mês anterior ao vencimento
EXPIRY_RULES = {
    "WIN": "quarterly",   # 3ª sexta de H, M, U, Z
    "WDO": "quarterly",
    "IND": "quarterly",
    "DOL": "quarterly",
    "BIT": "monthly",     # último dia útil do mês anterior
    "WSP": "monthly",     # último dia útil do mês anterior
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
    """Retorna a 3ª sexta-feira do mês (regra de vencimento B3 para índice/dólar)."""
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
    symbol_root: WIN, DOL, BIT, etc.
    """
    rule = EXPIRY_RULES.get(symbol_root, "quarterly")

    if rule == "quarterly":
        return _third_friday(contract_year, contract_month)
    else:  # monthly
        # BIT/WSP vencem no último dia útil do mês
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


def _get_next_expiry_month(symbol_root: str, after_date: date = None) -> tuple[int, int]:
    """
    Retorna o próximo mês de vencimento disponível para o ativo.
    (month, year)
    """
    if after_date is None:
        after_date = date.today()

    rule = EXPIRY_RULES.get(symbol_root, "quarterly")

    if rule == "quarterly":
        # Vencimentos trimestrais: H(Mar), M(Jun), U(Sep), Z(Dec)
        quarterly = [3, 6, 9, 12]
        year = after_date.year
        for m in quarterly:
            expiry = _third_friday(year, m)
            if expiry > after_date:
                return m, year
        # Próximo ano
        return quarterly[0], year + 1
    else:
        # Mensal: próximo mês
        year = after_date.year
        month = after_date.month + 1
        if month > 12:
            month = 1
            year += 1
        return month, year


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


def resolve_symbol(symbol_root: str, force_check: bool = False) -> str:
    """
    Resolve automaticamente o contrato vigente para o symbol_root.

    Lógica:
    1. Verifica se o contrato atual (do config) ainda está vigente
    2. Se sim, compara spread com o próximo contrato — escolhe o de menor spread
    3. Se contrato atual está perto de vencer (< 3 dias úteis), migra para o próximo
    4. Confirma no MT5 que o contrato existe e tem liquidez

    Critério: escaneia TODOS os próximos 6 contratos a partir do mês atual
    e escolhe o de menor spread real (MT5). Mantém o atual se ele ainda tiver
    cotação, > 3 dias úteis, e spread <= melhor alternativa.

    Retorna o código do contrato (ex: WINQ26, WDON26, INDM26).
    """
    from vt_config_loader import load_config

    config = load_config()
    resolved = config.get("resolved_symbols", {})
    current = resolved.get(symbol_root, "")

    # Lista de candidatos: 6 meses consecutivos a partir do mês atual
    # (cobre tanto trimestrais H/M/U/Z quanto mensais N/Q)
    today = date.today()
    candidates = []
    for i in range(6):
        m = ((today.month - 1 + i) % 12) + 1
        y = today.year + ((today.month - 1 + i) // 12)
        candidates.append((m, y))

    # Para cada candidato, busca contrato + spread + dias úteis
    candidate_info = []
    for m, y in candidates:
        contract = _make_contract_code(symbol_root, m, y)
        try:
            expiry = get_contract_expiry(symbol_root, m, y)
        except Exception:
            continue
        days_util = 0
        check = today
        while check < expiry:
            check += timedelta(days=1)
            if is_trading_day(check)[0]:
                days_util += 1
        spread = _check_contract_spread(contract)
        candidate_info.append({
            "contract": contract,
            "month": m,
            "year": y,
            "expiry": expiry,
            "days_util": days_util,
            "spread": spread,
        })

    # Filtra apenas contratos com cotação real (spread < 999) e > 0 dias úteis
    # (não opera no último dia útil, mas permite se < 3 dias se não há alternativa)
    liquidos_3d = [c for c in candidate_info if c["spread"] < 999 and c["days_util"] > 3]
    liquidos_1d = [c for c in candidate_info if c["spread"] < 999 and c["days_util"] > 0]

    # Caso ideal: liquidez + 3+ dias úteis (evita surpresa de rollover)
    if liquidos_3d:
        liquidos = liquidos_3d
    else:
        # Fallback: aceita mesmo < 3 dias (mas não no último dia)
        liquidos = liquidos_1d

    if not liquidos:
        # Nenhum contrato líquido — fallback para o config atual
        if current:
            return current
        month, year = candidates[0]
        return _make_contract_code(symbol_root, month, year)

    # Ordena por spread (menor é melhor)
    liquidos.sort(key=lambda c: c["spread"])
    best = liquidos[0]

    # Se o atual está nos líquidos e tem spread <= best, manter (estabilidade)
    if current:
        current_match = next((c for c in liquidos if c["contract"] == current), None)
        if current_match and current_match["spread"] <= best["spread"] * 1.5:
            # Atual é aceitável (não é 50% pior que o melhor)
            return current

    return best["contract"]


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


def resolve_all_symbols() -> dict:
    """
    Resolve todos os símbolos configurados.
    Retorna dict: {"WIN": "WINM26", ...}
    Também atualiza o config se houver mudança de contrato.
    """
    from vt_config_loader import load_config
    
    config = load_config()
    symbols = config.get("symbols", [])
    current = config.get("resolved_symbols", {})
    updated = {}
    changed = []

    for root in symbols:
        resolved = resolve_symbol(root)
        updated[root] = resolved
        
        if resolved != current.get(root):
            changed.append(f"{root}: {current.get(root, '?')} → {resolved}")

    if changed:
        # Atualizar config
        config["resolved_symbols"] = updated
        config["_notes"] = f"auto-resolve vencimento: {', '.join(changed)}"
        _save_config(config)
        _notify(f"📅 Rolagem de contrato detectada!\n" + "\n".join(changed))

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
            rule = EXPIRY_RULES.get(root, "quarterly")
            if rule == "quarterly":
                for m in [3, 6, 9, 12]:
                    if _third_friday(d.year, m) == d:
                        expiries.append(root)
            else:
                if _last_business_day(d.year, d.month) == d and d == _last_business_day(d.year, d.month):
                    expiries.append(root)
        
        calendar.append({
            "date": d.strftime("%d/%m/%Y (%a)"),
            "trading": ok,
            "reason": motivo,
            "expiries": expiries if expiries else None,
        })
    return calendar


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
