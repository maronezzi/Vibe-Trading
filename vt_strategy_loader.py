"""
vt_strategy_loader — Carrega estratégias de trading dinamicamente.

Cada arquivo em strategies/ é um plugin com:
  STRATEGY_NAME = "NOME_DA_ESTRATEGIA"
  def check_entry(symbol, tf, price, atr, bar_ts, bars, params) -> dict | None

Retorno:
  None = sem sinal
  {"direction": "BUY"/"SELL", "sl_pts": int, "info": {...}} = sinal detectado

Uso:
    from vt_strategy_loader import load_strategies, get_strategy_func
    strategies = load_strategies()
    func = get_strategy_func("VWAP")
    result = func(symbol, tf, price, atr, bar_ts, bars, params)
"""

import importlib
import logging
from pathlib import Path

log = logging.getLogger("vt_strategies")

STRATEGIES_DIR = Path(__file__).parent / "strategies"

# Cache: nome → módulo
_strategies: dict = {}
_loaded = False


def load_strategies(force: bool = False) -> dict:
    """Carrega todas as estratégias de strategies/."""
    global _strategies, _loaded

    if _loaded and not force:
        return _strategies

    _strategies.clear()

    if not STRATEGIES_DIR.exists():
        log.warning(f"Diretório strategies/ não encontrado: {STRATEGIES_DIR}")
        return _strategies

    for py_file in sorted(STRATEGIES_DIR.glob("*.py")):
        if py_file.name.startswith("_"):
            continue

        module_name = py_file.stem
        try:
            spec = importlib.util.spec_from_file_location(
                f"strategies.{module_name}", str(py_file)
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            name = getattr(module, "STRATEGY_NAME", module_name.upper())
            check_func = getattr(module, "check_entry", None)

            if check_func is None:
                log.warning(f"Estratégia {module_name} não tem check_entry()")
                continue

            _strategies[name] = {
                "module": module,
                "check_entry": check_func,
                "name": name,
                "file": str(py_file),
            }
            log.info(f"✅ Estratégia carregada: {name} ({py_file.name})")

        except Exception as e:
            log.error(f"Erro ao carregar estratégia {module_name}: {e}")

    _loaded = True
    log.info(f"Total: {len(_strategies)} estratégias carregadas")
    return _strategies


def get_strategy_func(name: str):
    """Retorna a função check_entry da estratégia pelo nome."""
    if not _loaded:
        load_strategies()

    strategy = _strategies.get(name)
    if strategy is None:
        log.error(f"Estratégia '{name}' não encontrada. Disponíveis: {list(_strategies.keys())}")
        return None

    return strategy["check_entry"]


def list_strategies() -> list:
    """Retorna lista de nomes das estratégias disponíveis."""
    if not _loaded:
        load_strategies()
    return list(_strategies.keys())


def reload_strategies() -> dict:
    """Força reload de todas as estratégias (para quando AGI cria nova)."""
    global _loaded
    _loaded = False
    return load_strategies(force=True)
