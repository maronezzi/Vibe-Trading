"""
core/vt_confluence.py — Camada de confluencia multi-indicador.

Wave Per-TF+ (Bruno 09/07): filtro que envolve qualquer estrategia.
Combina 4 checks independentes. Score 0-4. Se score < min_score, bloqueia.

Checks:
  1. CANDLE_PATTERN: padrao de reversao (hammer/pin bar/engulfing) na direcao
  2. FIBONACCI:      preco perto (touch_pct) de nivel 38.2/50/61.8% de swing
  3. HTF_BIAS:       H1 confirma direcao (BULL/BEAR via EMA+ADX)
  4. ADX_TRENDING:   ADX >= adx_min (mercado em tendencia, nao chop)

API publica:
    evaluate(symbol, tf, direction, price, atr, bars, bars_h1, params, utils) -> dict
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, Optional, Tuple

# Self-bootstrap para permitir `from core.vt_confluence import ...` mesmo
# quando o modulo e carregado fora do contexto do autotrader (tests, scripts).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


# Defaults documentados no design (docs/CONFLUENCE_DESIGN.md). Cada chamada
# pode sobrescrever via params (lido do CONFIG["params_by_tf"][SYMBOL_TF]).
DEFAULT_MIN_CONFLUENCE_SCORE = 2
DEFAULT_CANDLE_REQUIRED = True
DEFAULT_FIB_REQUIRED = True
DEFAULT_FIB_LOOKBACK = 50
DEFAULT_FIB_TOUCH_PCT = 0.003
DEFAULT_HTF_REQUIRED = True
DEFAULT_HTF_EMA_FAST = 9
DEFAULT_HTF_EMA_SLOW = 21
DEFAULT_HTF_ADX_MIN = 18.0
DEFAULT_ADX_REQUIRED = True
DEFAULT_ADX_MIN = 18.0

MAX_SCORE = 4  # candle + fib + htf + adx


def _format_checkmarks(checks: Dict[str, Dict[str, Any]]) -> str:
    """Formata 'candle=✓ fib=✗ htf=✓ adx=✗' para log/Telegram."""
    return (
        f"candle={'✓' if checks['candle']['pass'] else '✗'} "
        f"fib={'✓' if checks['fibonacci']['pass'] else '✗'} "
        f"htf={'✓' if checks['htf_bias']['pass'] else '✗'} "
        f"adx={'✓' if checks['adx']['pass'] else '✗'}"
    )


def _check_candle_pattern(
    direction: str, bars: list, params: dict, utils: dict
) -> Tuple[bool, Optional[str], str]:
    """Detecta hammer/pin bar/engulfing na direcao desejada.

    Logica copiada/adaptada de strategies/candle_patterns.py:65-103.
    Retorna (pass, pattern_name, reason).
    """
    if not bars or len(bars) < 2:
        return False, None, "bars insuficientes (<2)"

    # Honor de candle_required — se False, sempre passa.
    if not params.get("candle_required", DEFAULT_CANDLE_REQUIRED):
        return True, None, "candle_required=False (skip)"

    body_ratio = params.get("candle_body_ratio", 0.3)
    wick_ratio = params.get("candle_wick_ratio", 2.0)

    curr = bars[0]
    prev = bars[1]

    c_open = curr.get("open", 0)
    c_high = curr.get("high", 0)
    c_low = curr.get("low", 0)
    c_close = curr.get("close", 0)

    p_open = prev.get("open", 0)
    p_close = prev.get("close", 0)

    if c_high <= c_low or c_open == 0 or c_close == 0 or p_open == 0 or p_close == 0:
        return False, None, "OHLC invalido"

    c_range = c_high - c_low
    if c_range <= 0:
        return False, None, "range do candle zerado"

    c_body = abs(c_close - c_open)
    c_upper_wick = c_high - max(c_open, c_close)
    c_lower_wick = min(c_open, c_close) - c_low
    p_body = abs(p_close - p_open)

    pattern: Optional[str] = None

    # BUY: hammer bullish (longa wick inferior) ou engulfing bullish.
    if direction == "BUY":
        if (
            c_body / c_range < body_ratio
            and c_lower_wick > c_body * wick_ratio
            and c_close > c_open
        ):
            pattern = "HAMMER"
        elif (
            c_close > c_open
            and p_close < p_open
            and c_body > p_body
            and c_close > p_open
            and c_open < p_close
        ):
            pattern = "ENGULFING_BULL"

    # SELL: pin bar bearish (longa wick superior) ou engulfing bearish.
    elif direction == "SELL":
        if (
            c_body / c_range < body_ratio
            and c_upper_wick > c_body * wick_ratio
            and c_close < c_open
        ):
            pattern = "PIN_BAR_BEARISH"
        elif (
            c_close < c_open
            and p_close > p_open
            and c_body > p_body
            and c_close < p_open
            and c_open > p_close
        ):
            pattern = "ENGULFING_BEAR"

    if pattern is None:
        return False, None, f"nenhum padrao de reversao para {direction}"

    return True, pattern, f"padrao {pattern} detectado"


def _check_fibonacci(
    direction: str, price: float, bars: list, params: dict
) -> Tuple[bool, Optional[float], float]:
    """Verifica se price esta perto (<fib_touch_pct) de nivel 38.2/50/61.8%.

    Para BUY (pullback de alta), niveis = swing_high - range * fib.
    Para SELL (pullback de baixa), niveis = swing_low + range * fib.
    Retorna (pass, nearest_level, distance_pct).
    """
    fib_lookback = int(params.get("fib_lookback", DEFAULT_FIB_LOOKBACK))
    fib_touch_pct = float(params.get("fib_touch_pct", DEFAULT_FIB_TOUCH_PCT))

    if not bars or len(bars) < 2 or price <= 0:
        return False, None, float("inf")

    if not params.get("fib_required", DEFAULT_FIB_REQUIRED):
        return True, None, 0.0

    recent = bars[:fib_lookback]
    if len(recent) < 5:
        return False, None, float("inf")

    swing_high = max(b.get("high", 0) for b in recent)
    swing_low = min(b.get("low", float("inf")) for b in recent)
    if swing_high == 0 or swing_low == float("inf") or swing_high <= swing_low:
        return False, None, float("inf")

    swing_range = swing_high - swing_low
    fib_levels = [0.382, 0.500, 0.618]

    nearest_level: Optional[float] = None
    nearest_dist_pct = float("inf")

    for fib in fib_levels:
        if direction == "BUY":
            level = swing_high - swing_range * fib
        else:  # SELL
            level = swing_low + swing_range * fib

        if level <= 0:
            continue
        dist_pct = abs(price - level) / level
        if dist_pct < nearest_dist_pct:
            nearest_dist_pct = dist_pct
            nearest_level = level

    if nearest_level is None:
        return False, None, float("inf")

    if nearest_dist_pct <= fib_touch_pct:
        return True, nearest_level, nearest_dist_pct

    return False, nearest_level, nearest_dist_pct


def _check_htf_bias(
    direction: str, bars_h1: Optional[list], params: dict, utils: dict
) -> Tuple[bool, Optional[str], str]:
    """Calcula bias H1 (BULL/BEAR) via EMA fast/slow + ADX + DI+/-.

    Logica copiada de strategies/htf_bias_ltf_entry.py:65-87.
    Se bars_h1 nao fornecido ou insuficiente, retorna (False, None, motivo)
    — operador pode tunar htf_required=False para desligar.
    """
    if not params.get("htf_required", DEFAULT_HTF_REQUIRED):
        return True, None, "htf_required=False (skip)"

    if not bars_h1 or len(bars_h1) < 5:
        return False, None, "bars_h1 indisponivel"

    ema_fast = int(params.get("htf_ema_fast", DEFAULT_HTF_EMA_FAST))
    ema_slow = int(params.get("htf_ema_slow", DEFAULT_HTF_EMA_SLOW))
    adx_min = float(params.get("htf_adx_min", DEFAULT_HTF_ADX_MIN))
    adx_period = params.get("htf_adx_period", 14)

    if len(bars_h1) < ema_slow + 5:
        return False, None, f"bars_h1 insuficiente (<{ema_slow + 5})"

    calculate_ema = utils.get("calculate_ema")
    calculate_adx = utils.get("calculate_adx")
    if not calculate_ema or not calculate_adx:
        return False, None, "utils calculate_ema/calculate_adx ausentes"

    try:
        ema_fast_h1 = calculate_ema(bars_h1, ema_fast)
        ema_slow_h1 = calculate_ema(bars_h1, ema_slow)
    except Exception as e:
        return False, None, f"EMA H1 falhou: {type(e).__name__}"

    if ema_fast_h1 == 0 or ema_slow_h1 == 0:
        return False, None, "EMA H1 zerada"

    try:
        adx_tuple = calculate_adx(bars_h1, adx_period)
    except Exception as e:
        return False, None, f"ADX H1 falhou: {type(e).__name__}"

    if not isinstance(adx_tuple, tuple) or len(adx_tuple) < 3:
        return False, None, "ADX H1 retorno invalido"
    adx_val, plus_di, minus_di = adx_tuple[0], adx_tuple[1], adx_tuple[2]

    if adx_val < adx_min:
        return False, None, f"ADX H1 {adx_val:.1f} < {adx_min}"

    bias: Optional[str] = None
    if ema_fast_h1 > ema_slow_h1 and plus_di > minus_di:
        bias = "BULL"
    elif ema_fast_h1 < ema_slow_h1 and minus_di > plus_di:
        bias = "BEAR"

    if bias is None:
        return False, None, "H1 sem bias claro (EMA vs DI nao alinham)"

    if direction == "BUY" and bias != "BULL":
        return False, bias, f"direction=BUY mas bias H1={bias}"
    if direction == "SELL" and bias != "BEAR":
        return False, bias, f"direction=SELL mas bias H1={bias}"

    return True, bias, f"H1 bias {bias} confirma {direction}"


def _check_adx(
    price: float, bars: list, params: dict, utils: dict
) -> Tuple[bool, float, float]:
    """Calcula ADX no timeframe corrente; passa se >= adx_min."""
    adx_min = float(params.get("adx_min", DEFAULT_ADX_MIN))
    adx_period = params.get("adx_period", 14)

    if not params.get("adx_required", DEFAULT_ADX_REQUIRED):
        return True, 0.0, adx_min

    if not bars or len(bars) < adx_period * 2:
        return False, 0.0, adx_min

    calculate_adx = utils.get("calculate_adx")
    if not calculate_adx:
        return False, 0.0, adx_min

    try:
        adx_tuple = calculate_adx(bars, adx_period)
    except Exception:
        return False, 0.0, adx_min

    if not isinstance(adx_tuple, tuple) or len(adx_tuple) < 1:
        return False, 0.0, adx_min

    adx_val = float(adx_tuple[0])
    if adx_val >= adx_min:
        return True, adx_val, adx_min
    return False, adx_val, adx_min


def evaluate(
    symbol: str,
    tf: str,
    direction: str,
    price: float,
    atr: float,
    bars: list,
    bars_h1: Optional[list],
    params: Optional[dict] = None,
    utils: Optional[dict] = None,
) -> Dict[str, Any]:
    """Aplica os 4 checks de confluencia e retorna dict padronizado.

    Args:
        symbol: simbolo resolved (ex: WINM26).
        tf: timeframe corrente (M5/M15/etc).
        direction: 'BUY' ou 'SELL'.
        price: preco de referencia (last close).
        atr: ATR do timeframe (nao usado pelos checks atuais, mantido p/ API).
        bars: barras do timeframe corrente (newest-first, bars[0]=atual).
        bars_h1: barras H1 ou None (se None, htf_bias check falha).
        params: dict de configuracao (lido do CONFIG["params_by_tf"]).
        utils: dict de indicadores (calculate_ema/adx/rsi/atr/vwap).

    Returns:
        dict com score, max_score, pass, checks{}, min_score, blocked_reason.
    """
    if params is None:
        params = {}
    if utils is None:
        utils = {}

    direction = (direction or "").upper()
    if direction not in ("BUY", "SELL"):
        # Direcao invalida — bloqueia conservadoramente.
        return {
            "score": 0,
            "max_score": MAX_SCORE,
            "pass": False,
            "checks": {
                "candle": {"pass": False, "pattern": None, "reason": "direction invalida"},
                "fibonacci": {"pass": False, "level": None, "distance_pct": float("inf")},
                "htf_bias": {"pass": False, "bias": None, "reason": "direction invalida"},
                "adx": {"pass": False, "adx": 0.0, "threshold": DEFAULT_ADX_MIN},
            },
            "min_score": int(params.get("min_confluence_score", DEFAULT_MIN_CONFLUENCE_SCORE)),
            "blocked_reason": f"direction invalida: {direction!r}",
        }

    candle_pass, candle_pattern, candle_reason = _check_candle_pattern(
        direction, bars, params, utils
    )
    fib_pass, fib_level, fib_dist = _check_fibonacci(direction, price, bars, params)
    htf_pass, htf_bias, htf_reason = _check_htf_bias(direction, bars_h1, params, utils)
    adx_pass, adx_val, adx_thr = _check_adx(price, bars, params, utils)

    score = int(candle_pass) + int(fib_pass) + int(htf_pass) + int(adx_pass)
    min_score = int(params.get("min_confluence_score", DEFAULT_MIN_CONFLUENCE_SCORE))
    passed = score >= min_score

    if passed:
        blocked_reason: Optional[str] = None
    else:
        failed = MAX_SCORE - score
        blocked_reason = (
            f"{failed} checks falharam, minimo {min_score}"
        )

    return {
        "score": score,
        "max_score": MAX_SCORE,
        "pass": passed,
        "checks": {
            "candle": {"pass": candle_pass, "pattern": candle_pattern, "reason": candle_reason},
            "fibonacci": {"pass": fib_pass, "level": fib_level, "distance_pct": fib_dist},
            "htf_bias": {"pass": htf_pass, "bias": htf_bias, "reason": htf_reason},
            "adx": {"pass": adx_pass, "adx": adx_val, "threshold": adx_thr},
        },
        "min_score": min_score,
        "blocked_reason": blocked_reason,
    }


def notify_confluence_result(
    symbol_root: str,
    tf: str,
    direction: str,
    score: int,
    max_score: int,
    checks: Dict[str, Dict[str, Any]],
    blocked: bool,
    min_score: int,
    send_fn=None,
) -> bool:
    """Notifica bloqueio de confluencia via Telegram com cooldown 5min.

    Args:
        symbol_root: raiz do simbolo (WIN/WDO/BIT/WSP).
        tf: timeframe.
        direction: BUY/SELL.
        score: pontuacao obtida.
        max_score: pontuacao maxima.
        checks: dict com resultados por check.
        blocked: se True, envia msg de bloqueio. Se False, nao envia.
        min_score: minimo exigido.
        send_fn: funcao de envio (default: core.vt_autotrader.notify_telegram).

    Returns:
        True se enviou, False se suprimido por cooldown ou se nao e blocked.
    """
    if not blocked:
        # Aprovacao nao e notificada por aqui — vem com a mensagem de
        # abertura do trade, gerenciada em _execute_entry.
        return False

    # Import lazy para evitar circular import com core.vt_autotrader.
    try:
        from core.vt_notify import notify_once as _notify_once
    except ImportError:
        return False

    if send_fn is None:
        try:
            from core.vt_autotrader import notify_telegram as _send_default
            send_fn = _send_default
        except ImportError:
            return False

    mark_str = _format_checkmarks(checks)
    candle_mark = '✓' if checks['candle']['pass'] else '✗'
    fib_mark = '✓' if checks['fibonacci']['pass'] else '✗'
    htf_mark = '✓' if checks['htf_bias']['pass'] else '✗'
    adx_mark = '✓' if checks['adx']['pass'] else '✗'
    failed = max_score - score
    msg = (
        f"🚫 CONFLUÊNCIA BLOQUEADA {symbol_root} {tf} {direction}\n"
        f"| score {score}/{max_score} ({mark_str})\n"
        f"| candle: {candle_mark} fib: {fib_mark} htf: {htf_mark} adx: {adx_mark}\n"
        f"| Motivo: {failed} checks falharam, mínimo {min_score}"
    )

    key = f"CONFLUENCE_BLOCK:{symbol_root}:{tf}"
    try:
        return _notify_once(key, msg, send_fn=send_fn, cooldown_min=5, force=False)
    except Exception:
        return False
