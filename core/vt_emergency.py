"""
vt_emergency.py — Safety-first: emergency close quando modify_sl falha definitivamente.

CONTEXTO DO BUG (23/06/2026 13:24-13:25):
- Autotrader em loop infinito tentando modificar SL da posição WSPU26 SELL.
- safe_modify_sl em mt5_error_recovery.py tem guard anti-loop (MAX_FIX_ATTEMPTS=3)
  que ABORTA sem fechar a posição.
- manage_position() re-chama safe_modify_sl a cada ciclo (30s) → loop infinito.
- Posição fica aberta sem SL funcional → prejuízo acumulado.

REGRA BRUNO (23/06/2026):
"se o SL não está sendo possível alterar e a operação está indo contra,
pulando SL, deve-se fechar imediatamente a operação para não aumentar a despesa."

SOLUÇÃO:
- safe_modify_sl_with_emergency_close() é um wrapper sobre safe_modify_sl.
- Se safe_modify_sl retorna status != "ok" E posição está contra
  (PnL < 0 OU price contra entry) → fecha IMEDIATAMENTE.
- Notificação CRÍTICA no Telegram (via notify_critical).
- Grava exit_reason='EMERGENCY_CLOSE_SL_FAILED' e close_source='EMERGENCY_CLOSE' no DB.

INTEGRAÇÃO:
- vt_autotrader.py substitui safe_modify_sl() por safe_modify_sl_with_emergency_close()
  em todos os 4 call sites (validator/breakeven/trailing).

NÃO MEXE NO AUTOTRADER RODANDO:
- O wrapper é fail-safe: se qualquer parte da lógica de emergency falhar,
  ainda retorna o resultado original do safe_modify_sl.
"""
from __future__ import annotations

import logging
from typing import Optional

_logger = logging.getLogger("vt.emergency")
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [vt.emergency] %(message)s",
        datefmt="%H:%M:%S",
    ))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)


# Constante exportada — número de fix attempts antes de considerar "falha definitiva".
# Alinhada com MAX_FIX_ATTEMPTS de mt5_error_recovery.safe_modify_sl.
MAX_SL_MODIFY_ATTEMPTS = 3


# ────────────────────────────────────────────────────────────────────
# Wrappers patcháveis — funcionam tanto em runtime quanto em testes
# ────────────────────────────────────────────────────────────────────

def safe_modify_sl(symbol: str, ticket, sl_pts: int, entry_price: float = None,
                    direction: str = None, atr: float = None, **kwargs) -> dict:
    """Wrapper patchável para mt5.mt5_error_recovery.safe_modify_sl."""
    from mt5.mt5_error_recovery import safe_modify_sl as _real
    return _real(symbol, ticket, sl_pts,
                 entry_price=entry_price, direction=direction, atr=atr, **kwargs)


def safe_close(symbol: str) -> dict:
    """Wrapper patchável para mt5.mt5_error_recovery.safe_close."""
    from mt5.mt5_error_recovery import safe_close as _real
    return _real(symbol)


def notify_critical(msg: str, key=None, cooldown_min: float = 0) -> bool:
    """Wrapper patchável para core.vt_notify_log_filter.notify_critical."""
    from core.vt_notify_log_filter import notify_critical as _real
    if key is None:
        return _real(msg)
    return _real(msg, key=key, cooldown_min=cooldown_min)


def notify_silent(msg: str) -> None:
    """Wrapper patchável para core.vt_notify_log_filter.notify_silent."""
    from core.vt_notify_log_filter import notify_silent as _real
    _real(msg)


# ────────────────────────────────────────────────────────────────────
# Detecção de "posição contra"
# ────────────────────────────────────────────────────────────────────

