"""param_tuner.py — Tuning de params próprios de estratégias AGI4 (Wave AGI-param-tuning).

PROBLEMA QUE RESOLVE
--------------------
Estratégias NOVAS geradas pelo LLM (Stage 4) nascem com ``params={}`` e nunca
têm seus params próprios otimizados — ``stage4_generate.py`` simula com ``{}``
e anexa ``{}`` em ``search_results``. Os params específicos (``breakout_lookback``,
``retest_atr_mult``, etc.) ficam congelados nos defaults que o LLM escreveu no
``.py``. O Stage 3 não os cobre (só 56 combos universais), e mesmo que cobrisse o
guardrail default-deny bloquearia a escrita.

Este módulo fecha o gap: extrai os params tunable da estratégia (declarados via
``TUNABLE_PARAMS`` no ``.py`` ou via fallback AST dos ``params.get``), gera um
grid, roda ``evaluate_candidate`` para cada combo e retorna o melhor params —
desde que supere os defaults. Também registra os params no guardrail (sanctioned)
para que o Stage 5 possa escrevê-los.

CONTRATO
--------
- ``extract_tunable_params(path) -> {param: {kind, lo, hi, default}}`` (top 5).
- ``sanctioned_spec(path) -> {param: (type, lo, hi)}`` (formato do guardrail).
- ``tune_strategy(sym, tf, name, path, config, thresholds) -> dict | None``.

SEGURANÇA
---------
- Tuning é best-effort: qualquer falha (MT5 indisponível, AST quebra) retorna
  ``None`` → estratégia é anexada com ``params={}`` (estado atual). Nunca derruba
  a geração.
- O registro sanctioned só abre exceção PONTUAL no guardrail para params que a
  própria estratégia declarou. Default-deny é preservado (ver guardrails.py).
"""
from __future__ import annotations

import ast
import itertools
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("agi_v4.param_tuner")

# Params universais de gestão — já tunados pelo Stage 3 (grid UNIVERSAL_PARAMS).
# Excluímos do tuning próprio para não duplicar esforço.
_UNIVERSAL_BLOCKLIST = {
    "sl_atr_mult", "cooldown_seconds", "max_consecutive_losses",
    "halt_duration_minutes", "profit_lock_r",
}

# Cap de combos testados por estratégia (latência: ~1-3s por combo via MT5).
_MAX_TUNING_COMBOS = 40

# Máximo de params próprios tunados por estratégia (contém a combinatória:
# 5 params × 3 valores = 243 → subamostrado para 40).
_MAX_TUNABLE_PARAMS = 5


# ═══════════════════════════════════════════════════════════════════════════
# Extração AST
# ═══════════════════════════════════════════════════════════════════════════

def extract_tunable_params(strategy_path: str | Path) -> dict[str, dict[str, Any]]:
    """Extrai os params tunable de uma estratégia.

    Duas fontes combinadas (prioridade: declaração TUNABLE_PARAMS para ranges;
    fallback AST sempre fornece o default, pois é o que a estratégia lê):

    1. ``TUNABLE_PARAMS = {"param": (tipo, min, max), ...}`` declarado pelo LLM
       no topo do ``.py`` — ranges pensados para a estratégia.
    2. Fallback: ``params.get("x", default)`` extraído via AST. Se TUNABLE_PARAMS
       não declarar o param, infere range conservador (int ±50%, float ±30%).

    Returns:
        ``{param: {"kind": "int"|"float", "lo", "hi", "default"}}`` (top 5,
        excluídos universais). Vazio se não houver params próprios.
    """
    try:
        src = Path(strategy_path).read_text(encoding="utf-8")
        tree = ast.parse(src)
    except Exception as e:
        log.warning(f"extract {strategy_path}: parse AST falhou ({e})")
        return {}

    defaults = _extract_defaults_fallback(tree)  # {param: default} — sempre lidos
    decl = _extract_tunable_decl(tree)            # {param: (kind, lo, hi)} ou {}

    result: dict[str, dict[str, Any]] = {}
    for param, default in defaults.items():
        if param in _UNIVERSAL_BLOCKLIST:
            continue
        if param in decl:
            kind, lo, hi = decl[param]
        else:
            kind, lo, hi = _infer_range(default)
        # Wave AGI-sweep fix (Bruno 12/08): clipsa ao range que o guardrail
        # ACEITA (whitelist estática). Sem isto, o tuner gera valores (ex:
        # ema_fast=4) que o guardrail rejeita por estar fora do range da
        # whitelist ([5,30]) — promoção falha e a melhoria se perde.
        lo, hi, default = _clip_to_guardrail_range(param, lo, hi, default)
        if lo > hi:
            continue  # range aceito não cobre nada útil — param não é tunable
        result[param] = {"kind": kind, "lo": lo, "hi": hi, "default": default}
        if len(result) >= _MAX_TUNABLE_PARAMS:
            break
    return result


