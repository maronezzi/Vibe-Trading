"""
test_agi_guardrails.py — Wave 875.G gate tests (2026-07-08).

Substitui o conceito aspiracional ``_SAFE_TARGETS`` da §18.2 do plano
PLAN_REFATOR_PROXIMAS_WAVES_2026-07-08.md (Errata 1, §0.5) por um gate
real: ``SAFE_WRITE_TARGETS`` (whitelist) + ``FORBIDDEN_TARGETS`` (hard
wall) + ``validate_write_target`` (default-deny).

O módulo AGI v4 era write-livre em ``vt_config.json`` inteiro (restrito
só pelo gate de PnL ≤ 0 em L68-70 de ``stage5_apply.py``). Estes testes
documentam e fixam a nova postura — qualquer write fora da whitelist é
rejeitado.

Estrutura dos testes:
  1. Whitelist aceita paths canônicos (strategy_by_tf, params_by_tf).
  2. Range rejects valores fora de (lo, hi).
  3. FORBIDDEN wall rejects chaves críticas (max_daily_loss, magic,
     sizing.mode, disabled_symbols, …).
  4. Default-deny rejeita paths não listados.
  5. disabled_timeframes: directional Lei 2 — AGI pode adicionar
     (pause), nunca remover (unpause humano).
  6. Sanity do contrato: ``validate_write_target`` retorna
     ``tuple[bool, str]``.
"""
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))  # noqa: E402
sys.path.insert(0, str(PROJECT_DIR / "optimization"))  # noqa: E402

