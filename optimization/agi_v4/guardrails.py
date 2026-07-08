"""
guardrails.py — AGI v4 guardrails (Wave 875.G, 2026-07-08).

Substitui o conceito aspiracional ``_SAFE_TARGETS`` da §18.2 do plano
PLAN_REFATOR_PROXIMAS_WAVES_2026-07-08.md (Errata 1, §0.5).

Antes desta wave, o AGI v4 (``optimization/agi_v4/stage5_apply.py``)
tinha write-livre em ``vt_config.json`` inteiro — o único gate real era
``cand_pnl <= 0`` em L68-70 (performance, não escopo). O módulo estava
na ``ALLOWED_WRITERS`` do ``vt_config_loader``, mas a whitelist é de
**módulo**, não de **chave**.

Esta wave adiciona:
  - ``SAFE_WRITE_TARGETS``: whitelist explícita (regex + tipo + range) dos
    únicos caminhos que o AGI pode escrever.
  - ``FORBIDDEN_TARGETS``: hard wall de chaves NUNCA tocáveis (kill switches,
    metadata, identidade do bot, sizing.mode humano-only).
  - ``validate_write_target()``: gate atômico — default-deny. Se a chave não
    está na whitelist E não é forbidden (caiu em default-deny), rejeita.
  - ``classify_disabled_timeframes_change()``: exceção controlada à Lei 2
    — AGI pode PAUSAR (adicionar) TFs à ``disabled_timeframes``, mas NUNCA
    DESPAUSAR (remover). Decisão documentada na §18.2 do plano.
  - ``normalize_target_key()``: helper para construir caminhos
    ``"a.b.c"`` estáveis a partir de segmentos.

Default-deny é a postura. O gate é fail-safe — qualquer chave não
explicitamente listada é rejeitada.

DOC: ``docs/PLAN_REFATOR_PROXIMAS_WAVES_2026-07-08.md`` §18.2.
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("agi_v4.guardrails")


# ─── Whitelist ─────────────────────────────────────────────────────────────
# Formato: (regex_path, expected_type, value_range_or_None).
#   - regex_path: regex anchored (^...$) que casa com o caminho normalizado
#     "section.key.subkey".
#   - expected_type: type ou tuple de types. bool NÃO casa com int/float
#     (ver _type_matches — bool é subclass de int em Python, mas rejeitamos
#     para evitar que ``True`` vire ``1`` silenciosamente).
#   - value_range: tuple (lo, hi) inclusivo para tipos numéricos;
#     ``None`` = sem range (ex.: strings de nome de estratégia).
#
# Default-deny: qualquer ``key_path`` que não case com nenhum dos regex é
# rejeitado. Adicionar uma chave aqui é decisão deliberada, não convenção.

SAFE_WRITE_TARGETS: list[tuple[str, type | tuple[type, ...], tuple[float, float] | None]] = [
    # Estratégia por (symbol, tf). Aceita string (nome da estratégia).
    (r"^strategy_by_tf\.[A-Z]+_(M5|M15|M30|H1)$", str, None),
    # SL em múltiplos de ATR. Range conservador (Lei 5: não blow up risk).
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.sl_atr_mult$", float, (0.5, 5.0)),
    # Trailing (mult OU distance). Range idêntico ao sl_atr_mult.
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.trail_(?:mult|distance)$", float, (0.5, 5.0)),
    # Breakeven (r OU mult). Range mais apertado — breakeven muito agressivo
    # custa edge (PnL realizável encolhe).
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.breakeven_(?:r|mult)$", float, (0.5, 3.0)),
    # Limite diário de trades por (symbol, tf) — aceita ``daily_trade_count``
    # ou ``max_daily_trade_count`` (convenção histórica no config).
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.(?:max_)?daily_trade_count$", int, (1, 50)),
    # disabled_timeframes: AGI pode PAUSAR (adicionar), nunca DESPAUSAR
    # (remover). Validação semântica em classify_disabled_timeframes_change.
    (r"^disabled_timeframes$", list, None),
    # Volume por símbolo. Range (0, 10] é folgado — B3 mini-contratos vão
    # até ~5; BTC similar. AGI não toca volume_by_symbol.*[volume] num
    # futuro próximo, mas a regra fica preemptiva.
    (r"^volume_by_symbol\.[A-Z]+$", (int, float), (0, 10)),
]


# ─── Hard wall ─────────────────────────────────────────────────────────────
# Chave NUNCA escrita pelo AGI, mesmo em dry-run. Default-deny reforçado.
# Tudo aqui é:
#   - decisão humana (sizing.mode, magic, horários de operação),
#   - metadata interna do loader (_version, _updated_by),
#   - kill switch de risco (max_daily_loss, halt_*),
#   - convenção de código (Lei 2 — disabled_symbols: nunca desabilita
#     símbolo inteiro, só TF por símbolo).
#
# Match é EXATO contra top-level key OU full path. ``"sizing.mode"`` casa
# com full path; ``"max_daily_loss"`` casa com top-level.

FORBIDDEN_TARGETS: set[str] = {
    # Metadata interna (loader)
    "_version", "_updated_at", "_updated_by", "_notes",
    # Identidade do bot — humano decide
    "magic",
    # Horário de operação — humano decide
    "start_hour", "start_minute", "close_hour", "close_minute",
    # Kill switches de risco — humano decide
    "max_daily_loss", "halt_trading", "halt_new_trades", "halt_on_loss",
    "pause_criteria", "max_daily_trades",
    # Sizing mode: humano decide (static vs vol_scaled). Sub-chaves
    # numéricas (atr_baseline, min_scale, max_scale) NÃO estão aqui —
    # são adicionadas ao whitelist em Wave N+2B.
    "sizing.mode",
    # Timing de runtime — não é param de trading
    "check_interval", "bars_count", "warmup_minutes", "winddown_minutes",
    # Lei 2: nunca desabilita SÍMBOLO inteiro. Pause é por TF via
    # disabled_timeframes (whitelist separada com semantic check).
    "disabled_symbols",
    # LLM validator flag — humano decide on/off
    "validate_with_llm",
}


# ─── Helpers ───────────────────────────────────────────────────────────────


def normalize_target_key(*parts: Any) -> str:
    """Junta segmentos de um caminho com '.'.

    Aceita qualquer quantidade de partes; cada parte é convertida via
    ``str()``. Útil para construir ``"params_by_tf.WIN_M5.sl_atr_mult"``
    a partir de ``("params_by_tf", "WIN_M5", "sl_atr_mult")``.

    >>> normalize_target_key("strategy_by_tf", "WIN_M5")
    'strategy_by_tf.WIN_M5'
    >>> normalize_target_key("a", "b", "c")
    'a.b.c'
    """
    return ".".join(str(p) for p in parts)


def classify_disabled_timeframes_change(
    current: list, proposed: list
) -> tuple[bool, str]:
    """Lei 2 (directional): AGI pode ADICIONAR entradas a
    ``disabled_timeframes`` (pause), nunca REMOVER (unpause é decisão
    humana — implica confiança no renascimento de edge).

    Args:
        current: lista atual de TFs pausados no config.
        proposed: lista que o AGI quer escrever.

    Returns:
        ``(True, "ok")`` se ``proposed ⊇ current`` (ou igual).
        ``(False, reason)`` se ``proposed`` tenta remover alguma entry de
        ``current`` (Lei 2 violada).
    """
    cur_set = set(current or [])
    prop_set = set(proposed or [])
    if prop_set.issuperset(cur_set):
        return True, "ok"
    removed = sorted(cur_set - prop_set)
    return (
        False,
        f"disabled_timeframes: tentativa de remover {removed} "
        f"(Lei 2: AGI só pausa, unpause é humano)",
    )


def _type_matches(value: Any, expected: type | tuple[type, ...]) -> bool:
    """Type check estrito — bool NÃO casa com int/float.

    Em Python, ``isinstance(True, int)`` é True (bool é subclass de int).
    Sem este guard, ``volume_by_symbol.WIN = True`` seria aceito como int
    (= 1), o que é silenciosamente errado. Rejeitamos explicitamente.
    """
    if isinstance(expected, tuple):
        return any(_type_matches(value, t) for t in expected)
    if expected is float:
        return isinstance(value, float) and not isinstance(value, bool)
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected is list:
        return isinstance(value, list)
    if expected is str:
        return isinstance(value, str) and not isinstance(value, bool)
    return isinstance(value, expected)


def _format_type_name(expected: type | tuple[type, ...]) -> str:
    if isinstance(expected, tuple):
        return "|".join(t.__name__ for t in expected)
    return expected.__name__


def validate_write_target(
    key_path: str,
    value: Any,
    current_config: dict | None = None,
) -> tuple[bool, str]:
    """Gate W875.G: valida um write contra ``SAFE_WRITE_TARGETS`` e
    ``FORBIDDEN_TARGETS``. Default-deny.

    Args:
        key_path: caminho normalizado ``"section.key"`` ou
            ``"section.key.subkey"``. Ex.: ``"strategy_by_tf.WIN_M5"``,
            ``"params_by_tf.WIN_M5.sl_atr_mult"``.
        value: valor a ser escrito.
        current_config: dict completo do config (necessário para
            validação semântica de ``disabled_timeframes``: AGI pode
            adicionar mas não remover). Opcional para os demais
            caminhos — se ausente e ``key_path`` é
            ``"disabled_timeframes"``, treat current como lista vazia
            (toda proposta não-vazia seria superset, permitido).

    Returns:
        ``(True, "ok")`` se o write é permitido.
        ``(False, reason)`` se rejeitado. ``reason`` é uma string
            humana-legível pronta para log/Telegram.
    """
    if not isinstance(key_path, str) or not key_path:
        return False, f"key_path inválido: {key_path!r}"

    top = key_path.split(".", 1)[0]

    # 1. Hard wall — match exato contra top-level key OU full path.
    #    Cobre tanto "max_daily_loss" (top-level) quanto "sizing.mode"
    #    (nested path exato).
    if top in FORBIDDEN_TARGETS or key_path in FORBIDDEN_TARGETS:
        return (
            False,
            f"target '{key_path}' está em FORBIDDEN_TARGETS (decisão humana)",
        )

    # 2. Special case: disabled_timeframes (semantic check, não regex).
    if key_path == "disabled_timeframes":
        if not isinstance(value, list):
            return (
                False,
                f"disabled_timeframes deve ser list, recebeu "
                f"{type(value).__name__}",
            )
        current = (current_config or {}).get("disabled_timeframes", []) or []
        return classify_disabled_timeframes_change(current, value)

    # 3. Whitelist — regex + type + range.
    for pattern, expected_type, value_range in SAFE_WRITE_TARGETS:
        if re.match(pattern, key_path):
            if not _type_matches(value, expected_type):
                return (
                    False,
                    f"target '{key_path}' valor {type(value).__name__} "
                    f"não é {_format_type_name(expected_type)}",
                )
            if (
                value_range is not None
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            ):
                lo, hi = value_range
                if value < lo or value > hi:
                    return (
                        False,
                        f"target '{key_path}'={value} fora do range "
                        f"[{lo}, {hi}]",
                    )
            return True, "ok"

    # 4. Default-deny — não está em whitelist nem forbidden, então bloqueia.
    return (
        False,
        f"target '{key_path}' não está em SAFE_WRITE_TARGETS (default-deny)",
    )


class GuardrailReject(Exception):
    """Levantada quando o gate W875.G rejeita um write do AGI.

    Capturada por ``stage5_apply._apply_one`` (try/except genérico em
    volta de ``_write_to_config``) e mapeada para ``gate="guardrail_reject"``
    no resultado — candidato vai para ``rejected_changes`` em vez de
    ``applied_changes``.
    """
    def __init__(self, reason: str, key_path: str = ""):
        self.reason = reason
        self.key_path = key_path
        super().__init__(reason)


__all__ = [
    "SAFE_WRITE_TARGETS",
    "FORBIDDEN_TARGETS",
    "normalize_target_key",
    "classify_disabled_timeframes_change",
    "validate_write_target",
    "validate_target_block",
    "GuardrailReject",
]


def validate_target_block(
    target: dict,
    current_config: dict,
) -> None:
    """Valida um bloco ``target`` inteiro (formato do AGI v4).

    Itera todas as chaves em ``strategy_by_tf`` e ``params_by_tf`` e chama
    ``validate_write_target(key, value, current_config)``. Se QUALQUER chave
    violar um gate, levanta ``GuardrailReject`` com a primeira razão (curto-
    circuito — não há atomicidade parcial entre strategy e params).

    Args:
        target: dict no formato ``{"strategy_by_tf": {...}, "params_by_tf": {...}}``
            (saída de ``_build_change`` em ``stage5_apply.py``).
        current_config: config carregado do disco via ``load_config(force=True)``
            — necessário para o check direcional de ``disabled_timeframes``.

    Raises:
        GuardrailReject: primeira chave que violar guardrail.
    """
    flat = []
    for sym_tf, strat in (target.get("strategy_by_tf") or {}).items():
        flat.append((normalize_target_key("strategy_by_tf", sym_tf), strat))
    for sym_tf, params in (target.get("params_by_tf") or {}).items():
        for k, v in (params or {}).items():
            flat.append((normalize_target_key("params_by_tf", sym_tf, k), v))

    for key_path, value in flat:
        ok, reason = validate_write_target(key_path, value, current_config)
        if not ok:
            raise GuardrailReject(reason=reason, key_path=key_path)