def _clip_to_guardrail_range(
    param: str, lo: float, hi: float, default: float
) -> tuple[float, float, float]:
    """Intersecta o range do tuner com o range aceito pelo guardrail.

    Se o param está na whitelist estática (ex: ema_fast ∈ [5,30]), o range do
    tuner é clipsado a esse — o grid só gera valores que passam no guardrail.
    O default também é clipsado (se 9 está dentro, mantém; senão, vira o limite
    mais próximo). Params só-sancionados (não na whitelist) retornam intactos.
    """
    try:
        from optimization.agi_v4.guardrails import accepted_range_for_param
        acc = accepted_range_for_param(param)
    except Exception:
        return lo, hi, default
    if acc is None:
        return lo, hi, default  # param não está na whitelist — usa range do tuner
    _, a_lo, a_hi = acc
    lo = max(lo, a_lo)
    hi = min(hi, a_hi)
    default = max(a_lo, min(a_hi, default))
    return lo, hi, default


def _extract_defaults_fallback(tree: ast.AST) -> dict[str, float]:
    """Extrai ``{param: default}`` de todos os ``params.get("x", default)``.

    Percorre a AST procurando ``Call`` onde ``func`` é ``params.get``. Lê
    ``args[0]`` (nome, string literal) e ``args[1]`` (default, literal numérico).
    Preserva a ORDEM de aparição (insertion-ordered dict) — os primeiros params
    são os mais relevantes (top of check_entry).
    """
    defaults: dict[str, float] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "get"
                and isinstance(func.value, ast.Name) and func.value.id == "params"):
            continue
        if len(node.args) < 2:
            continue
        name_node, default_node = node.args[0], node.args[1]
        if not (isinstance(name_node, ast.Constant) and isinstance(name_node.value, str)):
            continue
        if name_node.value in defaults:
            continue  # primeiro default vence
        try:
            d = ast.literal_eval(default_node)
        except Exception:
            continue
        # Só numéricos (int/float). Bool é subclass de int — rejeitamos.
        if isinstance(d, bool) or not isinstance(d, (int, float)):
            continue
        defaults[name_node.value] = d
    return defaults


def read_param_names(strategy_path: str | Path) -> set[str]:
    """Extrai TODOS os nomes de params que a estratégia LÊ (Wave zombie-fix).

    Diferente de ``_extract_defaults_fallback`` (só numéricos com default), esta
    captura TODO param acessado — via ``params.get("x", ...)`` **ou** ``params["x"]``
    (subscript) — independentemente de tipo/default. Usada para computar o
    keep-set ao limpar params zombie: se a nova estratégia lê o param, ele fica.

    Retorna um set de nomes (sem defaults). Defensiva: ``params["x"]`` sem default
    também é capturado (evita dropar um param que causaria KeyError em runtime).
    """
    names: set[str] = set()
    try:
        src = Path(strategy_path).read_text(encoding="utf-8")
        tree = ast.parse(src)
    except Exception:
        return names
    for node in ast.walk(tree):
        # params.get("x", ...) — Call com func params.get.
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "params"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            names.add(node.args[0].value)
            continue
        # params["x"] — Subscript defensivo (sem default).
        if (isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name) and node.value.id == "params"):
            key = node.slice
            # Python 3.8/3.9: slice é o nó direto; 3.10+ pode envolver ast.Index
            # (removido no 3.9+). Constant str cobre o uso comum params["x"].
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                names.add(key.value)
    return names


