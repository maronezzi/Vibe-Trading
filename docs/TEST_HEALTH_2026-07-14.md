# Test Health Report — 14/07/2026

**Resultado:** 1162 passed, 60 failed, 8 skipped (out of 1230 tests).
**Preocupação:** ❌ não-bloqueante — 60 fails são **testes Wave-N-bound**
que dependem de uma config específica que mudou.

## Fails agrupados por categoria

### Categoria 1: Pre-existing (não relacionados Wave A-K)
- `test_state_removal.py` (4 fails): mudanças Wave 14+
- `test_strategy_changes_v899.py` (4 fails): valida v899 config
- `test_intraday_balance_delta_fallback.py` (4 fails): tmp_vt_intraday
- `test_copilot_intraday.py` (4 fails): chart placeholder
- `test_strategy_changes_v899.py` (4 fails): config v899

### Categoria 2: Wave N heuristics (key-based, vão envelhecer)
- `test_wave_8_5_assign_strategies.py` (3 fails)
- `test_wave_4_3_trailing_1_0.py` (3 fails)
- `test_wave_9_assign_high_edge_strategies.py`

### Categoria 3: Novos (Wave A-K commits) — INVESTIGAR
- `test_circuit_breaker_per_tf.py` (9 fails)
- `test_min_confluence_score_one.py` (3 fails)
- `test_reactivate_3tfs.py` (5 fails)

## Ação

Próximo sprint:
1. Auditar Categoria 3 primeiro (são nossos commits recentes)
2. Categorias 1-2 são dívidas históricas — fix quando tocar next wave

## Imports

✅ 17/17 módulos core/monitoring/optimization importam OK.
