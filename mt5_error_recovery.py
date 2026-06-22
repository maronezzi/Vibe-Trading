"""
mt5_error_recovery.py — Auto-recuperação de erros do MT5 com LLM.

Fluxo:
1. Erro detectado → tenta fix padrão (baseado em padrão)
2. Fix padrão falha → consulta LLM Hermes para diagnóstico + correção
3. LLM sugere correção → aplica automaticamente
4. Tudo falha → notifica Telegram com detalhes para intervenção manual

Erros tratados:
- Invalid stops → recalcula SL com preço atual + margin mínima
- Requote → busca novo tick e reenvia
- No connection → aguarda e reconecta
- Insufficient margin → reduz volume
- Invalid volume → ajusta ao step/min
- Position not found → sincroniza state com MT5
- Unknown → consulta LLM
"""
import time
import json
import subprocess
import traceback
from datetime import datetime


# ─── Config ───
MAX_RETRIES = 3
RETRY_DELAY = 0.5
LLM_TIMEOUT = 30
NOTIFY_ALL_FIXES = True

# ─── Error classifiers ───
ERROR_PATTERNS = {
    "INVALID_STOPS": ["Invalid stops", "invalid stops", "stops"],
    "REQUOTE": ["Requote", "requote", "prices changed"],
    "NO_CONNECTION": ["No connection", "connection", "timeout", "Timeout"],
    "INSUFFICIENT_MARGIN": ["margem insuficiente", "insufficient margin", "margin"],
    "INVALID_VOLUME": ["Invalid volume", "volume", "Invalid amount"],
    "POSITION_NOT_FOUND": ["não encontrado", "not found", "sem posições"],
    "MARKET_CLOSED": ["market closed", "mercado fechado", "trade disabled"],
    "FILLING": ["Unsupported filling", "filling mode"],
}


def _classify_error(error_msg: str) -> str:
    if not error_msg:
        return "UNKNOWN"
    msg_lower = str(error_msg).lower()
    for err_type, patterns in ERROR_PATTERNS.items():
        for pat in patterns:
            if pat.lower() in msg_lower:
                return err_type
    return "UNKNOWN"


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [RECOVERY] {msg}", flush=True)


# ─── LLM Integration ───

def _ask_llm(prompt: str, timeout: int = None) -> dict:
    """Consulta LLM Hermes. Retorna dict parsed ou {'error': ...}."""
    if timeout is None:
        timeout = LLM_TIMEOUT
    try:
        # Provider: minimax-oauth (ativo no Hermes), fallback automático
        result = subprocess.run(
            ["hermes", "-z", prompt, "-m", "MiniMax-M3", "--provider", "minimax-oauth"],
            capture_output=True, text=True, timeout=timeout,
            env={**__import__('os').environ, "WINEDEBUG": "-all"}
        )
        if result.returncode == 0 and result.stdout.strip():
            raw = result.stdout.strip()
            # Tenta parsear como JSON
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"text": raw}
        return {"error": result.stderr[:200] if result.stderr else "empty response"}
    except subprocess.TimeoutExpired:
        return {"error": "LLM timeout"}
    except Exception as e:
        return {"error": str(e)}


