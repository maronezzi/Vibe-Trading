"""
core/vt_truth.py
================
FASE 2.5 do refactor (data/architecture_proposal_2026_07_01.md, secao 3.2).

Truth Layer centralizado: MT5 = fonte de verdade autoritativa, period.

Mandato Bruno: toda decisao sensivel passa por estas funcoes get_*.
DB vira cache write-through. State vira projection em memoria.
Logs locais: append-only, nunca usado pra decisao.

Contrato publico (5 funcoes):
    get_open_positions(magic_filter)         -> List[Position]
    get_position_history(symbol, days)        -> List[Deal]
    get_daily_pnl(date_iso)                   -> Decimal (centavos)
    reconcile_db_position(trade_id)           -> Optional[Decimal]
    validate_order_pre_send(symbol, direction)-> bool

Cache em memoria com TTL:
    - positions: 2.0s  (mudam a cada tick)
    - history:   2.0s  (read-frequente dentro de uma iteracao)
    - pnl:       5.0s  (PnL cresce monotonicamente; intraday invalida manual)

FAIL-SAFE: MT5 indisponivel NAO trava caller. Erros sao capturados,
logados via log() e propagados como resultado vazio / False / None —
depende da funcao (ver docstrings).

NUNCA importa mt5_orchestrator fora dos 4 wrappers no topo. Toda chamada
MT5 no projeto deve passar por este modulo (single point of truth).
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

# Unica ponte MT5 — import lazy-safe para nao crashar import se Wine down.
# O orchestrator tem type hints legacy (symbol: str = None) que conflitam com
# o tipo correto (symbol: Optional[str]). Envolvemos tudo em `Any` aqui para
# isolar o resto do modulo desses hints problematicos (camada de baixo nivel,
# NAO TOCAR).
from typing import Any as _Any
try:
    from mt5.mt5_orchestrator import (
        status as _mt5_status_raw,
        history as _mt5_history_raw,
    )
    _MT5_AVAILABLE = True
except Exception:  # pragma: no cover — runtime guard
    _MT5_AVAILABLE = False

    def _mt5_status_raw(*_args: _Any, **_kwargs: _Any) -> _Any:  # type: ignore
        return {"error": "mt5_orchestrator indisponivel", "positions": []}

    def _mt5_history_raw(*_args: _Any, **_kwargs: _Any) -> _Any:  # type: ignore
        return {"error": "mt5_orchestrator indisponivel", "history": []}


# Adaptadores tipados (saida Dict[str, Any], aceita Optional[str] no symbol).
def _mt5_status() -> Dict[str, Any]:
    return _mt5_status_raw()  # type: ignore[no-any-return]


def _mt5_history(symbol: Optional[str] = None, days: int = 7) -> Dict[str, Any]:
    return _mt5_history_raw(symbol, days)  # type: ignore[arg-type,no-any-return]


# ===== Constantes =====
MAGIC_VIBETRADING = 555501  # mt5_executor.py L231/L341 (mesmo do validate_order_pre_send)
CACHE_TTL_POSITIONS = 2.0
CACHE_TTL_HISTORY = 2.0
CACHE_TTL_PNL = 5.0

# Mapeamento canonico de "type" MT5 -> direcao.
# 0 = BUY, 1 = SELL (conforme docs MetaTrader5). Strings tambem aceitas
# (MT5 real hoje retorna "BUY"/"SELL", mas o codigo nao pode assumir isso).
_DIRECTION_BUY = 0
_DIRECTION_SELL = 1


def _normalize_direction(type_value: _Any) -> str:
    """Normaliza o campo `type` MT5 para string de direcao canonica.

    Aceita:
        - 0 / "0"             -> "BUY"
        - 1 / "1"             -> "SELL"
        - "BUY" / "buy"       -> "BUY"
        - "SELL" / "sell"     -> "SELL"
        - qualquer outro str  -> passado adiante (logado como WARN se !=
                                 BUY/SELL, pra nao mascarar typos do broker)
        - None / False / ""   -> "" (logado como WARN; caller decide o que fazer)

    Por que existe (Fase 3.5):
        str(p.get("type", "") or "") colapsa type=0 (int BUY) para "" porque
        `0 or ""` -> "". Hoje MT5 retorna string, entao o bug nao se
        manifesta em producao, mas o codigo fica fragil: se um helper interno
        esquecer de mapear int->str, a direcao e silenciosamente perdida
        e positions com direction="" nao conseguem matchear filtros
        BUY/SELL. Esta funcao torna o mapping explicito e tolerante.
    """
    # None / False / ""  (cuidado: `0 or ""` cai aqui tambem, por isso
    # testamos o int ANTES do `or ""`)
    if type_value is None or type_value is False or type_value == "":
        _log(f"_normalize_direction: type vazio/None/False recebido ({type_value!r})")
        return ""

    # int 0/1 (codigo MT5 nativo) — caminho principal do fix
    if isinstance(type_value, bool):
        # bool eh subclasse de int, mas True/False nao sao direcoes validas.
        _log(f"_normalize_direction: bool recebido ({type_value!r}), retornando ''")
        return ""
    if isinstance(type_value, int):
        if type_value == _DIRECTION_BUY:
            return "BUY"
        if type_value == _DIRECTION_SELL:
            return "SELL"
        _log(f"_normalize_direction: int fora do range BUY/SELL ({type_value!r})")
        return str(type_value)

    # Strings: "0"/"1"/"BUY"/"SELL"/"buy"/"sell" etc.
    if isinstance(type_value, str):
        s = type_value.strip()
        if s == "":
            _log("_normalize_direction: string vazia apos strip")
            return ""
        if s == "0":
            return "BUY"
        if s == "1":
            return "SELL"
        upper = s.upper()
        if upper in ("BUY", "SELL"):
            return upper
        # String desconhecida — passa adiante mas loga WARN pra nao
        # mascarar typos do broker.
        _log(f"_normalize_direction: string de direcao nao-canonica ({s!r})")
        return s

    # Qualquer outro tipo (float, list, etc) — passa como string
    # e loga WARN.
    _log(f"_normalize_direction: tipo nao-suportado ({type(type_value).__name__}: {type_value!r})")
    return str(type_value)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRADES_DB = PROJECT_ROOT / "vt_trades.db"


# ===== Logger minimal (append-only, nunca usado pra decisao) =====
def _log(msg: str) -> None:
    """Append-only log. NAO levantar excecao — log e seguia."""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] [vt_truth] {msg}", flush=True)
    except Exception:
        pass


# ===== TTL Cache =====
class _TTLCache:
    """Cache simples key->value com TTL em segundos."""

    def __init__(self, ttl_sec: float) -> None:
        self.ttl = ttl_sec
        self._store: Dict[str, Any] = {}
        self._ts: Dict[str, float] = {}

    def get(self, key: str) -> Optional[Any]:
        if key in self._store and key in self._ts:
            if time.time() - self._ts[key] < self.ttl:
                return self._store[key]
        return None

    def set(self, key: str, value: Any) -> None:
        self._store[key] = value
        self._ts[key] = time.time()

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)
        self._ts.pop(key, None)

    def clear(self) -> None:
        self._store.clear()
        self._ts.clear()


_positions_cache = _TTLCache(CACHE_TTL_POSITIONS)
_history_cache = _TTLCache(CACHE_TTL_HISTORY)
_pnl_cache = _TTLCache(CACHE_TTL_PNL)


def _reset_caches_for_testing() -> None:
    """Limpa todos os caches. Apenas para testes."""
    _positions_cache.clear()
    _history_cache.clear()
    _pnl_cache.clear()


# ===== Tipos publicos (frozen dataclasses — imutaveis) =====
@dataclass(frozen=True)
class Position:
    """Posicao aberta autoritativa (MT5)."""
    ticket: int
    symbol: str
    direction: str  # "BUY" | "SELL"
    volume: float
    price_open: float
    price_current: float
    sl: float
    tp: float
    profit: float
    swap: float
    magic: int
    open_time: str
    comment: str
    identifier: int = 0


@dataclass(frozen=True)
class Deal:
    """Deal historico autoritativo (MT5 history)."""
    ticket: int
    symbol: str
    direction: str  # "BUY" | "SELL"
    volume: float
    price: float
    profit: float
    commission: float
    swap: float
    fee: float
    time: str
    position_id: int
    reason: int
    magic: int
    comment: str = ""


# ===== 1. get_open_positions =====
def get_open_positions(magic_filter: int = MAGIC_VIBETRADING) -> List[Position]:
    """Retorna posicoes abertas no MT5 (autoritativo), filtradas por magic.

    Args:
        magic_filter: magic number do bot. Default 555501 (VibeTrading).

    Returns:
        Lista de Position (dataclass frozen, imutavel). Vazia se MT5 indisponivel.

    Comportamento:
        - TTL 2.0s (cache em memoria, evita chamada Wine repetida dentro de 1 tick).
        - FAIL-SAFE: se MT5 retorna dict sem 'positions' ou com erro -> lista vazia.
        - NAO levanta excecao (caller nao precisa de try/except).
    """
    cache_key = f"open_{magic_filter}"
    cached = _positions_cache.get(cache_key)
    if cached is not None:
        return list(cached)

    try:
        raw = _mt5_status() or {}
    except Exception as e:
        _log(f"get_open_positions: MT5 status() falhou ({type(e).__name__}: {e})")
        return []

    if not isinstance(raw, dict):
        _log(f"get_open_positions: status() retornou tipo invalido {type(raw).__name__}")
        return []

    positions_raw = raw.get("positions") or []
    if not isinstance(positions_raw, list):
        _log(f"get_open_positions: 'positions' nao eh list ({type(positions_raw).__name__})")
        return []

    result: List[Position] = []
    for p in positions_raw:
        if not isinstance(p, dict):
            continue
        if p.get("magic") != magic_filter:
            continue
        try:
            result.append(Position(
                ticket=int(p.get("ticket", 0) or 0),
                symbol=str(p.get("symbol", "") or ""),
                direction=_normalize_direction(p.get("type")),
                volume=float(p.get("volume", 0.0) or 0.0),
                price_open=float(p.get("price_open", 0.0) or 0.0),
                price_current=float(p.get("price_current", 0.0) or 0.0),
                sl=float(p.get("sl", 0.0) or 0.0),
                tp=float(p.get("tp", 0.0) or 0.0),
                profit=float(p.get("profit", 0.0) or 0.0),
                swap=float(p.get("swap", 0.0) or 0.0),
                magic=int(p.get("magic", 0) or 0),
                open_time=str(p.get("time", "") or ""),
                comment=str(p.get("comment", "") or ""),
                identifier=int(p.get("identifier", 0) or 0),
            ))
        except (TypeError, ValueError) as e:
            _log(f"get_open_positions: pos malformada ignorada ({e})")
            continue

    _positions_cache.set(cache_key, result)
    return list(result)


# ===== 2. get_position_history =====
def get_position_history(
    symbol: Optional[str] = None, days: int = 1
) -> List[Deal]:
    """Retorna deals historicos do MT5 (autoritativo).

    Args:
        symbol: filtro de simbolo (None = todos). Ex: "WINM26", "WDON26".
        days: janela retroativa em dias. Default 1 (so hoje).

    Returns:
        Lista de Deal (frozen dataclass). Vazia se MT5 indisponivel / sem deals.

    Comportamento:
        - TTL 2.0s em memoria.
        - FAIL-SAFE: erros nao levantam.
        - Tolerante a formato de entrada errado (pula pos malformada com warn).
    """
    cache_key = f"hist_{symbol or 'ALL'}_{days}"
    cached = _history_cache.get(cache_key)
    if cached is not None:
        return list(cached)

    try:
        raw = _mt5_history(symbol=symbol, days=days) or {}
    except Exception as e:
        _log(f"get_position_history: MT5 history() falhou ({type(e).__name__}: {e})")
        return []

    if not isinstance(raw, dict):
        _log(f"get_position_history: history() retornou tipo invalido {type(raw).__name__}")
        return []

    # Executor retorna {"history": [...], "count": N}. Tolerar "deals" tbm (legacy).
    deals_raw = raw.get("history") or raw.get("deals") or []
    if not isinstance(deals_raw, list):
        _log(f"get_position_history: 'history' nao eh list ({type(deals_raw).__name__})")
        return []

    result: List[Deal] = []
    for d in deals_raw:
        if not isinstance(d, dict):
            continue
        try:
            result.append(Deal(
                ticket=int(d.get("ticket", 0) or 0),
                symbol=str(d.get("symbol", "") or ""),
                direction=_normalize_direction(d.get("type")),
                volume=float(d.get("volume", 0.0) or 0.0),
                price=float(d.get("price", 0.0) or 0.0),
                profit=float(d.get("profit", 0.0) or 0.0),
                commission=float(d.get("commission", 0.0) or 0.0),
                swap=float(d.get("swap", 0.0) or 0.0),
                fee=float(d.get("fee", 0.0) or 0.0),
                time=str(d.get("time", "") or ""),
                position_id=int(d.get("position_id", 0) or 0),
                reason=int(d.get("reason", 0) or 0),
                magic=int(d.get("magic", 0) or 0),
                comment=str(d.get("comment", "") or ""),
            ))
        except (TypeError, ValueError) as e:
            _log(f"get_position_history: deal malformado ignorado ({e})")
            continue

    _history_cache.set(cache_key, result)
    return list(result)


# ===== 3. get_daily_pnl =====
def get_daily_pnl(date_iso: Optional[str] = None) -> Decimal:
    """PnL realizado do dia (broker-truth, MT5 history deals).

    Args:
        date_iso: data de referencia (YYYY-MM-DD). Se None, usa "today".

    Returns:
        Decimal com PnL em R$ (precisao 0.01). Zero se sem deals / MT5 down.

    Comportamento:
        - Soma profit + commission + swap por deal (mesmo padrao do copilot
          FASE 1, agora centralizado aqui).
        - TTL 5.0s em memoria (PnL cresce monotonicamente; intraday invalida).
        - Filtra por data se date_iso fornecido (hoje por default).
        - FAIL-SAFE: retorna Decimal('0.00') em qualquer erro.
    """
    cache_key = f"pnl_{date_iso or 'today'}"
    cached = _pnl_cache.get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]

    # days=2 cobre transbordo de meia-noite (1 deal de ontem + 1 de hoje)
    deals = get_position_history(symbol=None, days=2)

    if date_iso is None:
        target_date = datetime.now().strftime("%Y-%m-%d")
    else:
        target_date = date_iso

    total = Decimal("0.00")
    for d in deals:
        # time vem como string "1719840300" (epoch) ou ISO.
        # Tentar extrair a data (YYYY-MM-DD) de ambos formatos.
        deal_date = _extract_date_iso(d.time)
        if deal_date and deal_date != target_date:
            continue
        try:
            total += Decimal(str(d.profit)) + Decimal(str(d.commission)) + Decimal(str(d.swap))
        except Exception as e:
            _log(f"get_daily_pnl: deal malformado ignorado ({e})")
            continue

    total = total.quantize(Decimal("0.01"))
    _pnl_cache.set(cache_key, total)
    return total


def _extract_date_iso(time_str: str) -> Optional[str]:
    """Extrai YYYY-MM-DD de string de timestamp MT5 (epoch ou ISO)."""
    if not time_str:
        return None
    # Epoch numerico: "1719840300" ou "1719840300.123"
    try:
        epoch = float(time_str)
        if epoch > 1e9:  # plausivel (> 2001)
            return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError):
        pass
    # ISO: "2026-07-01 14:30:00" ou "2026-07-01T14:30:00"
    try:
        s = time_str.replace("T", " ")[:10]
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return s[:10]
    except Exception:
        pass
    return None


# ===== 4. reconcile_db_position =====
def reconcile_db_position(trade_id: int, db_path: Optional[Path] = None) -> Optional[Decimal]:
    """Para um trade do DB: busca deal correspondente no MT5 history e atualiza
    gross_pnl/net_pnl com broker-truth se exit_time IS NULL e ticket nao esta
    mais aberto no MT5.

    Args:
        trade_id: id do trade em vt_trades.db.
        db_path: path customizado do DB (None = usar TRADES_DB do projeto).

    Returns:
        Decimal com PnL reconciliado, ou None se nada para reconciliar / erro.

    Comportamento:
        - Le history(symbol=trade.symbol, days=7) do MT5.
        - Filtra deals cujo position_id == entry_ticket (MT5 conceito: deals
          in/out compartilham o mesmo position_id).
        - Soma profit+commission+swap de TODOS os deals relacionados (in+out).
        - UPDATE apenas se exit_time IS NULL (nao sobrescreve exit ja logado).
        - FAIL-SAFE: qualquer erro de DB/MT5 -> log + retorna None.
    """
    db_file = Path(db_path) if db_path else TRADES_DB
    try:
        if not db_file.exists():
            return None
        conn = sqlite3.connect(str(db_file), timeout=5.0)
        conn.row_factory = sqlite3.Row
    except Exception as e:
        _log(f"reconcile_db_position({trade_id}): DB indisponivel ({e})")
        return None

    try:
        row = conn.execute(
            "SELECT id, entry_ticket, symbol, exit_time FROM trades WHERE id = ?",
            (trade_id,),
        ).fetchone()
        if not row:
            conn.close()
            return None
        if row["exit_time"]:  # ja tem exit, nao mexe
            conn.close()
            return None

        entry_ticket_str = str(row["entry_ticket"] or "").strip()
        symbol = str(row["symbol"] or "").strip()
        if not entry_ticket_str or not symbol:
            conn.close()
            return None

        # Tentar converter entry_ticket pra int (MT5 deals sao int)
        try:
            entry_ticket_int = int(entry_ticket_str)
        except (ValueError, TypeError):
            entry_ticket_int = None

        deals = get_position_history(symbol=symbol, days=7)
        if not deals:
            conn.close()
            return None

        # Deals relacionados: in (entry) e out (exit) do mesmo position.
        # position_id MT5 == entry_ticket do bot.
        relevant = [
            d for d in deals
            if (entry_ticket_int is not None and d.position_id == entry_ticket_int)
            or str(d.ticket) == entry_ticket_str
        ]
        if not relevant:
            conn.close()
            return None

        total = Decimal("0.00")
        for d in relevant:
            try:
                total += Decimal(str(d.profit)) + Decimal(str(d.commission)) + Decimal(str(d.swap))
            except Exception:
                continue

        total = total.quantize(Decimal("0.01"))
        conn.execute(
            "UPDATE trades SET gross_pnl = ?, net_pnl = ? WHERE id = ? AND exit_time IS NULL",
            (float(total), float(total), trade_id),
        )
        conn.commit()
        _log(f"reconcile_db_position({trade_id}): broker-truth aplicado R$ {total} "
             f"({len(relevant)} deals, symbol={symbol}, entry_ticket={entry_ticket_str})")
        conn.close()
        return total
    except Exception as e:
        _log(f"reconcile_db_position({trade_id}): erro inesperado ({type(e).__name__}: {e})")
        try:
            conn.close()
        except Exception:
            pass
        return None


# ===== 5. validate_order_pre_send =====
def validate_order_pre_send(
    symbol: str,
    direction: str,
    magic: int = MAGIC_VIBETRADING,
) -> bool:
    """Bloqueia ordem duplicada: se ja existe pos aberta com mesmo magic+symbol,
    retorna False (NAO envia). Caso contrario True.

    Args:
        symbol: contrato MT5 (ex: "WINM26", "WDON26").
        direction: "BUY" ou "SELL" (informativo; bloqueia qualquer direcao nova
            enquanto existir pos aberta do bot no symbol — reversoes sao feitas
            em manage_position, nao aqui).
        magic: magic number do bot. Default 555501.

    Returns:
        True -> seguro enviar ordem.
        False -> bloqueado (ja existe pos aberta no symbol com mesmo magic).

    Comportamento:
        - Consulta get_open_positions() (cache 2s).
        - FAIL-SAFE: se MT5 indisponivel, retorna True (permite envio). Mesma
          politica do validate_order_pre_send original em vt_autotrader.py L1568:
          bloquear por defeito de leitura = lockup; safe_buy/safe_sell tem retry
          proprio.
        - Loga [BLOCKED-DUPLICATE] quando bloqueia (compat com format original).
    """
    try:
        positions = get_open_positions(magic_filter=magic)
    except Exception as e:
        _log(f"validate_order_pre_send({symbol}): falha leitura ({type(e).__name__}: {e}) — FAIL-SAFE: permite")
        return True

    for p in positions:
        if p.symbol == symbol:
            _log(
                f"[BLOCKED-DUPLICATE] {symbol} ja tem pos aberta "
                f"ticket={p.ticket} type={p.direction} "
                f"magic={p.magic} volume={p.volume} "
                f"— bloqueando novo {direction}"
            )
            return False
    return True


# =============================================================================
# Fase 3 — Helper de cálculo de SL (Lei 3: SL obrigatório)
# =============================================================================
def compute_sl_atr(atr: float, sl_atr_mult: float, min_sl: float = 1.0) -> float:
    """Calcula SL baseado em ATR. Garante SL >= min_sl (Lei 3).

    Helper GENÉRICO: atr * sl_atr_mult, com floor em `min_sl`.

    Quando usar este vs `_calc_sl` do autotrader?
    ---------------------------------------------
    - `compute_sl_atr` (este): genérico, sem specs por símbolo. Útil para
      validações, testes, e callers que só precisam garantir que SL > 0.
    - `_calc_sl` (`core/vt_autotrader.py:1533`): canônico para o path de ordens
      AO VIVO. Tem specs por símbolo (min_native/max_native/point_mult para
      WIN/WDO/BIT/WSP/IND/DOL) e arredondamento. O autotrader JÁ o usa.

    Lei 3 (Segurança de Execução): toda ordem DEVE ter SL > 0. Este helper
    garante o floor; `_calc_sl` garante os limites por símbolo. Ambos defendem
    a Lei 3 — quem chama buy()/sell() deve passar sl > 0.

    Args:
        atr: ATR do ativo (em pontos nativos do preço).
        sl_atr_mult: multiplicador (típico 1.0-2.5, de params_by_tf).
        min_sl: floor mínimo (default 1.0; nunca 0 — Lei 3).

    Returns:
        sl >= min_sl (sempre positivo).
    """
    if atr is None or atr <= 0:
        sl = float(min_sl)
    else:
        mult = sl_atr_mult if (sl_atr_mult and sl_atr_mult > 0) else 1.5
        sl = atr * mult
        if sl < min_sl:
            _log(f"[SL] compute_sl_atr: {sl:.4f} < {min_sl} (min), usando {min_sl}")
            sl = float(min_sl)
    return max(sl, float(min_sl))