def _get_current_pnl(symbol: str, ticket, direction: str,
                       entry_price: float):
    """
    Retorna PnL atual estimado em R$ para a posição, ou None se indisponível.

    Usa status() do MT5 para pegar profit real. Fallback: estimar
    via tick() se status falhar.

    Wave 880.B6 fix (Bruno 2026-08-05): antes retornava 0.0 em qualquer falha
    de leitura. Como _is_position_against_us tratava pnl <= 0 como "contra",
    um 0.0 de falha era ambíguo (0 <= 0 = True, mas era falha, não pnl real).
    Agora retorna None quando não consegue ler — o caller decide, mas o
    safety-first (fechar em incerteza) é preservado com logging claro.

    Retorna:
        float: PnL em R$ (positivo = lucro, negativo = perda).
        None: não foi possível ler o PnL (MT5 down, posição não encontrada).
    """
    try:
        from mt5.mt5_orchestrator import status as mt5_status
        st = mt5_status()
        positions = st.get("positions", []) if isinstance(st, dict) else []
        for p in positions:
            if str(p.get("ticket", "")) == str(ticket):
                profit = p.get("profit", 0)
                if profit is None:
                    profit = 0
                return float(profit)
        # Posição não encontrada no MT5 (já fechada?) — None, não 0.0
        _logger.warning("_get_current_pnl: posição %s não encontrada no MT5", ticket)
        return None
    except Exception as e:
        _logger.warning("falha ao ler PnL de %s ticket=%s: %s", symbol, ticket, e)

    # Fallback: estimar via tick (sem fees, sem swap, aproximação)
    try:
        from mt5.mt5_orchestrator import tick as mt5_tick
        # Imports lazy de point_val
        from mt5.mt5_error_recovery import _get_point_val
        pv = _get_point_val(symbol)
        tk = mt5_tick(symbol)
        if not tk or not tk.get("bid"):
            return None  # falha de leitura, não 0.0
        # Multiplicador aproximado (R$/ponto). Casa com vt_trade_log.get_multiplier
        from core.vt_trade_log import get_multiplier
        mult = get_multiplier(symbol)
        if direction == "BUY":
            current = tk["bid"]
        else:
            current = tk["ask"]
        gross_pts = (current - entry_price) if direction == "BUY" else (entry_price - current)
        # Converte pts (native) → executor pts → R$
        # gross_pts aqui está em preço (R$). Multiplicador já é R$/executor_pt,
        # mas gross_pts é em unidade de preço. pt = gross_pts / pv (executo pt)
        executor_pts = gross_pts / pv if pv > 0 else gross_pts
        return executor_pts * mult
    except Exception as e:
        _logger.warning("fallback PnL estimate falhou: %s", e)
        return None  # falha, não 0.0


def _netting_container(symbol: str, ticket):
    """Wave 891 (incidente 02/09 10:19): em conta NETTING existe UMA posição
    aberta por símbolo. Se o ticket consultado não está mais em positions_get
    mas há posição aberta do MESMO símbolo com outro ticket, é o container
    netted que absorveu a sub-entrada (consolidação) — e o PnL verdadeiro do
    risco vivo é o DELE.

    Retorna (ticket_pai, profit, price_open) ou None (sem container / falha).
    """
    try:
        from mt5.mt5_orchestrator import status as mt5_status
        st = mt5_status()
        positions = st.get("positions", []) if isinstance(st, dict) else []
        for p in positions:
            if (str(p.get("symbol", "")) == str(symbol)
                    and str(p.get("ticket", "")) != str(ticket)):
                return (p.get("ticket"),
                        float(p.get("profit", 0) or 0),
                        float(p.get("price_open", 0) or 0))
    except Exception as e:
        _logger.warning("_netting_container falhou (%s ticket=%s): %s", symbol, ticket, e)
    return None


def _abs_sl_price(entry_price: float, sl_pts, direction: str,
                  point_val: float) -> float:
    """Preço ABSOLUTO do SL a partir do entry + sl_pts (executor units)."""
    s = float(sl_pts)
    if direction == "BUY":
        return entry_price - s * point_val
    return entry_price + s * point_val


def _is_position_against_us(symbol: str, ticket, direction: str,
                              entry_price: float, current_pnl) -> bool:
    """
    Decide se a posição está indo contra o trader.

    Conservador (safety-first):
    - PnL < 0 → contra (fechar).
    - PnL == 0 (exatamente) → contra (incerteza é perigosa).
    - PnL is None (falha de leitura) → contra (não dá pra afirmar que está a
      favor; fechar é mais seguro que deixar sem SL funcional).
    - PnL > 0 → a favor (não fechar).

    Wave 880.B6 fix (Bruno 2026-08-05): agora distingue None (falha) de 0.0
    (pnl real zero). Ambos resultam em "contra" (safety-first preservado), mas
    o logging é claro sobre qual caso é, para auditoria.

    NÃO tenta heurísticas complexas de price-vs-entry aqui — o PnL do MT5 é
    a fonte da verdade (já inclui fees e swap).
    """
    if current_pnl is None:
        _logger.warning("_is_position_against_us %s ticket=%s: PnL indisponível "
                        "(None) — tratando como CONTRA (safety-first, fechar)", symbol, ticket)
        return True
    return current_pnl <= 0