def _llm_diagnose_error(context: dict) -> dict:
    """Pede ao LLM para diagnosticar o erro e sugerir correção."""
    prompt = f"""Você é um especialista em trading com MetaTrader 5. Analise este erro e sugira uma correção.

CONTEXTO DO ERRO:
- Ação: {context.get('action', '?')}
- Símbolo: {context.get('symbol', '?')}
- Direção: {context.get('direction', '?')}
- Erro MT5: {context.get('error', '?')}
- Tipo classificado: {context.get('error_type', '?')}
- SL atual (pts): {context.get('sl_pts', '?')}
- Entry price: {context.get('entry_price', '?')}
- Volume: {context.get('volume', '?')}
- Tentativa: {context.get('attempt', '?')}/{MAX_RETRIES}
- Ticket: {context.get('ticket', '?')}

CONTEXTO DO MERCADO:
- Preço atual bid: {context.get('current_bid', '?')}
- Preço atual ask: {context.get('current_ask', '?')}
- ATR: {context.get('atr', '?')}
- Point value: {context.get('point_val', '?')}

Responda APENAS com JSON (sem markdown):
{{
  "diagnosis": "explicação curta do problema",
  "action": "nome da correção",
  "params": {{
    "sl_pts": novo_sl_pts_ou_null,
    "volume": novo_volume_ou_null,
    "retry": true_ou_false,
    "abort": true_ou_false
  }},
  "reasoning": "por que essa correção deve funcionar"
}}"""

    _log(f"Consultando LLM para erro {context.get('error_type', '?')}...")
    response = _ask_llm(prompt, timeout=20)

    if "error" in response and "text" not in response:
        _log(f"LLM falhou: {response['error']}")
        return {"action": "none", "params": {"retry": False, "abort": True}}

    # Parse response
    raw = response.get("text", "")
    if not raw:
        raw = json.dumps(response)

    # Try extract JSON from response
    try:
        # Remove markdown fences if present
        clean = raw.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[-1]
        if clean.endswith("```"):
            clean = clean.rsplit("```", 1)[0]
        if clean.startswith("```"):
            clean = clean[3:]

        parsed = json.loads(clean.strip())
        _log(f"LLM diagnosticou: {parsed.get('diagnosis', '?')}")
        _log(f"LLM sugere: {parsed.get('action', '?')} → {parsed.get('params', {})}")
        return parsed
    except json.JSONDecodeError:
        _log(f"LLM resposta não-JSON: {raw[:200]}")
        return {"action": "none", "params": {"retry": False, "abort": True}, "raw": raw}


# ─── Fix strategies (pattern-based, fast) ───

def _fix_invalid_stops(symbol: str, side: str, sl_pts: int, point_val: float, tick_data: dict) -> int:
    from mt5_orchestrator import info
    info_data = info(symbol)
    if not info_data or "error" in info_data:
        return int(sl_pts * 1.5)
    stops_level = info_data.get("trade_stops_level", 0)
    spread = info_data.get("spread", 0)
    # Converter stops_level e spread para executor units (sl_pts já está em executor units)
    _pv = point_val if point_val and point_val > 0 else 1.0
    min_distance_pts = max(stops_level / _pv, (spread + 5) / _pv)
    if sl_pts < min_distance_pts:
        new_sl = int(min_distance_pts * 1.5)
        _log(f"SL curto ({sl_pts}pts < {min_distance_pts:.0f}pts). Aumentando para {new_sl}pts")
        return new_sl
    return int(sl_pts * 1.5)


# Tabela point_mult (executor pts → 1 unidade de preço em R$) por símbolo.
# Deve casar com _specs em vt_autotrader.py. Garante 1 native pt = 1 executor pt
# (point * point_mult = 1.0 para todos). Usado por _fix_invalid_stops_modify
# pra retornar valores em EXECUTOR PTS, consistentes com a convenção do sistema.
_POINT_MULT = {
    "WIN": 1, "WDO": 1000, "BIT": 100, "DOL": 1000, "IND": 1, "WSP": 100,
}


def _get_point_mult(symbol: str) -> int:
    for prefix, mult in _POINT_MULT.items():
        if prefix in symbol:
            return mult
    return 1


