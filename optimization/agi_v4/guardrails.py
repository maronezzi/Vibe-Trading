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
  - ``classify_disabled_timeframes_change()``: Wave AGI-soberano (01/08) —
    o AGI é soberano: pode PAUSAR e DESPAUSAR TFs de ``disabled_timeframes``.
    Antes (Lei 2 original) só podia pausar, deixando pares lucrativos
    bloqueados. Agora: se validou lucratividade (stage5), reativa.
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
    # Trailing (mult OU distance). Wave 880.F (07/08): range ampliado p/ poder
    # apertar em symbols point<1.0 (WDO/WSP/BIT) onde 1 ATR em preço ~ R$8000+
    # e a distância de trailing precisa ser fração pequena de ATR (ex 0.05).
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.trail_(?:mult|distance)$", float, (0.05, 5.0)),
    # Wave 880.F (07/08): trail_activate — múltiplo de ATR pra ATIVAR o trailing
    # por lucro. Range amplo (0.01 a 5.0) porque p/ símbolos com point<1.0
    # (WDO/WSP/BIT) 1 ATR em preço ~ R$8000+ e o valor precisa ser pequeno
    # (ex: 0.01-0.05) pra ativar em lucro razoável. AGI tune por (symbol, tf).
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.trail_activate$", float, (0.0005, 5.0)),
    # Breakeven (r OU mult). Range mais apertado — breakeven muito agressivo
    # custa edge (PnL realizável encolhe).
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.breakeven_(?:r|mult)$", float, (0.5, 3.0)),
    # Limite diário de trades por (symbol, tf) — aceita ``daily_trade_count``
    # ou ``max_daily_trade_count`` (convenção histórica no config).
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.(?:max_)?daily_trade_count$", int, (1, 50)),
    # ── Wave N+2A (2026-07-08): TP1 + ATR trailing.
    # tp1_r: múltiplo de R pra disparar TP1 (1.0 = "1R de profit", 1.5 = "1.5R").
    # tp1_pct: fração da posição a fechar em TP1 (0.5 = metade).
    # atr_trail_mult: multiplicador ATR pro trailing após TP1.
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.tp1_r$", float, (0.5, 3.0)),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.tp1_pct$", float, (0.1, 0.9)),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.atr_trail_mult$", float, (0.5, 5.0)),
    # ── Wave 880.B4 (2026-07-19): TP2 ladder — segundo parcial pós-TP1.
    # tp2_r: múltiplo de ATR pra disparar TP2 (default 2.0).
    # tp2_pct: fração do RESTANTE (não do original) a fechar em TP2.
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.tp2_r$", float, (1.5, 4.0)),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.tp2_pct$", float, (0.1, 0.9)),
    # disabled_timeframes: AGI soberano pode pausar E despausar (Wave 01/08).
    # Validação semântica em classify_disabled_timeframes_change.
    (r"^disabled_timeframes$", list, None),
    # day_trade_intent: AGI soberano pode ativar/desativar por par (Wave 01/08).
    # Quando reativa um par lucrativo, precisa ligar day_trade_intent=true.
    (r"^day_trade_intent\.[A-Z]+_(M5|M15|M30|H1)$", bool, None),
    # Volume por símbolo. Range (0, 10] é folgado — B3 mini-contratos vão
    # até ~5; BTC similar. AGI não toca volume_by_symbol.*[volume] num
    # futuro próximo, mas a regra fica preemptiva.
    (r"^volume_by_symbol\.[A-Z]+$", (int, float), (0, 10)),
    # ── Wave N+2B (2026-07-08): sizing vol-scaled.
    # AGI pode tunar parâmetros quantitativos do scaling, mas NÃO o mode
    # (humano-only, ver FORBIDDEN_TARGETS abaixo).
    (r"^sizing\.atr_baseline_period$", int, (60, 1440)),    # 1h..24h
    (r"^sizing\.atr_baseline$", float, (10.0, 500.0)),       # pts (symbol-agnóstico)
    (r"^sizing\.min_scale$", float, (0.1, 1.0)),
    (r"^sizing\.max_scale$", float, (1.0, 5.0)),
    (r"^sizing\.atr_warmup_bars$", int, (10, 500)),
    # ── Wave N+3A (2026-07-08): MTF confluence score threshold.
    # Range apertado: <0.5 rejeita quase tudo, >0.9 aceita quase nada útil.
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.min_confluence_score$", float, (0.4, 0.9)),
    # ── Wave N+5 (2026-07-09): desbloquear tuning de indicadores core.
    # AGI encontrou candidatos reais (WIN_M15 RSI_REVERSION +65% PnL,
    # WIN_M5 BOLLINGER +38%) bloqueados por esta whitelist ser conservadora.
    # Ranges apertados para evitar overfitting — AGI já exige PF>=1.2 e
    # walk-forward consistency no gate de aplicação.
    # Timing / cooldown
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.cooldown_seconds$", int, (60, 3600)),
    # RSI
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.rsi_period$", int, (5, 30)),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.rsi_overbought$", int, (50, 95)),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.rsi_oversold$", int, (5, 50)),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.rsi_pullback_level$", int, (20, 60)),
    # Bollinger Bands
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.bb_period$", int, (10, 30)),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.bb_std$", float, (1.0, 3.0)),
    # Keltner Channel (paralelo a BB)
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.kc_period$", int, (10, 30)),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.kc_atr_mult$", float, (1.0, 3.0)),
    # ADX
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.adx_period$", int, (10, 20)),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.adx_threshold$", int, (15, 35)),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.adx_min$", int, (10, 25)),
    # MACD
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.macd_fast$", int, (5, 30)),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.macd_slow$", int, (10, 50)),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.macd_signal$", int, (5, 20)),
    # EMA
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.ema_fast$", int, (5, 30)),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.ema_slow$", int, (10, 100)),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.ema_period$", int, (10, 200)),
    # ATR period (alguns strategies usam)
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.atr_period$", int, (7, 30)),
    # Donchian
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.donchian_period$", int, (10, 50)),
    # VWAP thresholds (em torno de 1.0, range apertado)
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.vwap_period$", int, (5, 50)),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.vwap_buy_threshold$", float, (1.000, 1.020)),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.vwap_sell_threshold$", float, (0.980, 1.000)),
    # Volume filter
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.vol_ratio_min$", float, (0.1, 1.0)),
    # Pullback / touch (fração)
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.pullback_pct$", float, (0.0, 0.5)),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.touch_pct$", float, (0.001, 0.05)),
    # Confluence toggles (on/off por check)
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.candle_required$", bool, None),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.fib_required$", bool, None),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.htf_required$", bool, None),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.adx_required$", bool, None),
    # Exit timing
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.max_position_minutes$", int, (15, 180)),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.breakeven_minutes$", int, (3, 30)),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.hard_exit_minutes$", int, (30, 180)),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.time_trail_minutes$", int, (10, 60)),
    # RSI Long bands (WIN_M15 strategies)
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.min_rsi_long$", int, (0, 100)),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.max_rsi_long$", int, (0, 100)),
    # ── Wave Melhoria 1+2 (Bruno 12/07): circuit breaker + profit-lock.
    # max_consecutive_losses: após N losses seguidas no slot, pausa (1=ultra
    # agressivo, 999=off). Range [1, 999] permite ao AGI desligar se necessário.
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.max_consecutive_losses$", int, (1, 999)),
    # halt_duration_minutes: tempo de pausa após circuit breaker disparar.
    # Range [15, 240] = 15min a 4h (cobre intraday sem travar o dia todo).
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.halt_duration_minutes$", int, (15, 240)),
    # profit_lock_r: fração de R (risco inicial) que dispara lock zero-loss.
    # 0.0 = off. Range [0.0, 1.5] — 1.5R permite lock tardio para trend-follow.
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.profit_lock_r$", float, (0.0, 1.5)),
    # ── Wave LLM-AGI (Bruno 17/07): params canônicos faltantes, usados por
    # múltiplas estratégias mainstream. Libera a LLM a sugerir ajustes nelas
    # (antes esses params caiam em default-deny e a sugestão era rejeitada).
    # lookback: janela de lookback para high/low/zscore/etc (DIVERGENCE_RSI,
    # MEAN_REVERSION_ZSCORE, MOMENTUM_BREAKOUT, RANGE_TRADING, VWAP_RECLAIM).
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.lookback$", int, (5, 100)),
    # multiplier: fator ATR do Supertrend (1.0-5.0 cobre clássico 2.0-3.0).
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.multiplier$", float, (1.0, 5.0)),
    # max_ema_distance_pct: distância máx. do preço à EMA (filtro de sobre-extensão).
    # Usado por RSI_REVERSION e VOLATILITY_MEAN_REVERSION. Range [0.5, 15.0] %.
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.max_ema_distance_pct$", float, (0.5, 15.0)),
    # max_ema_dist: alias usado por WIN_REVERSION (mesma semântica).
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.max_ema_dist$", float, (0.5, 15.0)),
    # ema_mid: EMA intermediária do TRIPLE_EMA.
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.ema_mid$", int, (10, 60)),
    # period / exit_period: Donchian (high/low lookback + exit lookback).
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.period$", int, (5, 50)),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.exit_period$", int, (3, 30)),
    # z_threshold: limiar de Z-score do MEAN_REVERSION_ZSCORE (típico 1.5-3.0).
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.z_threshold$", float, (1.0, 4.0)),
    # roc_period / roc_threshold: MOMENTUM_BREAKOUT (rate of change).
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.roc_period$", int, (5, 30)),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.roc_threshold$", float, (0.001, 0.05)),
    # rsi_high / rsi_low: bands RSI do OPENING_HOUR_EDGE (simétricas ao over/sold).
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.rsi_high$", int, (60, 95)),
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.rsi_low$", int, (5, 40)),
    # range_atr_pct: fração ATR do RANGE_TRADING (largura do range em ATR).
    (r"^params_by_tf\.[A-Z]+_(M5|M15|M30|H1)\.range_atr_pct$", float, (0.2, 3.0)),
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
    """Wave AGI-soberano (Bruno 01/08): o AGI é soberano — pode tanto PAUSAR
    (adicionar) quanto DESPAUSAR (remover) entradas de ``disabled_timeframes``.

    Antes (Lei 2 original): AGI só podia pausar, nunca despausar (unpause era
    decisão humana). Mas isso deixava pares lucrativos bloqueados — o AGI
    validava que WSP/WDO tinham edge (após a correção de mult), otimizava a
    estratégia, mas não podia reativar o par. Decisão do Bruno: se o AGI
    validou lucratividade (passou profitability + walk-forward + regra1 no
    stage5), ele tem autoridade para reativar.

    Args:
        current: lista atual de TFs pausados no config.
        proposed: lista que o AGI quer escrever.

    Returns:
        ``(True, "ok")`` sempre — AGI soberano pode pausar e despausar.
    """
    cur_set = set(current or [])
    prop_set = set(proposed or [])
    removed = sorted(cur_set - prop_set)
    added = sorted(prop_set - cur_set)
    if removed and not added:
        return True, f"AGI-soberano: despausou {removed} (lucratividade validada)"
    if added and not removed:
        return True, f"AGI-soberano: pausou {added}"
    if removed and added:
        return True, f"AGI-soberano: pausou {added}, despausou {removed}"
    return True, "ok"


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

