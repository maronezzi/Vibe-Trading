"""
AGI v4 — Busca Exaustiva + Web + Geração de Estratégias.

Substitui optimization/agi_tuning_17h.py (mantido intacto como fallback).

Pipeline de 6 estágios (ver pipeline.run):
  1. collect  — PnL real do DB + regime (broker-truth, Lei 4)
  2. intel    — WebSearch (fatos) + LLM (síntese) gera hipóteses
  3. search   — busca exaustiva paralela (CPU pool) sobre strategies × params
  4. generate — gera estratégias .py novas em strategies/_pending/ (sandbox)
  5. apply    — aplica candidatos aprovados com gates (Regra 1 + evidence + safety)
  6. report   — Telegram + audit JSON

Leis de Ouro respeitadas:
  Lei 1 (Zero hardcode): gates leem thresholds de vt_config.json
  Lei 2 (Escopo): AGI nunca desabilita símbolo/TF; se sem edge, gera estratégia
  Lei 3 (SL mandatory): gate AST em estratégias geradas
  Lei 4 (MT5 truth): avaliação por simulação bar-by-bar em 30d de barras
  reais do MT5 + walk-forward. Trades passados do DB NÃO são referência
  (pertenciam à estratégia antiga).
  Lei 5 (Iterar até lucrar): loop de convergência em pipeline.run

Arquitetura: cada stage é um módulo independente com função run(ctx) -> dict.
O pipeline.py orquestra; runner.py é o entrypoint do cron.
"""

__version__ = "4.0.0"
__tag__ = "W871"