from optimization.agi_v4.guardrails import (  # noqa: E402
    SAFE_WRITE_TARGETS,
    FORBIDDEN_TARGETS,
    normalize_target_key,
    validate_write_target,
    classify_disabled_timeframes_change,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Whitelist: strategy_by_tf e params_by_tf permitidos
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_safe_target_allowed_strategy_by_tf():
    """strategy_by_tf.<SYM>_<TF> = <strategy_name> é aceito (string, sem range)."""
    ok, reason = validate_write_target(
        "strategy_by_tf.WIN_M5", "BOLLINGER", current_config={}
    )
    assert ok is True
    assert reason == "ok"


def test_safe_target_allowed_params_sl_atr_mult_in_range():
    """params_by_tf.<SYM>_<TF>.sl_atr_mult = float ∈ [0.5, 5.0] é aceito."""
    ok, reason = validate_write_target(
        "params_by_tf.WDO_M15.sl_atr_mult", 1.5, current_config={}
    )
    assert ok is True
    assert reason == "ok"


def test_params_sl_atr_mult_out_of_range_rejected():
    """params_by_tf.<SYM>_<TF>.sl_atr_mult = 10.0 (acima do range) é rejeitado."""
    ok, reason = validate_write_target(
        "params_by_tf.WIN_M5.sl_atr_mult", 10.0, current_config={}
    )
    assert ok is False
    assert "fora do range" in reason
    assert "[0.5, 5.0]" in reason


def test_params_trailing_out_of_range_rejected():
    """trail_mult=10.0 também rejeitado — mesma range sl_atr_mult."""
    ok, reason = validate_write_target(
        "params_by_tf.BIT_M30.trail_mult", 10.0, current_config={}
    )
    assert ok is False
    assert "fora do range" in reason


def test_params_breakeven_tighter_range_rejected():
    """breakeven_r=4.0 rejeitado — range é [0.5, 3.0] (mais apertado)."""
    ok, reason = validate_write_target(
        "params_by_tf.WSP_M5.breakeven_r", 4.0, current_config={}
    )
    assert ok is False
    assert "[0.5, 3.0]" in reason


def test_params_daily_trade_count_zero_rejected():
    """daily_trade_count=0 rejeitado — range [1, 50]."""
    ok, reason = validate_write_target(
        "params_by_tf.WIN_M5.daily_trade_count", 0, current_config={}
    )
    assert ok is False
    assert "fora do range" in reason


def test_params_max_daily_trade_count_in_range_accepted():
    """Convenção ``max_daily_trade_count`` (com prefixo) também é aceita."""
    ok, reason = validate_write_target(
        "params_by_tf.WDO_H1.max_daily_trade_count", 10, current_config={}
    )
    assert ok is True
    assert reason == "ok"


def test_volume_by_symbol_in_range_accepted():
    """volume_by_symbol.<SYM> = int|float ∈ [0, 10] é aceito."""
    ok, reason = validate_write_target(
        "volume_by_symbol.WIN", 1.0, current_config={}
    )
    assert ok is True
    assert reason == "ok"


def test_volume_by_symbol_out_of_range_rejected():
    """volume_by_symbol.WIN=15.0 rejeitado — range [0, 10]."""
    ok, reason = validate_write_target(
        "volume_by_symbol.WIN", 15.0, current_config={}
    )
    assert ok is False
    assert "fora do range" in reason


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FORBIDDEN_TARGETS: hard wall
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_forbidden_target_rejected_max_daily_loss():
    """max_daily_loss (kill switch) é humano-only — rejeitado sempre."""
    ok, reason = validate_write_target(
        "max_daily_loss", -500, current_config={}
    )
    assert ok is False
    assert "FORBIDDEN_TARGETS" in reason
    assert "decisão humana" in reason


def test_forbidden_target_rejected_magic():
    """magic (identidade do bot) é humano-only — rejeitado sempre."""
    ok, reason = validate_write_target(
        "magic", 555501, current_config={}
    )
    assert ok is False
    assert "FORBIDDEN_TARGETS" in reason


def test_forbidden_target_rejected_sizing_mode():
    """sizing.mode (humano decide static vs vol_scaled) — rejeitado.

    Nested path exato — match contra FORBIDDEN pelo full path, não só
    top-level. Garante que ``sizing.atr_baseline`` (não-forbidden, futuro
    alvo da Wave N+2B) não é rejeitado por top-level match errado.
    """
    ok, reason = validate_write_target(
        "sizing.mode", "vol_scaled", current_config={}
    )
    assert ok is False
    assert "FORBIDDEN_TARGETS" in reason


def test_forbidden_target_rejected_disabled_symbols():
    """disabled_symbols: Lei 2 — AGI nunca desabilita SÍMBOLO inteiro."""
    ok, reason = validate_write_target(
        "disabled_symbols", ["IND"], current_config={}
    )
    assert ok is False
    assert "FORBIDDEN_TARGETS" in reason


def test_forbidden_target_rejected_metadata():
    """_version, _updated_by: metadata interna do loader — humano/sistema."""
    for key in ("_version", "_updated_by", "_updated_at", "_notes"):
        ok, reason = validate_write_target(key, "anything", current_config={})
        assert ok is False, f"{key} deveria ser forbidden"
        assert "FORBIDDEN_TARGETS" in reason


def test_forbidden_target_rejected_halt_switches():
    """halt_* e pause_criteria: kill switches — humano decide."""
    for key in ("halt_trading", "halt_new_trades", "halt_on_loss", "pause_criteria"):
        ok, reason = validate_write_target(key, True, current_config={})
        assert ok is False, f"{key} deveria ser forbidden"


def test_forbidden_target_rejected_session_hours():
    """start_hour, close_hour, etc.: humano decide janela operacional."""
    for key in ("start_hour", "start_minute", "close_hour", "close_minute"):
        ok, reason = validate_write_target(key, 9, current_config={})
        assert ok is False, f"{key} deveria ser forbidden"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Default-deny: paths não listados
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_unknown_target_rejected_default_deny():
    """Path desconhecido cai em default-deny — message explícita."""
    ok, reason = validate_write_target(
        "some_random_thing", 42, current_config={}
    )
    assert ok is False
    assert "default-deny" in reason


def test_unknown_strategy_by_tf_pair_rejected():
    """strategy_by_tf com par lowercase ou TF inválido → default-deny."""
    for bad in ("strategy_by_tf.win_m5", "strategy_by_tf.WIN_M1", "strategy_by_tf.WIN"):
        ok, reason = validate_write_target(bad, "BOLLINGER", current_config={})
        assert ok is False, f"{bad} deveria ser rejeitado"
        assert "default-deny" in reason


def test_unknown_params_subkey_rejected():
    """params_by_tf.<pair>.foo (sub-key não whitelistada) → default-deny."""
    ok, reason = validate_write_target(
        "params_by_tf.WIN_M5.foo", 1.5, current_config={}
    )
    assert ok is False
    assert "default-deny" in reason


def test_wrong_type_rejected():
    """Tipo errado no valor → rejeitado (mesmo em path whitelistado)."""
    # strategy_by_tf espera string, não int.
    ok, reason = validate_write_target(
        "strategy_by_tf.WIN_M5", 42, current_config={}
    )
    assert ok is False
    assert "não é str" in reason

    # sl_atr_mult espera float, não string.
    ok, reason = validate_write_target(
        "params_by_tf.WIN_M5.sl_atr_mult", "1.5", current_config={}
    )
    assert ok is False
    assert "não é float" in reason

    # daily_trade_count espera int, não float.
    ok, reason = validate_write_target(
        "params_by_tf.WIN_M5.daily_trade_count", 5.5, current_config={}
    )
    assert ok is False
    assert "não é int" in reason


def test_bool_rejected_for_numeric_fields():
    """bool NÃO casa com int/float (Python subclass trap — testado explicitamente)."""
    # True/False NÃO devem passar como int (= 1/0).
    ok, reason = validate_write_target(
        "volume_by_symbol.WIN", True, current_config={}
    )
    assert ok is False, "bool True não deve passar como int/float"

    ok, reason = validate_write_target(
        "params_by_tf.WIN_M5.sl_atr_mult", False, current_config={}
    )
    assert ok is False, "bool False não deve passar como float"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# disabled_timeframes: Lei 2 directional (pause OK, unpause NO)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_disabled_timeframes_addition_allowed():
    """AGI pode ADICIONAR entries a disabled_timeframes (Lei 2: pause OK)."""
    current = ["BIT_M5", "BIT_M30"]
    proposed = ["BIT_M5", "BIT_M30", "WDO_M5"]
    ok, reason = validate_write_target(
        "disabled_timeframes", proposed, current_config={"disabled_timeframes": current}
    )
    assert ok is True
    assert reason == "ok"


def test_disabled_timeframes_removal_rejected():
    """AGI NÃO pode REMOVER entries de disabled_timeframes (Lei 2: unpause humano)."""
    current = ["BIT_M5", "BIT_M30", "WDO_M5"]
    proposed = ["BIT_M5"]  # tentou remover BIT_M30 e WDO_M5
    ok, reason = validate_write_target(
        "disabled_timeframes", proposed, current_config={"disabled_timeframes": current}
    )
    assert ok is False
    assert "Lei 2" in reason or "unpause" in reason
    # reason deve listar as entries removidas
    assert "BIT_M30" in reason or "WDO_M5" in reason


def test_disabled_timeframes_same_set_allowed():
    """Re-escrever a mesma lista (idempotente) é permitido."""
    current = ["BIT_M5", "WDO_M5"]
    proposed = ["BIT_M5", "WDO_M5"]
    ok, reason = validate_write_target(
        "disabled_timeframes", proposed, current_config={"disabled_timeframes": current}
    )
    assert ok is True
    assert reason == "ok"


def test_disabled_timeframes_empty_to_nonempty_allowed():
    """current vazio + proposed não-vazio: superset trivial — permitido."""
    ok, reason = validate_write_target(
        "disabled_timeframes",
        ["BIT_M5"],
        current_config={"disabled_timeframes": []},
    )
    assert ok is True


def test_disabled_timeframes_non_list_rejected():
    """disabled_timeframes deve ser list, não string/dict/int."""
    for bad in ("BIT_M5", 42, {"BIT_M5": True}, None):
        ok, reason = validate_write_target(
            "disabled_timeframes", bad, current_config={}
        )
        assert ok is False, f"{type(bad).__name__} deveria ser rejeitado"
        assert "deve ser list" in reason


def test_classify_disabled_timeframes_change_direct():
    """Helper classify_disabled_timeframes_change é testável diretamente."""
    # Adicionar: OK
    ok, reason = classify_disabled_timeframes_change(["A"], ["A", "B"])
    assert ok is True

    # Remover: FAIL
    ok, reason = classify_disabled_timeframes_change(["A", "B"], ["A"])
    assert ok is False
    assert "B" in reason

    # Igual: OK
    ok, reason = classify_disabled_timeframes_change(["A"], ["A"])
    assert ok is True

    # current vazio, proposed qualquer: OK
    ok, reason = classify_disabled_timeframes_change([], ["X"])
    assert ok is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Helpers & contrato
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_validate_write_target_returns_tuple():
    """Contrato: retorna SEMPRE tuple[bool, str] (truthy contract)."""
    result = validate_write_target("strategy_by_tf.WIN_M5", "BOLLINGER", {})
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert isinstance(result[0], bool)
    assert isinstance(result[1], str)

    # Também em rejeição
    result = validate_write_target("magic", 555501, {})
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert isinstance(result[0], bool)
    assert isinstance(result[1], str)


def test_normalize_target_key_joins_with_dot():
    """Helper normalize_target_key junta segmentos com '.'."""
    assert normalize_target_key("a", "b") == "a.b"
    assert normalize_target_key("a", "b", "c") == "a.b.c"
    assert normalize_target_key("strategy_by_tf", "WIN_M5") == "strategy_by_tf.WIN_M5"
    assert normalize_target_key("params_by_tf", "WIN_M5", "sl_atr_mult") == (
        "params_by_tf.WIN_M5.sl_atr_mult"
    )


def test_invalid_key_path_rejected():
    """key_path vazio ou não-string → rejeitado (sem match, fail-safe)."""
    ok, reason = validate_write_target("", "x", {})
    assert ok is False
    ok, reason = validate_write_target(None, "x", {})  # type: ignore[arg-type]
    assert ok is False


def test_safe_write_targets_is_not_empty():
    """Smoke: SAFE_WRITE_TARGETS tem as regras mínimas esperadas.

    Não-bloqueante se alguém adicionar mais, mas o conjunto canônico
    tem que existir desde o W875.G — se alguém apagou, isto falha.
    """
    assert len(SAFE_WRITE_TARGETS) >= 6

    patterns = [p for p, _, _ in SAFE_WRITE_TARGETS]
    joined = " ".join(patterns)

    # Regras mínimas canônicas
    assert "strategy_by_tf" in joined
    assert "params_by_tf" in joined
    assert "sl_atr_mult" in joined
    assert "trail_" in joined
    assert "breakeven_" in joined
    assert "daily_trade_count" in joined
    assert "disabled_timeframes" in joined
    assert "volume_by_symbol" in joined


def test_forbidden_targets_is_not_empty():
    """Smoke: FORBIDDEN_TARGETS tem o hard wall mínimo esperado."""
    assert len(FORBIDDEN_TARGETS) >= 15
    expected = {
        "magic", "max_daily_loss", "halt_trading",
        "disabled_symbols", "sizing.mode", "validate_with_llm",
        "_version", "_updated_by", "pause_criteria",
        "start_hour", "close_hour",
    }
    missing = expected - FORBIDDEN_TARGETS
    assert not missing, f"Faltando no FORBIDDEN_TARGETS: {missing}"