def _fix_invalid_stops_modify(symbol: str, ticket: str, sl_pts: int, point_val: float,
                               entry_price: float, direction: str) -> int:
    """Calcula novo SL quando MODIFY falha com 'Invalid stops'.

    Retorna SEMPRE em EXECUTOR PTS, consistente com a entrada (sl_pts) e com
    o que cmd_modify() em mt5_executor.py espera. Como 1 executor pt = 1 native
    pt (point * point_mult = 1.0), a conversão é dividir por (point_val * point_mult)
    que é 1.0 para todos os símbolos — mas mantemos a fórmula explícita para
    legibilidade e futura tolerância a símbolos onde isso não seja 1.

    Bug original (jun/2026): a função dividia só por point_val, retornando native
    pts. O safe_modify_sl() tratava como executor pts, e o próximo modify fazia
    `* point` de novo, gerando SLs 100x (BIT), 1000x (WDO/DOL), 1x (WIN/IND)
    maiores do que deveriam. Resultado: 3 retries com SL crescendo
    exponencialmente, todos rejeitados, trade ficava com SL original errado.
    """
    from mt5_orchestrator import tick, info
    tick_data = tick(symbol)
    if not tick_data or tick_data.get("bid", 0) == 0:
        return sl_pts
    current = tick_data["bid"] if direction == "BUY" else tick_data["ask"]

    # Multiplicador pra converter R$ → executor pts. 1 native pt = 1 executor pt
    # por design do sistema, mas mantemos a divisão explícita.
    pts_per_unit = point_val * _get_point_mult(symbol)  # = 1.0 para todos os símbolos atuais

    if direction == "BUY":
        current_sl_price = entry_price - sl_pts * point_val
        if current_sl_price >= current:
            info_data = info(symbol)
            stops = info_data.get("trade_stops_level", 0) if info_data and "error" not in info_data else 0
            min_dist = max(stops * point_val, point_val * 50)
            new_sl_price = current - min_dist
            new_sl_pts = max(int((entry_price - new_sl_price) / pts_per_unit), 1)
            _log(f"BUY SL acima do preço! sl={current_sl_price:.2f} current={current:.2f}. Novo: {new_sl_pts}pts")
            return new_sl_pts
    else:
        current_sl_price = entry_price + sl_pts * point_val
        if current_sl_price <= current:
            info_data = info(symbol)
            stops = info_data.get("trade_stops_level", 0) if info_data and "error" not in info_data else 0
            min_dist = max(stops * point_val, point_val * 50)
            new_sl_price = current + min_dist
            new_sl_pts = max(int((new_sl_price - entry_price) / pts_per_unit), 1)
            _log(f"SELL SL abaixo do preço! sl={current_sl_price:.2f} current={current:.2f}. Novo: {new_sl_pts}pts")
            return new_sl_pts

    # SL no lado certo mas muito perto — usar stops_level (não hardcoded 50pts)
    # Bug fix (2026-06-22): min_dist era point_val*50 (0.50 R$ p/ BIT), ignorando
    # trade_stops_level (3300 nativos = 33 R$ p/ BIT). Resultado: SL continuava
    # perto demais do preço, MT5 rejeitava de novo → loop infinito de retries.
    # Agora usa max(stops_level, 50pts) * 1.5 (margem de segurança pra ticks
    # entre cálculo e envio).
    info_data = info(symbol)
    stops = info_data.get("trade_stops_level", 0) if info_data and "error" not in info_data else 0

    if direction == "BUY":
        sl_price = entry_price - sl_pts * point_val
        distance = current - sl_price
    else:
        sl_price = entry_price + sl_pts * point_val
        distance = sl_price - current

    min_dist = max(stops * point_val, point_val * 50) * 1.5
    if distance < min_dist:
        if direction == "BUY":
            new_sl_price = current - min_dist
            new_sl_pts = max(int((entry_price - new_sl_price) / pts_per_unit), 1)
        else:
            new_sl_price = current + min_dist
            new_sl_pts = max(int((new_sl_price - entry_price) / pts_per_unit), 1)
        _log(f"SL muito perto! dist={distance:.2f} min={min_dist:.2f} "
             f"entry={entry_price:.2f} current={current:.2f} stops={stops}. "
             f"{sl_pts}pts → {new_sl_pts}pts")
        return new_sl_pts

    return sl_pts


def _build_context(action: str, symbol: str, direction: str = None, sl_pts: int = None,
                    entry_price: float = None, volume: float = None, ticket=None,
                    error: str = "", error_type: str = "", attempt: int = 0,
                    atr: float = None) -> dict:
    """Coleta contexto completo para diagnóstico."""
    from mt5_orchestrator import tick
    ctx = {
        "action": action, "symbol": symbol, "direction": direction,
        "error": error, "error_type": error_type, "sl_pts": sl_pts,
        "entry_price": entry_price, "volume": volume, "attempt": attempt,
        "ticket": ticket, "atr": atr,
        "point_val": _get_point_val(symbol),
    }
    tick_data = tick(symbol)
    if tick_data:
        ctx["current_bid"] = tick_data.get("bid")
        ctx["current_ask"] = tick_data.get("ask")
    return ctx


# ─── Wrapper functions com LLM fallback ───

