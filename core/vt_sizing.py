"""
vt_sizing.py — Wave N+2B (2026-07-08)

Decisor único de SIZING (volume em contratos + limites diários).
Extraído de core/vt_autotrader.py para isolar a complexidade das
fórmulas (vol-scaled) dos limites diários — antes estas 3 funções
estavam espalhadas em vt_autotrader.py:1125-1265.

Modos (Wave N+2B):
- "static": volume fixo da hierarquia volume_by_tf > volume_by_symbol > volume.
- "vol_scaled": volume modulado por ATR baseline / ATR atual
  (calmo → aumenta exposição, vol expandindo → encolhe).

Estrutura de sizing no vt_config.json:

    "sizing": {
        "mode": "static" | "vol_scaled",
        "atr_baseline_period": 240,       # minutos (4h); barr para warmup
        "atr_baseline": 120.0,            # pts baseline por (symbol, tf)
        "min_scale": 0.4,                 # limite inferior (não zerar)
        "max_scale": 1.8,                 # limite superior (não dobrar)
        "atr_warmup_bars": 100            # barras necessárias p/ ativar
    }

defaults (Wave N+2B): se bloco ausente, fallback para "static".

AGI pode tunar: atr_baseline, min_scale, max_scale (range [0.4, 4.0]).
AGI NÃO pode mudar "mode" (humano-only).
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("vt_sizing")

DEFAULT_SIZING = {
    "mode": "static",
    "atr_baseline_period": 240,
    "atr_baseline": 0.0,            # 0 = desativa scaling até humano setar
    "min_scale": 0.4,
    "max_scale": 1.8,
    "atr_warmup_bars": 100,
}

_SAFE_SIZING_RANGES: dict[str, tuple[float, float]] = {
    "atr_baseline_period": (60, 1440),       # 1h a 24h
    "min_scale": (0.1, 1.0),
    "max_scale": (1.0, 5.0),
    "atr_warmup_bars": (10, 500),
}


def _coerce_sizing(cfg: dict | None) -> dict:
    """Lê sizing do config, com defaults para chaves ausentes."""
    cfg = cfg if isinstance(cfg, dict) else {}
    out = dict(DEFAULT_SIZING)
    for k, v in cfg.items():
        if k in DEFAULT_SIZING:
            out[k] = v
    # Coerções numéricas defensivas
    for k in ("atr_baseline_period", "atr_warmup_bars"):
        try:
            out[k] = int(out[k])
        except (TypeError, ValueError):
            out[k] = DEFAULT_SIZING[k]
    for k in ("atr_baseline", "min_scale", "max_scale"):
        try:
            out[k] = float(out[k])
        except (TypeError, ValueError):
            out[k] = DEFAULT_SIZING[k]
    out["mode"] = str(out.get("mode", "static"))
    if out["mode"] not in ("static", "vol_scaled"):
        out["mode"] = "static"
    return out


def _symbol_root(symbol: str) -> str:
    """Extrai root (WIN/WDO/BIT/etc.) do symbol resolvido."""
    for r in ("WIN", "WDO", "BIT", "DOL", "IND", "WSP"):
        if r in symbol:
            return r
    return ""


def _base_volume(config: dict, symbol: str, tf: str) -> float:
    """Hierarquia: volume_by_tf > volume_by_symbol > volume > 1.0."""
    root = _symbol_root(symbol)
    tf_key = f"{root}_{tf}"

    vol_by_tf = config.get("volume_by_tf") or {}
    if isinstance(vol_by_tf, dict):
        try:
            v = vol_by_tf.get(tf_key)
            if isinstance(v, (int, float)) and v >= 1.0:
                return float(v)
        except Exception as exc:
            log.warning(f"VOL {tf_key}: erro em volume_by_tf ({exc!r}) — fallback")

    vol_by_sym = config.get("volume_by_symbol") or {}
    if isinstance(vol_by_sym, dict):
        try:
            v = vol_by_sym.get(root)
            if isinstance(v, (int, float)) and v >= 1.0:
                return float(v)
        except Exception:
            pass

    try:
        v = config.get("volume")
        if isinstance(v, (int, float)) and v >= 1.0:
            return float(v)
    except Exception:
        pass

    return 1.0  # safety default


def _vol_scale(
    config: dict,
    symbol: str,
    tf: str,
    current_atr: float | None,
    bars_count: int | None = None,
) -> float:
    """Calcula factor de escala por ATR baseline / ATR atual. 1.0 se desativado.

    Returns:
        float ∈ [min_scale, max_scale] clamped. 1.0 se scaling não ativo.
    """
    s = _coerce_sizing(config.get("sizing"))
    if s["mode"] != "vol_scaled":
        return 1.0
    if current_atr is None or current_atr <= 0:
        return 1.0
    if s["atr_baseline"] <= 0:
        # baseline não setado pelo humano → scaling fica inerte até set.
        return 1.0
    if (
        bars_count is not None
        and bars_count < s["atr_warmup_bars"]
    ):
        return 1.0  # ainda aquecendo

    raw = s["atr_baseline"] / current_atr
    return max(s["min_scale"], min(s["max_scale"], raw))


def resolve_volume(
    symbol: str,
    tf: str,
    *,
    config: dict,
    current_atr: float | None = None,
    bars_count: int | None = None,
) -> float:
    """Volume (qtd contratos) por (symbol, tf) com modo "static" ou "vol_scaled".

    Args:
        symbol: contrato MT5 resolvido (ex: "WDON26").
        tf: timeframe ("M5", "M15", "M30", "H1").
        config: vt_config dict (snapshot frozen por tick). Use load_effective_config().
        current_atr: ATR atual em pts (None desativa vol-scaling).
        bars_count: número de barras fetchadas (gate warmup).

    Returns:
        float >= 1.0. NUNCA retorna 0 — pares a zerar devem usar
        disabled_timeframes (não esta função).
    """
    base = _base_volume(config, symbol, tf)
    scale = _vol_scale(config, symbol, tf, current_atr, bars_count)
    vol = base * scale
    # Convenção de floors: nunca abaixo de 1 contrato.
    return float(max(1.0, round(vol)))


def resolve_max_daily_trades(
    config: dict,
    symbol_root: str,
    tf: str = "",
) -> int:
    """Limite diário de trades por (symbol_root, [tf]).

    Hierarquia: max_daily_trades_by_tf[(symbol,tf)] >
    max_daily_trades_by_symbol[symbol_root] > max_daily_trades raiz.
    """
    by_tf = config.get("max_daily_trades_by_tf") or {}
    if isinstance(by_tf, dict):
        v = by_tf.get(f"{symbol_root}_{tf}") if tf else None
        if v is None and tf:
            # Se TF específico não tem override, tenta symbol-only via tf vazio.
            v = by_tf.get(symbol_root)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)

    by_sym = config.get("max_daily_trades_by_symbol") or {}
    if isinstance(by_sym, dict):
        v = by_sym.get(symbol_root)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)

    root_val = config.get("max_daily_trades")
    if isinstance(root_val, (int, float)) and root_val > 0:
        return int(root_val)
    return 999  # safety default


def global_max_daily_trades(config: dict) -> int:
    """Cap global (config). Independente de symbol."""
    v = config.get("global_max_daily_trades")
    if isinstance(v, (int, float)) and v > 0:
        return int(v)
    return 999


def get_sizing_for_inspection(config: dict) -> dict[str, Any]:
    """Snap do bloco sizing com defaults aplicados — útil pra debug/Telegram."""
    return _coerce_sizing(config.get("sizing"))


__all__ = [
    "DEFAULT_SIZING",
    "resolve_volume",
    "resolve_max_daily_trades",
    "global_max_daily_trades",
    "get_sizing_for_inspection",
]
