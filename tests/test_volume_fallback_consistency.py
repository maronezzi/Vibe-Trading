"""
test_volume_fallback_consistency.py
=====================================
TDD: garante que o fallback de volume é CONSISTENTE entre todas as
estratégias. Hoje ENHANCED_MACD_MOMENTUM tem ordem INVERTIDA
(volume antes de tick_volume), enquanto as outras 5 usam
tick_volume primeiro.

PROBLEMA IDENTIFICADO 2026-06-25 (auditoria de código):
  MT5 retorna 'tick_volume' (não 'volume') em barras. Estratégias
  que tentam `b['volume']` direto crasham (já tem fix via .get()).
  Mas a ORDEM do fallback varia:

  ERRADO (enhanced_macd_momentum.py:139-140):
    recent_vol = b.get("volume", b.get("tick_volume", 1))

  CERTO (outras 5 estratégias):
    recent_vol = b.get("tick_volume", b.get("volume", 1))

  No MT5, `volume` é um campo separado e frequentemente retorna 0
  ou None. Se `b["volume"]` retorna 0 (falsy), o fallback funciona
  MAS a média fica errada (0 em vez do volume real). Resultado:
  vol_ratio subdimensionado, filtro de volume passa entradas ruins.

FIX: padronizar todas as estratégias pra `b.get("tick_volume", b.get("volume", 1))`.

Por que importa: ENHANCED_MACD_MOMENTUM é BIT_M5 (única estratégia
do BIT_M5 que perdeu -R$5.395 em 30d). Volume mal calculado pode
ser um dos motivos do loss.
"""
import ast
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = "/home/bruno/Projects/Vibe-Trading"
sys.path.insert(0, PROJECT_ROOT)


def _find_volume_patterns(filepath: str) -> list:
    """Retorna todas as chamadas .get('volume', ...) ou .get('tick_volume', ...) no arquivo."""
    src = Path(filepath).read_text()
    tree = ast.parse(src)
    patterns = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "get":
                # Pega o primeiro argumento (chave) se for string
                if node.args and isinstance(node.args[0], ast.Constant):
                    key = node.args[0].value
                    if key in ("volume", "tick_volume"):
                        # Verifica se é parte de um chain .get('volume', .get('tick_volume', ...))
                        # Procura pelo segundo argumento
                        if len(node.args) >= 2 and isinstance(node.args[1], ast.Call):
                            inner = node.args[1]
                            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                                if inner.func.attr == "get" and inner.args and isinstance(inner.args[0], ast.Constant):
                                    inner_key = inner.args[0].value
                                    if inner_key in ("volume", "tick_volume"):
                                        patterns.append((key, inner_key))
    return patterns


class TestVolumeFallbackConsistency(unittest.TestCase):
    """Garante que todas as estratégias têm fallback tick_volume PRIMEIRO."""

    def test_enhanced_macd_momentum_uses_tick_volume_first(self):
        """ENHANCED_MACD_MOMENTUM deve ter 'tick_volume' antes de 'volume'."""
        path = Path(PROJECT_ROOT, "strategies", "enhanced_macd_momentum.py")
        patterns = _find_volume_patterns(str(path))
        # Cada pattern é (outer_key, inner_key) — deve ser (tick_volume, volume)
        # ou só tick_volume isolado (sem fallback nested)
        for outer, inner in patterns:
            self.assertEqual(
                outer, "tick_volume",
                f"enhanced_macd_momentum.py tem fallback invertido: "
                f".get('{outer}', .get('{inner}')) — deve ser "
                f".get('tick_volume', .get('volume', 1))"
            )

    def test_all_strategies_use_tick_volume_first(self):
        """Auditoria: TODAS as estratégias em strategies/ devem ter ordem consistente."""
        strategies_dir = Path(PROJECT_ROOT, "strategies")
        for py_file in strategies_dir.glob("*.py"):
            if py_file.name == "__init__.py":
                continue
            patterns = _find_volume_patterns(str(py_file))
            for outer, inner in patterns:
                self.assertEqual(
                    outer, "tick_volume",
                    f"{py_file.name} tem fallback invertido: "
                    f".get('{outer}', .get('{inner}'))"
                )


if __name__ == "__main__":
    unittest.main()
