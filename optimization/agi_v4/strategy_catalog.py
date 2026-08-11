"""
strategy_catalog.py — Catálogo de estratégias para alimentar o prompt da LLM.

Construído em runtime lendo strategies/*.py via AST (sem importar os plugins —
evita side-effects e mantém o catálogo sempre sincronizado com o disco). Para
cada estratégia extrai:

  - STRATEGY_NAME (constante exigida pelo contrato do plugin)
  - params lidos via params.get(...) dentro de check_entry (lista de chaves)
  - utils referenciados via utils["..."] dentro de check_entry
  - Descrição curta PT-BR da lógica (heurística a partir do nome + utils)

A função build_catalog_for_llm() retorna uma string compacta pronta para
ser embutida no prompt do stage3 (sugestão de candidatos) e do stage2
(hipóteses). Só inclui estratégias cujos params têm cobertura razoável no
guardrails.py (default-deny) — o catálogo não deve sugerir ajustes que
seriam rejeitados em validate_write_target.

Reuso: optimization.exhaustive_strategy_search._discover_all_strategies já
faz a enumeração de STRATEGY_NAME; aqui adicionamos a camada de params/utils.

Lei 4 (broker-truth): o catálogo é apenas informação para a LLM sugerir. Toda
decisão ainda passa por simulate bar-by-bar no MT5 (backtest_evaluator).
"""
from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger("agi_v4.catalog")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
STRATEGIES_DIR = _PROJECT_ROOT / "strategies"

# Params universais (UNIVERSAL_PARAMS do exhaustive_strategy_search) — toda
# estratégia os recebe via base do símbolo, então a LLM pode ajustá-los para
# qualquer estratégia. Mantemos a lista aqui para incluir no catálogo.
UNIVERSAL_TUNABLE_PARAMS = {
    "sl_atr_mult": (float, 0.5, 5.0),
    "cooldown_seconds": (int, 60, 3600),
    "max_consecutive_losses": (int, 1, 999),
    "halt_duration_minutes": (int, 15, 240),
    "profit_lock_r": (float, 0.0, 1.5),
    # Wave 880.B4 (2026-07-19): TP1/TP2 ladder agora modelado no backtest e live.
    "tp1_r": (float, 0.5, 3.0),
    "tp1_pct": (float, 0.1, 0.9),
    "tp2_r": (float, 1.5, 4.0),
    "tp2_pct": (float, 0.1, 0.9),
    "atr_trail_mult": (float, 0.5, 5.0),  # tighter trail pós-TP1
    # Wave 880.F (07/08): trailing por lucro — AGI deve tunar COMPORTAMENTO,
    # não número fixo. trail_activate = múltiplo de ATR pra ATIVAR; trail_distance
    # = distância do trailing. Range pequeno p/ symbols point<1.0 (WDO/WSP/BIT)
    # onde 1 ATR em preço ~ R$8000+ (precisa de fração pequena de ATR).
    "trail_activate": (float, 0.0005, 5.0),
    "trail_distance": (float, 0.05, 5.0),
}

