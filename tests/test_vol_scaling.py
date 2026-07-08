"""
test_vol_scaling.py — Wave N+2B (2026-07-08)

Valida core/vt_sizing.py:
  1. resolve_volume respeita hierarquia volume_by_tf > volume_by_symbol >
     volume > 1.0.
  2. Modo "static" (default) ignora current_atr.
  3. Modo "vol_scaled" aplica scale = atr_baseline / current_atr com clamp.
  4. Warmup incompleto (< atr_warmup_bars) → scale=1.0.
  5. Sem baseline setado (0/None) → scale=1.0 (inerte).
  6. Clamps min_scale / max_scale.
  7. resolve_max_daily_trades segue hierarquia by_tf > by_symbol > raiz.
  8. global_max_daily_trades respeita cap raiz.
  9. Defaults seguros quando bloco sizing ausente.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "core")):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.vt_sizing import (  # noqa: E402  (path inj. acima)
    global_max_daily_trades,
    resolve_max_daily_trades,
    resolve_volume,
    get_sizing_for_inspection,
)


# ══════════════════════════════════════════════════════════════════════
# Hierarquia base (modo static)
# ══════════════════════════════════════════════════════════════════════

def test_static_mode_default_returns_base():
    """Sem sizing no config: usa hierarquia volume_by_tf > volume_by_symbol > volume."""
    cfg = {"volume": 1, "volume_by_symbol": {"WDO": 3}, "volume_by_tf": {}}
    # sem TF override → cai pra symbol (WDO=3) → não chega em volume raiz
    assert resolve_volume("WDON26", "M5", config=cfg) == 3


def test_volume_by_tf_overrides():
    """TF-específico ganha sobre symbol e root."""
    cfg = {
        "volume": 1,
        "volume_by_symbol": {"WDO": 2},
        "volume_by_tf": {"WDO_M5": 5},
    }
    assert resolve_volume("WDON26", "M5", config=cfg) == 5
    # TF diferente cai pra symbol-level
    assert resolve_volume("WDON26", "M15", config=cfg) == 2


def test_volume_by_symbol_used_when_no_tf_override():
    cfg = {
        "volume": 1,
        "volume_by_symbol": {"WIN": 3},
        "volume_by_tf": {"WIN_M5": 0.5},  # valor inválido (<1) → ignora
    }
    assert resolve_volume("WINQ26", "M5", config=cfg) == 3


def test_static_mode_ignores_atr():
    """Modo static ignora current_atr — comportamento existente preservado."""
    cfg = {
        "sizing": {"mode": "static"},
        "volume": 2,
    }
    assert resolve_volume("WINQ26", "M5", config=cfg,
                          current_atr=999.0, bars_count=999) == 2


def test_missing_sizing_block_defaults_to_static():
    """Bloco sizing ausente = static (safe-by-default)."""
    cfg = {"volume": 1}
    assert resolve_volume("X", "M5", config=cfg, current_atr=100) == 1


# ══════════════════════════════════════════════════════════════════════
# Modo vol_scaled
# ══════════════════════════════════════════════════════════════════════

def test_vol_scaled_doubles_when_calm():
    """ATR atual metade do baseline → scale 2x (clamped por max_scale)."""
    cfg = {
        "sizing": {
            "mode": "vol_scaled",
            "atr_baseline": 100.0,
            "min_scale": 0.4,
            "max_scale": 1.8,    # clamp em 1.8
        },
        "volume": 1,
    }
    # ATR=50 (metade do baseline) → scale=2.0 → clamp 1.8 → vol=1.8
    vol = resolve_volume("WINQ26", "M5", config=cfg, current_atr=50)
    assert vol == 2.0  # 1 * max(0.4, min(1.8, 2.0)) = 1.8 → arredondado = 2


def test_vol_scaled_halves_when_volatile():
    """ATR atual dobro do baseline → scale 0.5x."""
    cfg = {
        "sizing": {
            "mode": "vol_scaled",
            "atr_baseline": 100.0,
            "min_scale": 0.4,
            "max_scale": 1.8,
        },
        "volume": 10,
    }
    # ATR=200 (dobro) → scale=0.5 → vol=5
    vol = resolve_volume("WINQ26", "M5", config=cfg, current_atr=200)
    assert vol == 5


def test_vol_scaled_min_floor():
    """ATR altíssimo (muito vol) → scale cai no min_scale floor."""
    cfg = {
        "sizing": {
            "mode": "vol_scaled",
            "atr_baseline": 100.0,
            "min_scale": 0.4,
            "max_scale": 1.8,
        },
        "volume": 10,
    }
    # ATR=10000 → raw=0.01 → clamp 0.4 → vol=4
    vol = resolve_volume("WINQ26", "M5", config=cfg, current_atr=10000)
    assert vol == 4


def test_vol_scaled_inert_when_no_baseline():
    """atr_baseline=0 ou ausente → scaling fica inerte (scale=1)."""
    cfg = {
        "sizing": {"mode": "vol_scaled", "atr_baseline": 0.0},
        "volume": 5,
    }
    assert resolve_volume("WINQ26", "M5", config=cfg, current_atr=100) == 5


def test_vol_scaled_inert_when_warmup_incomplete():
    """bars < atr_warmup_bars → scale=1.0 (não acumula cold start)."""
    cfg = {
        "sizing": {
            "mode": "vol_scaled",
            "atr_baseline": 100.0,
            "atr_warmup_bars": 100,
            "min_scale": 0.4,
            "max_scale": 1.8,
        },
        "volume": 1,
    }
    # current_atr=50 (calmo) com só 50 barras → ainda não warming → scale 1
    vol = resolve_volume(
        "WINQ26", "M5", config=cfg,
        current_atr=50, bars_count=50,
    )
    assert vol == 1  # 1 * 1.0 (warmup incompleto)


def test_vol_scaled_inert_when_current_atr_none():
    """current_atr=None → scale=1.0 (callsite precisa passar ATR)."""
    cfg = {
        "sizing": {"mode": "vol_scaled", "atr_baseline": 100.0},
        "volume": 3,
    }
    assert resolve_volume("WINQ26", "M5", config=cfg, current_atr=None) == 3


# ══════════════════════════════════════════════════════════════════════
# resolve_max_daily_trades
# ══════════════════════════════════════════════════════════════════════

def test_max_daily_trades_by_tf_wins():
    cfg = {
        "max_daily_trades": 999,
        "max_daily_trades_by_symbol": {"WDO": 50},
        "max_daily_trades_by_tf": {"WDO_M5": 5},
    }
    assert resolve_max_daily_trades(cfg, "WDO", "M5") == 5


def test_max_daily_trades_by_symbol_when_no_tf():
    cfg = {
        "max_daily_trades": 999,
        "max_daily_trades_by_symbol": {"WDO": 50},
        "max_daily_trades_by_tf": {},
    }
    assert resolve_max_daily_trades(cfg, "WDO", "M5") == 50


def test_max_daily_trades_root_fallback():
    cfg = {"max_daily_trades": 100}
    assert resolve_max_daily_trades(cfg, "XYZ", "M5") == 100


def test_max_daily_trades_default_999_when_missing():
    cfg = {}
    assert resolve_max_daily_trades(cfg, "WDO", "M5") == 999


def test_max_daily_trades_skips_zero_or_negative():
    """Valor 0 ou negativo cai pro próximo nível (não desativa silencioso)."""
    cfg = {
        "max_daily_trades": 999,
        "max_daily_trades_by_symbol": {"WDO": 0},  # zero = pula
    }
    assert resolve_max_daily_trades(cfg, "WDO", "M5") == 999


# ══════════════════════════════════════════════════════════════════════
# global_max_daily_trades
# ══════════════════════════════════════════════════════════════════════

def test_global_max_daily_trades_present():
    assert global_max_daily_trades({"global_max_daily_trades": 42}) == 42


def test_global_max_daily_trades_default_999():
    assert global_max_daily_trades({}) == 999


# ══════════════════════════════════════════════════════════════════════
# Defaults / Sanity
# ══════════════════════════════════════════════════════════════════════

def test_get_sizing_for_inspection_returns_defaults_when_missing():
    cfg = {}
    snap = get_sizing_for_inspection(cfg)
    assert snap["mode"] == "static"
    assert snap["atr_baseline_period"] == 240
    assert snap["min_scale"] == 0.4
    assert snap["max_scale"] == 1.8


def test_get_sizing_for_inspection_handles_garbage():
    """Config corrompido não derruba o sistema."""
    cfg = {"sizing": {"mode": "garbage", "min_scale": "high", "max_scale": None}}
    snap = get_sizing_for_inspection(cfg)
    # mode inválido cai pra "static"
    assert snap["mode"] == "static"
    # floats inválidos caem pra defaults
    assert snap["min_scale"] == 0.4  # default
    assert snap["max_scale"] == 1.8  # default


def test_floor_never_below_one_contract():
    """Mesmo com scaling agressivo (max_scale=5), vol nunca < 1."""
    cfg = {
        "sizing": {
            "mode": "vol_scaled",
            "atr_baseline": 10.0,
            "min_scale": 0.1,
            "max_scale": 5.0,
        },
        "volume": 1,
    }
    # ATR=10000 → raw=0.001 → clamp 0.1 → vol=1*0.1=0.1 → floor 1.0
    vol = resolve_volume("WINQ26", "M5", config=cfg, current_atr=10000)
    assert vol >= 1.0