def safe_buy(symbol: str, volume: float = 1.0, sl_pts: int = None,
             tp_pts: int = None, strategy: str = "", atr: float = None) -> dict:
    from mt5_orchestrator import buy, tick
    result = None

    for attempt in range(MAX_RETRIES):
        result = buy(symbol, volume, sl_pts=sl_pts, tp_pts=tp_pts)
        if result.get("status") == "FILLED":
            return result

        error = result.get("error") or result.get("comment", "")
        err_type = _classify_error(error)
        _log(f"BUY {symbol} falhou [{err_type}]: {error} (tentativa {attempt+1}/{MAX_RETRIES})")

        # 1. Fix padrão rápido
        if err_type == "INVALID_STOPS" and sl_pts:
            point_val = _get_point_val(symbol)
            tick_data = tick(symbol)
            if tick_data:
                new_sl = _fix_invalid_stops(symbol, "BUY", sl_pts, point_val, tick_data)
                if new_sl != sl_pts:
                    _log(f"Fix padrão: SL {sl_pts}pts → {new_sl}pts")
                    sl_pts = new_sl
                    _notify_fix(f"🔧 BUY {symbol}: SL corrigido →{new_sl}pts (Invalid stops)")
                    time.sleep(RETRY_DELAY)
                    continue

        elif err_type == "REQUOTE":
            time.sleep(RETRY_DELAY)
            continue

        elif err_type == "INSUFFICIENT_MARGIN":
            new_vol = max(1.0, volume / 2)
            if new_vol != volume:
                _log(f"Margem insuficiente, volume: {volume} → {new_vol}")
                volume = new_vol
                _notify_fix(f"🔧 BUY {symbol}: Volume reduzido →{new_vol} (margem)")
                continue

        elif err_type in ("NO_CONNECTION", "MARKET_CLOSED"):
            return result

        # 2. LLM fallback — para erros unknown ou quando fix padrão não resolveu
        if attempt >= 1 or err_type == "UNKNOWN":
            ctx = _build_context("BUY", symbol, "BUY", sl_pts=sl_pts,
                                 volume=volume, error=error, error_type=err_type,
                                 attempt=attempt+1, atr=atr)
            llm_result = _llm_diagnose_error(ctx)
            params = llm_result.get("params", {})

            if params.get("abort"):
                _log(f"LLM abortou: {llm_result.get('diagnosis', '?')}")
                _notify_fix(f"🛑 BUY {symbol}: LLM abortou — {llm_result.get('diagnosis', '?')}")
                return result

            applied = False
            if params.get("sl_pts") and isinstance(params["sl_pts"], (int, float)):
                new_sl = int(params["sl_pts"])
                if new_sl > 0 and new_sl != sl_pts:
                    _log(f"LLM sugeriu SL: {sl_pts}pts → {new_sl}pts")
                    sl_pts = new_sl
                    applied = True

            if params.get("volume") and isinstance(params["volume"], (int, float)):
                new_vol = float(params["volume"])
                if new_vol > 0 and new_vol != volume:
                    _log(f"LLM sugeriu volume: {volume} → {new_vol}")
                    volume = new_vol
                    applied = True

            if applied:
                _notify_fix(f"🤖 BUY {symbol}: LLM corrigiu → SL={sl_pts}pts vol={volume}")
                time.sleep(RETRY_DELAY)
                continue

            if not params.get("retry", True):
                _log("LLM disse para não tentar mais")
                return result

        time.sleep(RETRY_DELAY)

    # Última tentativa falhou — notifica
    _notify_fix(f"❌ BUY {symbol}: falhou após {MAX_RETRIES} tentativas — [{err_type}] {error}")
    return result