# Descrições PT-BR curtas das estratégias mais comuns. Para estratégias fora
# desta tabela, geramos descrição genérica a partir do nome. Isto NÃO é
# hardcode de produção (Lei 1) — é metadata descritiva para o prompt da LLM,
# não parâmetro de trading.
_STRATEGY_DESCRIPTIONS = {
    "RSI_REVERSION": "Mean reversion RSI (compra sobrevendido, vende sobrecomprado)",
    "ENHANCED_RSI_REVERSION": "RSI reversion com filtro ADX+Bollinger (só opera com volatilidade)",
    "BOLLINGER": "Reversão à média nas bandas de Bollinger + confirmação RSI",
    "ENHANCED_BOLLINGER": "Bollinger com filtro ADX+EMA (evita markets laterais fracos)",
    "EMA_CROSSOVER": "Cruzamento de EMAs (fast/slow) + filtro ADX/RSI",
    "EMA_PULLBACK": "Tendência EMA + entrada em pullback (recuo)",
    "MACD_MOMENTUM": "Momentum MACD (cruzamento signal) + ADX/RSI",
    "ENHANCED_MACD_MOMENTUM": "MACD com filtro ADX+EMA (momentum confirmado por tendência)",
    "STRONG_TREND": "Trend following forte (ADX alto, segue EMA)",
    "ADX_TREND": "Trend following puro baseado em ADX (força da tendência)",
    "SMART_EMA": "EMA adaptativo por TF (M15 trend-follow, M5 pullback)",
    "VWAP": "Reversão à média VWAP (preço vs volume-weighted average)",
    "VWAP_EXTREME_REVERSION": "Reversão quando preço se afasta extremamente do VWAP",
    "VWAP_VALUE_AREA": "Operação dentro do value area do VWAP",
    "KELTNER_CHANNEL": "Breakout/reversão no canal Keltner (ATR-based)",
    "DONCHIAN_BREAKOUT": "Breakout do canal Donchian (high/low do período)",
    "PIVOT_POINTS": "Reversão em pivot points clássicos + RSI",
    "TRIPLE_EMA": "Sistema de 3 EMAs (fast/mid/slow) com filtro ADX",
    "ICHIMOKU": "Sistema Ichimoku clássico (tenkan/kijun/senkou)",
    "HEIKIN_ASHI": "Tendência via candlesticks Heikin-Ashi + EMA/RSI",
    "CANDLE_PATTERNS": "Padrões de candlestick (engolfo/martelo) + confirmação",
    "FIBONACCI_RETRACEMENT": "Entrada em retrações Fibonacci + RSI",
    "MEAN_REVERSION_ZSCORE": "Mean reversion por Z-score estatístico",
    "DIVERGENCE_RSI": "Divergência preço/RSI (reversão de tendência)",
    "MOMENTUM_BREAKOUT": "Breakout por ROC (rate of change) + RSI",
    "RANGE_TRADING": "Trading em range lateral (suporte/resistência + RSI)",
    "SUPERTREND": "Seguir tendência Supertrend (ATR-based)",
    "WIN_REVERSION": "Mean reversion específico para WIN (índice)",
    "ATR_EXPANSION_BREAKOUT": "Breakout após expansão de volatilidade (ATR alto)",
    "SQUEEZE_BREAKOUT": "Breakout após squeeze de volatilidade (BB contraiu)",
    "OPENING_RANGE_BREAKOUT": "Breakout do range da abertura (ORB clássico)",
    "OPENING_HOUR_EDGE": "Edge na primeira hora de pregão (volume+ADX)",
    "SESSION_MOMENTUM_CLOSE": "Momentum direcional no fechamento da sessão",
    "HTF_BIAS_LTF_ENTRY": "Viés HTF (H1) + entrada LTF (M5/M15)",
    "HTF_EMA_PULLBACK_TIGHT": "Pullback EMA com viés HTF + filtro volume",
    "LIQUIDITY_SWEEP_REVERSAL": "Reversão após varrida de liquidez (stop hunt)",
    "VOLATILITY_BREAKOUT": "Breakout de volatilidade (ATR + RSI)",
    "VOLATILITY_BREAKOUT_TIGHT": "Vol breakout com janela de horário restrita",
    "VOLATILITY_MEAN_REVERSION": "Mean reversion em alta volatilidade",
    "VOLATILITY_REGIME_TREND": "Trend follow em regime de volatilidade",
    "TRAIL_HOLDERS_TREND": "Segue position holders institucionais",
    "IND_INSTITUTIONAL_SELL": "Detecta venda institucional (volume + VWAP)",
}


def _discover_strategies() -> list[dict[str, Any]]:
    """Enumera estratégias via AST (sem importar). Retorna lista de dicts.

    Cada dict: {"name", "params": [...], "utils": [...], "description"}.
    Estratégias em strategies/_pending/ (_-prefixed) são ignoradas — não
    são promovidas ainda.
    """
    found: list[dict[str, Any]] = []
    if not STRATEGIES_DIR.exists():
        return found

    for py in sorted(STRATEGIES_DIR.glob("*.py")):
        if py.name == "__init__.py" or py.name.startswith("_"):
            continue
        try:
            src = py.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except Exception as e:
            log.debug(f"catalog: ignorando {py.name} (parse falhou: {e})")
            continue

        name_match = re.search(
            r'^STRATEGY_NAME\s*=\s*["\'](.+?)["\']', src, re.MULTILINE
        )
        if not name_match:
            continue
        name = name_match.group(1)

        params: set[str] = set()
        utils: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "check_entry":
                body_src = ast.get_source_segment(src, node) or ""
                for m in re.finditer(r'params\.get\(\s*["\']([^"\']+)["\']', body_src):
                    params.add(m.group(1))
                for m in re.finditer(r'utils\[\s*["\']([^"\']+)["\']\s*\]', body_src):
                    utils.add(m.group(1))
                break  # só a primeira check_entry

        description = _STRATEGY_DESCRIPTIONS.get(
            name,
            _generic_description(name, utils),
        )
        found.append({
            "name": name,
            "params": sorted(params),
            "utils": sorted(utils),
            "description": description,
        })

    return found