# ────────────────────────────────────────────────────────────────────
# Emergency close
# ────────────────────────────────────────────────────────────────────

def _emergency_close_position(symbol: str, ticket, trade_log_id: Optional[int],
                               pnl: float, attempts: int,
                               last_error: str) -> dict:
    """
    Fecha a posição a mercado como emergency stop.

    Idempotente: se posição já não existe, retorna already_closed.
    Grava no DB com close_source='EMERGENCY_CLOSE' e exit_reason='EMERGENCY_CLOSE_SL_FAILED'.
    """
    close_result = safe_close(symbol)

    exit_price = None
    if isinstance(close_result, dict):
        exit_price = close_result.get("exit_price") or close_result.get("price")

    status = "unknown"
    if isinstance(close_result, dict):
        if close_result.get("status") == "ok":
            status = "closed"
        elif close_result.get("status") == "already_closed":
            status = "already_closed"
        elif close_result.get("status") == "gone":
            status = "already_closed"
        else:
            status = f"failed:{close_result.get('error', '?')}"

    # Grava DB (se temos trade_log_id)
    if trade_log_id is not None and status in ("closed", "already_closed"):
        try:
            from core.vt_trade_log import log_exit
            # Se já fechou (already_closed), pega preço atual via tick
            if exit_price is None:
                try:
                    from mt5.mt5_orchestrator import tick as mt5_tick
                    tk = mt5_tick(symbol)
                    exit_price = tk.get("bid", 0) if tk else 0
                except Exception:
                    exit_price = 0

            log_exit(
                trade_id=trade_log_id,
                exit_price=exit_price or 0,
                exit_reason="EMERGENCY_CLOSE_SL_FAILED",
                exit_ticket=str(ticket),
                notes=f"Emergency close: {attempts} modify_sl attempts falharam. "
                      f"Último erro: {last_error}. PnL estimado: "
                      f"{('R$ ' + format(pnl, '+.2f')) if pnl is not None else 'indisponível'}.",
                close_source="EMERGENCY_CLOSE",
            )
        except Exception as e:
            _logger.error("falha ao gravar log_exit do emergency close: %s", e)

    return {
        "status": status,
        "exit_price": exit_price,
        "close_result": close_result,
    }


def _notify_critical_emergency(symbol: str, ticket, attempts: int,
                                pnl, last_error: str,
                                exit_price: Optional[float]) -> None:
    """Envia notificação CRÍTICA Telegram com detalhes do emergency close."""
    # Wave 880.B6: pnl pode ser None (falha de leitura do MT5).
    pnl_str = (f"{pnl:+,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
               if pnl is not None else "indisponível")
    exit_str = f"{exit_price:.2f}" if exit_price else "?"

    msg = (
        f"🚨 *EMERGENCY CLOSE* 🚨\n"
        f"• {symbol} ticket={ticket}\n"
        f"• SL não aplicado após {attempts} tentativas\n"
        f"• PnL estimado: R$ {pnl_str}\n"
        f"• Último erro: {last_error}\n"
        f"• Preço de saída: {exit_str}\n"
        f"• Motivo: safety-first — não aumentar prejuízo com SL quebrado\n"
        f"• Verifique posição e logs urgentemente"
    )

    # Usa key por ticket pra dedup (1 close por ticket)
    key = f"EMERGENCY_CLOSE:{ticket}"
    try:
        notify_critical(msg, key=key, cooldown_min=0)  # sem dedup — sempre envia
    except Exception as e:
        _logger.error("falha ao enviar notify_critical: %s", e)


def _notify_silent(msg: str) -> None:
    """Loga warning silencioso (PnL positivo, sem emergency)."""
    try:
        notify_silent(msg)
    except Exception as e:
        _logger.warning("notify_silent falhou: %s — fallback logger: %s", e, msg)