def safe_sell(symbol: str, volume: float = 1.0, sl_pts: int = None,
              tp_pts: int = None, strategy: str = "", atr: float = None) -> dict:
    from mt5_orchestrator import sell, tick
    result = None

    for attempt in range(MAX_RETRIES):
        result = sell(symbol, volume, sl_pts=sl_pts, tp_pts=tp_pts)
        if result.get("status") == "FILLED":
            return result

        error = result.get("error") or result.get("comment", "")
        err_type = _classify_error(error)
        _log(f"SELL {symbol} falhou [{err_type}]: {error} (tentativa {attempt+1}/{MAX_RETRIES})")

        if err_type == "INVALID_STOPS" and sl_pts:
            point_val = _get_point_val(symbol)
            tick_data = tick(symbol)
            if tick_data:
                new_sl = _fix_invalid_stops(symbol, "SELL", sl_pts, point_val, tick_data)
                if new_sl != sl_pts:
                    _log(f"Fix padrão: SL {sl_pts}pts → {new_sl}pts")
                    sl_pts = new_sl
                    _notify_fix(f"🔧 SELL {symbol}: SL corrigido →{new_sl}pts (Invalid stops)")
                    time.sleep(RETRY_DELAY)
                    continue

        elif err_type == "REQUOTE":
            time.sleep(RETRY_DELAY)
            continue

        elif err_type == "INSUFFICIENT_MARGIN":
            new_vol = max(1.0, volume / 2)
            if new_vol != volume:
                volume = new_vol
                _notify_fix(f"🔧 SELL {symbol}: Volume reduzido →{new_vol} (margem)")
                continue

        elif err_type in ("NO_CONNECTION", "MARKET_CLOSED"):
            return result

        # LLM fallback
        if attempt >= 1 or err_type == "UNKNOWN":
            ctx = _build_context("SELL", symbol, "SELL", sl_pts=sl_pts,
                                 volume=volume, error=error, error_type=err_type,
                                 attempt=attempt+1, atr=atr)
            llm_result = _llm_diagnose_error(ctx)
            params = llm_result.get("params", {})

            if params.get("abort"):
                _log(f"LLM abortou: {llm_result.get('diagnosis', '?')}")
                return result

            applied = False
            if params.get("sl_pts") and isinstance(params["sl_pts"], (int, float)):
                new_sl = int(params["sl_pts"])
                if new_sl > 0 and new_sl != sl_pts:
                    sl_pts = new_sl
                    applied = True

            if params.get("volume") and isinstance(params["volume"], (int, float)):
                new_vol = float(params["volume"])
                if new_vol > 0:
                    volume = new_vol
                    applied = True

            if applied:
                _notify_fix(f"🤖 SELL {symbol}: LLM corrigiu → SL={sl_pts}pts vol={volume}")
                time.sleep(RETRY_DELAY)
                continue

            if not params.get("retry", True):
                return result

        time.sleep(RETRY_DELAY)

    _notify_fix(f"❌ SELL {symbol}: falhou após {MAX_RETRIES} tentativas — [{err_type}] {error}")
    return result