def _generic_description(name: str, utils: list[str]) -> str:
    """Gera descrição PT-BR genérica a partir do nome + utils usados."""
    parts = []
    if "calculate_rsi" in utils:
        parts.append("RSI")
    if "calculate_ema" in utils:
        parts.append("EMA")
    if "calculate_adx" in utils:
        parts.append("ADX")
    if "calculate_bollinger" in utils:
        parts.append("Bollinger")
    if "calculate_vwap" in utils:
        parts.append("VWAP")
    indicators = " + ".join(parts) if parts else "indicadores técnicos"
    pretty = name.replace("_", " ").title()
    return f"{pretty} (usa {indicators})"


def _is_param_tunable(param_name: str) -> bool:
    """True se o param pode ser ajustado pelo AGI (está no guardrails whitelist).

    Faz a checagem importando validate_write_target e testando um valor
    canônico. Default-deny do guardrails significa: se não casa nenhuma regex,
    o param é read-only para o AGI (ex: params de sessão, ichimoku periods).

    Params universais (sl_atr_mult etc) sempre são tunable.
    """
    if param_name in UNIVERSAL_TUNABLE_PARAMS:
        return True
    try:
        from optimization.agi_v4.guardrails import validate_write_target
    except ImportError:
        # Sem guardrails, assume tunable (fallback conservador)
        return True
    # Testar com alguns valores representativos (int e float)
    testvals = [9, 14, 20, 50, 70, 1.0, 1.5, 2.0, 0.15, 0.01]
    for v in testvals:
        ok, _ = validate_write_target(
            f"params_by_tf.WIN_M5.{param_name}", v, {"disabled_timeframes": []}
        )
        if ok:
            return True
    return False


def build_catalog() -> list[dict[str, Any]]:
    """Retorna o catálogo completo (uma chamada por execução do pipeline).

    Cache em módulo evita reparse do disco se chamado múltiplas vezes na
    mesma run (estratégias não mudam durante uma execução).
    """
    global _CACHED_CATALOG
    if _CACHED_CATALOG is None:
        _CACHED_CATALOG = _discover_strategies()
    return _CACHED_CATALOG


_CACHED_CATALOG: list[dict[str, Any]] | None = None


def build_catalog_for_llm(max_strategies: int = 25) -> str:
    """Constrói string compacta do catálogo para o prompt da LLM.

    Formato (uma linha por estratégia, legível para LLM):
      RSI_REVERSION [rsi_period, rsi_overbought, rsi_oversold, ema_period]:
        Mean reversion RSI (compra sobrevendido, vende sobrecomprado)

    Só params tunable (aceitos pelo guardrails) são listados — a LLM não
    deve sugerir ajustes que seriam rejeitados. Params universais são listados
    uma única vez no cabeçalho (valem para todas).

    Args:
        max_strategies: limite de estratégias no catálogo (top por nº de
            params tunable). Default 25 — prompt da LLM tem budget limitado.
    """
    catalog = build_catalog()

    # Classificar por nº de params tunable (mais ajustáveis primeiro)
    enriched = []
    for strat in catalog:
        tunable = [p for p in strat["params"] if _is_param_tunable(p)]
        enriched.append((len(tunable), strat, tunable))
    enriched.sort(key=lambda x: -x[0])  # mais tunable primeiro

    lines = [
        "# Catálogo de estratégias disponíveis",
        "# Params universais (valem para TODAS as estratégias, ajuste livre):",
        "#   sl_atr_mult [0.5-5.0], cooldown_seconds [60-3600],",
        "#   max_consecutive_losses [1-999], halt_duration_minutes [15-240],",
        "#   profit_lock_r [0.0-1.5]",
        "# Formato: NOME [params ajustáveis]: descrição",
        "",
    ]

    for _, strat, tunable in enriched[:max_strategies]:
        params_str = ", ".join(tunable) if tunable else "(sem params ajustáveis)"
        lines.append(f"{strat['name']} [{params_str}]:")
        lines.append(f"  {strat['description']}")

    return "\n".join(lines)


def get_strategy_names() -> list[str]:
    """Lista simples de nomes de estratégia (para stage3 fallback grid)."""
    return [s["name"] for s in build_catalog()]


__all__ = [
    "build_catalog",
    "build_catalog_for_llm",
    "get_strategy_names",
    "UNIVERSAL_TUNABLE_PARAMS",
]
