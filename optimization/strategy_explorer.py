"""
Wave 13 (Bruno 2026-07-12) — LEGACY DB-BASED API REMOVIDO.

Bruno decidiu que evidência para escolha de estratégias e parâmetros
deve vir de simulação forward sobre DADOS BRUTOS MT5 (via
optimization/vt_forward_backtest.simulate_forward()), nunca de trades
passados em vt_trades.db. Esta decisão bloqueia o anti-padrão de
"otimizar fitando PnL já observado" — que tende a sobreviver com base em
ruído e produzir overfit na próxima janela de mercado.

─── APIs REMOVIDAS (todas levantam NotImplementedError) ──────────────
  load_trades(), compute_stats(), group_trades_by_strategy(),
  compare_strategies_for_pair(), compare_strategies_for_symbol(),
  filter_by_params(), find_best_config(), explore_strategy_variants(),
  generate_strategy_comparison_report(), generate_optimization_report()

  Em qualquer uma: a mensagem aponta para o caminho certo.

─── APIs MANTIDAS ───────────────────────────────────────────────────
  - discover_strategies()  / ALL_STRATEGIES   (auto-discovery)
  - load_config()          (lê vt_config.json)
  - get_current_strategies() / get_all_symbols() / get_timeframes_for_symbol()

─── SUBSTITUTO PARA FORWARD VALIDATION ──────────────────────────────
  from optimization.vt_forward_backtest import (
      fetch_bars_for_backtest, simulate_forward,
      run_mini_backtest_pair_with_strategy, run_all_pairs_parallel,
  )

  Estes consomem barras brutas MT5 (Wine/mt5_fetch.py) e aplicam
  slippage+commission real por contract spec — não há reaproveitamento
  de PnL passado.

  O otimizador canônico é o AGI v4 (optimization/agi_v4/runner.py,
  crontab 12:00 + 17:10), que já usa esse caminho. super_agi_v5.py
  também. agi_tuning_17h.py (legacy v3) só roda manualmente.
"""
import json
import logging
import re
import sys
from pathlib import Path

log = logging.getLogger("strategy_explorer")

# Mantido apenas para load_config() e get_*() helpers.
CONFIG_PATH = Path(__file__).parent.parent / "vt_config.json"


# ── Wave 13 — Dynamic strategy discovery ──────────────────────────────
# Filtra apenas __init__.py; qualquer outro .py é plugin válido.
# Antes (até Wave 12) havia um filtro `name.startswith("_")` que ignorava
# silenciosamente estratégias em rascunho (`_pending/`, `_*_wip.py`) e
# quebrava a ponte AGI→autotrader quando o otimizador promovia uma
# estratégia nomeada em `strategy_by_tf` que o live loader nunca encontrava.
def discover_strategies() -> list[str]:
    """Scan strategies/ directory for all available strategies.
    Reads STRATEGY_NAME from each .py file without importing (fast).
    """
    strategies = []
    strategies_dir = Path(__file__).parent.parent / "strategies"
    for py_file in sorted(strategies_dir.glob("*.py")):
        if py_file.name == "__init__.py":
            continue
        try:
            content = py_file.read_text()
            match = re.search(r'STRATEGY_NAME\s*=\s*["\']([^"\']+)["\']', content)
            if match:
                strategies.append(match.group(1))
        except Exception:
            continue
    return strategies


ALL_STRATEGIES = discover_strategies()
log.info(f"Discovered {len(ALL_STRATEGIES)} strategies: {ALL_STRATEGIES}")


def load_config() -> dict:
    """Carrega vt_config.json."""
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"Erro ao carregar config: {e}")
        return {}


def get_current_strategies() -> dict:
    """Retorna o mapa strategy_by_tf atual do config."""
    config = load_config()
    return config.get("strategy_by_tf", {})


def get_all_symbols() -> list[str]:
    """Retorna lista de símbolos do config."""
    config = load_config()
    return config.get("symbols", ["WIN", "BIT", "WSP", "WDO"])