def safe_modify_sl(symbol: str, ticket, sl_pts: int, entry_price: float = None,
                    direction: str = None, atr: float = None) -> dict:
    """modify_sl com auto-recuperação + LLM fallback.

    Guard contra loop infinito de "Invalid stops fix":
    - MAX_FIX_ATTEMPTS: máx 3 chamadas a _fix_invalid_stops_modify por invocação
    - Convergence check: se fix retorna mesmo valor (±5%), escala pra 3x stops_level
    - Same-value abort: se mesmo após escala ainda falha, aborta
    """
    from mt5_orchestrator import modify_sl, tick, status
    result = None
    fix_attempts = 0          # quantas vezes _fix_invalid_stops_modify foi chamado
    MAX_FIX_ATTEMPTS = 3      # máximo de fix por invocação de safe_modify_sl
    prev_sl_pts = None        # valor anterior do fix (pra detecção de convergência)

    for attempt in range(MAX_RETRIES):
        result = modify_sl(symbol, ticket, sl_pts)
        if result.get("status") == "ok":
            return result

        error = result.get("error", "")
        err_type = _classify_error(error)
        _log(f"MODIFY {symbol} ticket={ticket} falhou [{err_type}]: {error} (tentativa {attempt+1}/{MAX_RETRIES})")

        # 1. Fix padrão
        if err_type == "INVALID_STOPS" and entry_price and direction:
            if fix_attempts >= MAX_FIX_ATTEMPTS:
                _log(f"MODIFY {symbol}: max fix attempts ({MAX_FIX_ATTEMPTS}) atingido. Abortando fix.")
                _notify_fix(f"⚠️ MODIFY {symbol}: Invalid stops após {fix_attempts} fixes — abortado (SL={sl_pts}pts)")
                return result

            point_val = _get_point_val(symbol)
            new_sl = _fix_invalid_stops_modify(symbol, str(ticket), sl_pts, point_val,
                                                entry_price, direction)
            fix_attempts += 1

            if new_sl > 0:
                # Convergence check: se new_sl ≈ prev_sl_pts, escala pra 3x stops_level
                if prev_sl_pts is not None and abs(new_sl - prev_sl_pts) <= max(prev_sl_pts * 0.05, 1):
                    from mt5_orchestrator import info as mt5_info
                    info_data = mt5_info(symbol)
                    stops = info_data.get("trade_stops_level", 0) if info_data else 0
                    if stops > 0:
                        escalated_sl = int(stops * 3)
                        _log(f"Convergence detectada ({prev_sl_pts}≈{new_sl}), escalando: "
                             f"{new_sl}pts → {escalated_sl}pts (3x stops_level={stops})")
                        new_sl = escalated_sl
                    else:
                        new_sl = int(new_sl * 3)
                        _log(f"Convergence detectada ({prev_sl_pts}≈{new_sl}), escalando 3x: → {new_sl}pts")

                if new_sl != sl_pts:
                    _log(f"Fix padrão: SL {sl_pts}pts → {new_sl}pts (fix_attempt #{fix_attempts})")
                    prev_sl_pts = sl_pts
                    sl_pts = new_sl
                    _notify_fix(f"🔧 MODIFY {symbol}: SL →{new_sl}pts (Invalid stops fix)")
                    time.sleep(RETRY_DELAY)
                    continue
                else:
                    # Fix retornou mesmo valor → MT5 vai rejeitar de novo.
                    # Abortar em vez de retry infinito.
                    _log(f"Fix retornou mesmo valor ({sl_pts}pts). Abortando.")
                    _notify_fix(f"⚠️ MODIFY {symbol}: fix não resolveu ({sl_pts}pts) após {fix_attempts} tentativas")
                    return result

        elif err_type == "POSITION_NOT_FOUND":
            positions = status().get("positions", [])
            found = [p for p in positions if str(p.get("ticket")) == str(ticket)]
            if not found:
                _log(f"Posição {ticket} não existe mais. Abortando.")
                return {"status": "gone", "error": "posição fechada"}
            time.sleep(RETRY_DELAY)

        elif err_type == "NO_CONNECTION":
            time.sleep(5)
            continue

        # 2. LLM fallback
        if attempt >= 1 or err_type == "UNKNOWN":
            ctx = _build_context("MODIFY_SL", symbol, direction, sl_pts=sl_pts,
                                 entry_price=entry_price, ticket=ticket,
                                 error=error, error_type=err_type,
                                 attempt=attempt+1, atr=atr)
            llm_result = _llm_diagnose_error(ctx)
            params = llm_result.get("params", {})

            if params.get("abort"):
                _log(f"LLM abortou modify: {llm_result.get('diagnosis', '?')}")
                return result

            if params.get("sl_pts") and isinstance(params["sl_pts"], (int, float)):
                new_sl = int(params["sl_pts"])
                if new_sl > 0 and new_sl != sl_pts:
                    _log(f"LLM sugeriu SL: {sl_pts}pts → {new_sl}pts")
                    sl_pts = new_sl
                    _notify_fix(f"🤖 MODIFY {symbol}: LLM corrigiu SL →{new_sl}pts — {llm_result.get('diagnosis', '?')}")
                    time.sleep(RETRY_DELAY)
                    continue

            if not params.get("retry", True):
                return result

        time.sleep(RETRY_DELAY)

    _log(f"MODIFY {symbol}: falhou após {MAX_RETRIES} tentativas")
    return result


def safe_close(symbol: str) -> dict:
    from mt5_orchestrator import close
    result = None

    for attempt in range(MAX_RETRIES):
        result = close(symbol)
        if result.get("status") == "ok":
            return result

        error = result.get("error", "")
        err_type = _classify_error(error)
        _log(f"CLOSE {symbol} falhou [{err_type}]: {error} (tentativa {attempt+1}/{MAX_RETRIES})")

        if err_type == "POSITION_NOT_FOUND":
            return {"status": "already_closed"}

        elif err_type == "REQUOTE":
            time.sleep(RETRY_DELAY)
            continue

        # LLM fallback
        if attempt >= 1 or err_type == "UNKNOWN":
            ctx = _build_context("CLOSE", symbol, error=error, error_type=err_type,
                                 attempt=attempt+1)
            llm_result = _llm_diagnose_error(ctx)
            params = llm_result.get("params", {})

            if params.get("abort"):
                return result
            if not params.get("retry", True):
                return result

        time.sleep(RETRY_DELAY)

    return result


# ─── Helpers ───

def _get_point_val(symbol: str) -> float:
    _map = {"WIN": 1.0, "WDO": 0.001, "BIT": 0.01, "DOL": 0.001, "IND": 1.0, "WSP": 0.01}
    for prefix, val in _map.items():
        if prefix in symbol:
            return val
    return 1.0


def _notify_fix(msg: str):
    if not NOTIFY_ALL_FIXES:
        return
    try:
        from vt_hermes_helper import hermes_send
        hermes_send("telegram:-1004284773048", msg, timeout=15)
    except Exception:
        pass