# ────────────────────────────────────────────────────────────────────
# Wrapper principal
# ────────────────────────────────────────────────────────────────────

def safe_modify_sl_with_emergency_close(
    symbol: str,
    ticket,
    sl_pts: int,
    entry_price: float,
    direction: str,
    trade_log_id: Optional[int] = None,
    **kwargs,
) -> dict:
    """
    Wrapper de safe_modify_sl com safety-first: se modify_sl falhar
    definitivamente E posição está contra → fecha IMEDIATAMENTE.

    Args:
        symbol: símbolo MT5 (ex: "WSPU26")
        ticket: ticket da posição
        sl_pts: novo SL em executor pts
        entry_price: preço de entrada
        direction: "BUY" ou "SELL"
        trade_log_id: ID do trade no DB (pra log_exit). Opcional.
        **kwargs: passado para safe_modify_sl (atr, etc)

    Returns:
        dict com chaves:
        - status: "ok" | "aborted" | "emergency_closed"
        - emergency_closed: bool
        - emergency_reason: str (se aplicável)
        - exit_price: float (se emergency_closed)
        - underlying_result: dict (resultado original do safe_modify_sl)
    """
    # 1. Tenta modificar SL (com retries internos do safe_modify_sl)
    try:
        result = safe_modify_sl(
            symbol=symbol,
            ticket=ticket,
            sl_pts=sl_pts,
            entry_price=entry_price,
            direction=direction,
            **kwargs,
        )
    except Exception as e:
        _logger.error("safe_modify_sl lançou exceção: %s — tratando como abort", e)
        result = {"status": "exception", "error": str(e), "attempts": MAX_SL_MODIFY_ATTEMPTS}

    # 2. Se OK → retorna sem emergency
    if isinstance(result, dict) and result.get("status") == "ok":
        return {
            "status": "ok",
            "emergency_closed": False,
            "underlying_result": result,
        }

    # 2b. Wave 10/08 (Bruno 2026-08-10): status "skipped" / "already_applied"
    # NÃO são falha de SL — significam que o SL ANTERIOR (válido) foi mantido
    # (gate de stop_level bloqueou o aperto) ou que o SL pedido já está aplicado.
    # A posição continua protegida → NÃO é emergência, NÃO fecha. Antes, um
    # breakeven de 5pts (dentro do stop_level) gerava Invalid stops → fix
    # reaplicava o SL real → "No changes" → LLM timeout → EMERGENCY CLOSE de
    # posição protegida (2x em 10/08: -R$5 e -R$19 desnecessários).
    if isinstance(result, dict) and result.get("status") in ("skipped", "already_applied"):
        _logger.info(
            "safe_modify_sl %s ticket=%s: %s — SL anterior mantido (posição "
            "protegida), sem emergency close",
            symbol, ticket, result.get("status"),
        )
        return {
            "status": "ok",
            "emergency_closed": False,
            "sl_unchanged": True,
            "underlying_result": result,
        }

    # 2c. Wave 891 (incidente 02/09 10:19): em conta NETTING, quando o ticket
    # do filho é consolidado num PAI aberto, o modify falha com
    # POSITION_NOT_FOUND ("posição fechada") — mas o risco NÃO sumiu: vive na
    # posição netted do símbolo. Regra da casa tightest-SL-wins: reaplicar o
    # MESMO preço absoluto de SL no ticket do pai (convertendo sl_pts do filho
    # → sl_pts do pai via price_open de cada um). Se falhar, cai no fluxo de
    # PnL abaixo, que agora usa o PnL netted do pai como verdade.
    _err_s = str((result or {}).get("error", "")) if isinstance(result, dict) else ""
    if "POSITION_NOT_FOUND" in _err_s or "não encontrado" in _err_s:
        _parent = _netting_container(symbol, ticket)
        if _parent is not None:
            _p_ticket, _p_profit, _p_open = _parent
            _logger.warning(
                "Wave 891 NETTING: %s ticket=%s consolidado no pai %s "
                "(PnL netted R$%+.2f) — reaplicando SL no pai (tightest-SL-wins)",
                symbol, ticket, _p_ticket, _p_profit,
            )
            try:
                from mt5.mt5_error_recovery import _get_point_val
                _pv = _get_point_val(symbol)
                if _pv and _pv > 0 and _p_open > 0:
                    _sl_abs = _abs_sl_price(entry_price, sl_pts, direction, _pv)
                    _p_sl_pts = int(round(((_p_open - _sl_abs) if direction == "BUY"
                                           else (_sl_abs - _p_open)) / _pv))
                    result2 = safe_modify_sl(
                        symbol=symbol, ticket=_p_ticket, sl_pts=_p_sl_pts,
                        entry_price=_p_open, direction=direction, **kwargs,
                    )
                    if isinstance(result2, dict) and result2.get("status") == "ok":
                        _logger.warning(
                            "Wave 891 NETTING: SL do filho aplicado no PAI %s "
                            "(sl_pts=%d → SL≈%.2f) — risco da sub-entrada "
                            "protegido (tightest-SL-wins)",
                            _p_ticket, _p_sl_pts, _sl_abs,
                        )
                        return {
                            "status": "ok",
                            "emergency_closed": False,
                            "adopted_parent": _p_ticket,
                            "underlying_result": result2,
                        }
                    _logger.warning(
                        "Wave 891 NETTING: modify no pai falhou (%s) — seguindo "
                        "para avaliação de PnL netted",
                        (result2 or {}).get("error") if isinstance(result2, dict) else result2,
                    )
            except Exception as e:
                _logger.error("Wave 891 NETTING: adoção do pai falhou: %s", e)

    # 3. Falhou — verifica PnL
    pnl = _get_current_pnl(symbol, ticket, direction, entry_price)
    _netting_note = ""
    if pnl is None:
        # Wave 891: filho consolidado não tem PnL próprio (None) — o verdadeiro
        # é o da posição netted do símbolo. Sem pai legível, None segue → contra
        # (safety-first preservado).
        _parent_pnl = _netting_container(symbol, ticket)
        if _parent_pnl is not None:
            pnl = _parent_pnl[1]
            _netting_note = (f" [netting: ticket {ticket} consolidado no pai "
                             f"{_parent_pnl[0]} — PnL netted R${pnl:+.2f} usado "
                             f"como verdade; close age sobre a posição netted]")
            _logger.warning("Wave 891 %s", _netting_note)
    attempts = result.get("attempts", MAX_SL_MODIFY_ATTEMPTS) if isinstance(result, dict) else MAX_SL_MODIFY_ATTEMPTS
    last_error = result.get("error", "unknown") if isinstance(result, dict) else "unknown"
    if _netting_note:
        last_error = f"{last_error}{_netting_note}"
    # Wave 880.B6: pnl pode ser None (falha de leitura). Formatação None-safe.
    _pnl_str = f"R${pnl:+.2f}" if pnl is not None else "indisponível"

    if not _is_position_against_us(symbol, ticket, direction, entry_price, pnl):
        # Posição a favor — NÃO fecha, apenas loga
        _notify_silent(
            f"[EMERGENCY] modify_sl falhou mas PnL={_pnl_str} (a favor) — "
            f"{symbol} ticket={ticket} mantida. Erro: {last_error}"
        )
        return {
            "status": "aborted",
            "emergency_closed": False,
            "underlying_result": result,
            "pnl": pnl,
        }

    # 4. Posição contra → EMERGENCY CLOSE
    _logger.warning(
        "🚨 EMERGENCY CLOSE: %s ticket=%s | attempts=%d | pnl=%s | erro=%s",
        symbol, ticket, attempts, _pnl_str, last_error,
    )

    close_result = _emergency_close_position(
        symbol=symbol,
        ticket=ticket,
        trade_log_id=trade_log_id,
        pnl=pnl,
        attempts=attempts,
        last_error=last_error,
    )

    exit_price = close_result.get("exit_price") if isinstance(close_result, dict) else None

    # 5. Notifica CRÍTICO
    _notify_critical_emergency(
        symbol=symbol,
        ticket=ticket,
        attempts=attempts,
        pnl=pnl,
        last_error=last_error,
        exit_price=exit_price,
    )

    return {
        "status": "emergency_closed",
        "emergency_closed": True,
        "emergency_reason": "modify_sl_failed_and_position_against_us",
        "pnl": pnl,
        "attempts": attempts,
        "exit_price": exit_price,
        "underlying_result": result,
        "close_result": close_result,
    }