def get_timeframes_for_symbol(symbol: str) -> list[str]:
    """Retorna timeframes para um símbolo."""
    config = load_config()
    tfs_by_sym = config.get("timeframes_by_symbol", {})
    return tfs_by_sym.get(symbol, config.get("timeframes", ["M5", "M15", "M30", "H1"]))


# ── Wave 13 — Removed-API surface (NotImplementedError com mensagem) ──
#
# Estas funções existiam para validar estratégias comparando PnL histórico.
# Bruno proibiu esse caminho (2026-07-12): evidência vem do forward sim sobre
# dados brutos MT5, nunca do SQLite. Substituto é optimization.vt_forward_backtest.

_REMOVED_HINT = (
    "Use optimization.vt_forward_backtest.simulate_forward() "
    "com dados brutos MT5 (fetch_bars_for_backtest). PnL passado nunca é "
    "evidência forward — apenas sinal exploratório. Ver AGENTS.md seção "
    "'Two Python interpreters joined by Wine'."
)


def load_trades(*args, **kwargs):
    raise NotImplementedError(f"strategy_explorer.load_trades() removed Wave 13. {_REMOVED_HINT}")


def compute_stats(*args, **kwargs):
    raise NotImplementedError(f"strategy_explorer.compute_stats() removed Wave 13. {_REMOVED_HINT}")


def group_trades_by_strategy(*args, **kwargs):
    raise NotImplementedError(f"strategy_explorer.group_trades_by_strategy() removed Wave 13. {_REMOVED_HINT}")


def compare_strategies_for_pair(*args, **kwargs):
    raise NotImplementedError(f"strategy_explorer.compare_strategies_for_pair() removed Wave 13. {_REMOVED_HINT}")


def compare_strategies_for_symbol(*args, **kwargs):
    raise NotImplementedError(f"strategy_explorer.compare_strategies_for_symbol() removed Wave 13. {_REMOVED_HINT}")


def filter_by_params(*args, **kwargs):
    raise NotImplementedError(f"strategy_explorer.filter_by_params() removed Wave 13. {_REMOVED_HINT}")


def find_best_config(*args, **kwargs):
    raise NotImplementedError(f"strategy_explorer.find_best_config() removed Wave 13. {_REMOVED_HINT}")


def explore_strategy_variants(*args, **kwargs):
    raise NotImplementedError(f"strategy_explorer.explore_strategy_variants() removed Wave 13. {_REMOVED_HINT}")


def generate_strategy_comparison_report(*args, **kwargs):
    raise NotImplementedError(f"strategy_explorer.generate_strategy_comparison_report() removed Wave 13. {_REMOVED_HINT}")


def generate_optimization_report(*args, **kwargs):
    raise NotImplementedError(f"strategy_explorer.generate_optimization_report() removed Wave 13. {_REMOVED_HINT}")


# ── Wave 13 — IMPERATIVE_RULE reescrita para apontar ao forward sim ──
IMPERATIVE_RULE = (
    "REGRA IMPERATIVA (Wave 13): Antes de fixar uma estratégia ou parâmetro, "
    "validar via optimization.vt_forward_backtest.simulate_forward() sobre "
    "dados brutos MT5 (fetch_bars_for_backtest). NUNCA usar PnL de trades "
    "passados (vt_trades.db) como evidência forward — apenas como sinal "
    "exploratório. Estratégias precisam passar walk-forward ≥ 75% positivas "
    "para serem promovidas em vt_config.json:strategy_by_tf."
)


if __name__ == "__main__":
    print("=" * 70)
    print("LEGACY: strategy_explorer.__main__ removido em Wave 13.")
    print("Para validação forward real use:")
    print("  /usr/bin/python3 optimization/vt_forward_backtest.py")
    print("  /usr/bin/python3 optimization/agi_v4/runner.py --dry-run")
    print("  /usr/bin/python3 optimization/super_agi_v5.py --dry-run")
    print("=" * 70)
    print(f"Discovered {len(ALL_STRATEGIES)} strategies:")
    for s in ALL_STRATEGIES:
        print(f"  - {s}")
    sys.exit(0)