def _extract_tunable_decl(tree: ast.AST) -> dict[str, tuple[str, float, float]]:
    """Lê ``TUNABLE_PARAMS = {"param": (tipo, min, max), ...}`` do AST.

    Returns:
        ``{param: (kind, lo, hi)}`` ou ``{}`` se ausente/malformado.
        ``kind`` é ``"int"`` ou ``"float"``.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "TUNABLE_PARAMS":
                return _eval_tunable_dict(node.value)
    return {}


def _eval_tunable_dict(node: ast.AST) -> dict[str, tuple[str, float, float]]:
    """Avalia um nó Dict de TUNABLE_PARAMS no formato {str: (tipo, lo, hi)}."""
    if not isinstance(node, ast.Dict):
        return {}
    result: dict[str, tuple[str, float, float]] = {}
    for key_node, val_node in zip(node.keys, node.values):
        if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
            continue
        entry = _eval_tunable_entry(val_node)
        if entry is not None:
            result[key_node.value] = entry
    return result


def _eval_tunable_entry(node: ast.AST) -> tuple[str, float, float] | None:
    """Avalia ``(tipo, min, max)`` — aceita int/float como Name ou string."""
    if not isinstance(node, (ast.Tuple, ast.List)) or len(node.elts) != 3:
        return None
    type_node, lo_node, hi_node = node.elts
    # Tipo: ast.Name(id="int"|"float") ou string literal "int"/"float".
    if isinstance(type_node, ast.Name):
        kind = type_node.id
    elif isinstance(type_node, ast.Constant) and isinstance(type_node.value, str):
        kind = type_node.value
    else:
        return None
    if kind not in ("int", "float"):
        return None
    try:
        lo = ast.literal_eval(lo_node)
        hi = ast.literal_eval(hi_node)
    except Exception:
        return None
    if isinstance(lo, bool) or not isinstance(lo, (int, float)):
        return None
    if isinstance(hi, bool) or not isinstance(hi, (int, float)):
        return None
    if lo > hi:
        lo, hi = hi, lo  # tolerância: inverte se o LLM trocou
    return (kind, float(lo), float(hi))


def _infer_range(default: float) -> tuple[str, float, float]:
    """Infere range conservador a partir do default (fallback sem TUNABLE_PARAMS).

    int → ±50% (arredondado, mínimo 1); float → ±30%.
    """
    if isinstance(default, int) and not isinstance(default, bool):
        lo = max(1, round(default * 0.5))
        hi = max(lo + 1, round(default * 1.5))
        return ("int", float(lo), float(hi))
    lo = round(default * 0.7, 6)
    hi = round(default * 1.3, 6)
    if lo == hi:
        hi = lo + abs(default) * 0.1 + 0.0001
    return ("float", lo, hi)


# ═══════════════════════════════════════════════════════════════════════════
# Spec para o guardrail
# ═══════════════════════════════════════════════════════════════════════════

def sanctioned_spec(strategy_path: str | Path) -> dict[str, tuple[type, float, float]]:
    """Converte tunables no formato que o guardrail consome.

    Returns:
        ``{param: (python_type, lo, hi)}`` onde ``python_type`` é ``int`` ou
        ``float``. Pronto para ``guardrails.register_sanctioned_params``.
    """
    tunables = extract_tunable_params(strategy_path)
    spec: dict[str, tuple[type, float, float]] = {}
    for param, t in tunables.items():
        py_type = int if t["kind"] == "int" else float
        spec[param] = (py_type, t["lo"], t["hi"])
    return spec


# ═══════════════════════════════════════════════════════════════════════════
# Geração de grid
# ═══════════════════════════════════════════════════════════════════════════

def _generate_grid(tunables: dict[str, dict[str, Any]]) -> list[dict]:
    """Produto cartesiano de {lo, default, hi} por param. Subamostra p/ cap 40.

    Não inclui o combo all-defaults (avaliado à parte como baseline ``params={}``
    em tune_strategy, pois ``params={}`` e ``params={todos: defaults}`` produzem
    backtest idêntico — o plugin lê defaults quando a chave ausente).
    """
    param_values: dict[str, list] = {}
    for param, t in tunables.items():
        kind, d, lo, hi = t["kind"], t["default"], t["lo"], t["hi"]
        if kind == "int":
            vals = sorted({int(round(lo)), int(round(d)), int(round(hi))})
        else:
            vals = sorted({round(lo, 6), round(float(d), 6), round(hi, 6)})
        param_values[param] = list(vals)

    keys = list(param_values.keys())
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*[param_values[k] for k in keys])]

    # Remove o combo all-defaults (redundante com o baseline {}).
    combos = [c for c in combos if not _is_all_defaults(c, tunables)]

    # Cap com subamostragem determinística (preserva extremos).
    if len(combos) > _MAX_TUNING_COMBOS:
        step = len(combos) / _MAX_TUNING_COMBOS
        combos = [combos[int(i * step)] for i in range(_MAX_TUNING_COMBOS)]
    return combos


def _is_all_defaults(combo: dict, tunables: dict[str, dict[str, Any]]) -> bool:
    """True se todos os valores do combo == defaults dos params."""
    return all(combo.get(p) == t["default"] for p, t in tunables.items())


# ═══════════════════════════════════════════════════════════════════════════
# Tuning
# ═══════════════════════════════════════════════════════════════════════════

def tune_strategy(
    sym: str,
    tf: str,
    strategy_name: str,
    strategy_path: str | Path,
    config: dict,
    thresholds: dict | None = None,
) -> dict | None:
    """Otimiza os params próprios de uma estratégia AGI4 via grid search.

    Fluxo:
      1. Extrai tunables (TUNABLE_PARAMS ou fallback AST).
      2. Registra sanctioned no guardrail (Stage 5 poderá escrever).
      3. Avalia o baseline ``params={}`` (defaults do plugin).
      4. Avalia cada combo do grid via ``evaluate_candidate``.
      5. Retorna o melhor params que superou o baseline. Senão ``None``.

    Args:
        sym: root do símbolo (WIN, WDO, BIT, WSP).
        tf: timeframe (M5, M15, M30, H1).
        strategy_name: nome da estratégia (STRATEGY_NAME do plugin).
        strategy_path: path do arquivo ``.py`` da estratégia.
        config: config (para fetch MT5 + contract specs).
        thresholds: thresholds do gates (default: internos do backtest_evaluator).

    Returns:
        ``dict`` de params otimizado (superior aos defaults), ou ``None`` se
        nenhum combo superar o default ou houver falha (best-effort).
    """
    # 1. Extrair tunables.
    try:
        tunables = extract_tunable_params(strategy_path)
    except Exception as e:
        log.warning(f"tune {strategy_name}: extract falhou ({e}) — sem tuning")
        return None
    if not tunables:
        log.debug(f"tune {strategy_name}: sem params próprios tunable — skip")
        return None

    # 2. Registrar sanctioned no guardrail (para o Stage 5 poder escrever).
    try:
        from optimization.agi_v4.guardrails import register_sanctioned_params
        spec = {p: (int if t["kind"] == "int" else float, t["lo"], t["hi"])
                for p, t in tunables.items()}
        register_sanctioned_params(strategy_name, spec)
    except Exception as e:
        log.warning(f"tune {strategy_name}: register sanctioned falhou ({e}) "
                    f"— Stage 5 pode rejeitar a escrita")

    # 3. Avaliar baseline (params={} = defaults do plugin).
    try:
        from optimization.agi_v4.backtest_evaluator import evaluate_candidate
    except ImportError:
        log.warning(f"tune {strategy_name}: backtest_evaluator indisponível")
        return None

    try:
        base = evaluate_candidate(sym, tf, strategy_name, {}, config, thresholds=thresholds)
    except Exception as e:
        log.warning(f"tune {strategy_name}: baseline falhou ({e}) — sem tuning")
        return None
    if not base.get("passed"):
        log.debug(f"tune {strategy_name}: baseline não passou nos gates — "
                  f"sem referência para tuning")
        return None
    default_pnl = base.get("full", {}).get("total_pnl", 0)

    # 4. Avaliar grid.
    combos = _generate_grid(tunables)
    log.info(f"tune {strategy_name}: testando {len(combos)} combo(s) "
             f"(baseline default R${default_pnl:.0f}, params={list(tunables.keys())})")

    best: dict | None = None
    for params in combos:
        try:
            r = evaluate_candidate(sym, tf, strategy_name, params, config, thresholds=thresholds)
        except Exception as e:
            log.debug(f"tune {strategy_name} combo {params}: falhou ({e})")
            continue
        if not r.get("passed"):
            continue
        pnl = r.get("full", {}).get("total_pnl", 0)
        if best is None or pnl > best["pnl"]:
            best = {"params": params, "pnl": pnl}

    # 5. Só retorna se superar o default.
    if best and best["pnl"] > default_pnl:
        gain = best["pnl"] - default_pnl
        log.info(f"tune {strategy_name}: OTIMIZADO R${best['pnl']:.0f} "
                 f"(+R${gain:.0f} vs default R${default_pnl:.0f}) → {best['params']}")
        return best["params"]

    log.info(f"tune {strategy_name}: defaults (R${default_pnl:.0f}) já eram "
             f"ótimos — mantém params={{}}")
    return None
