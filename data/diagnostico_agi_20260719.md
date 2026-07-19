# Diagnóstico AGI v4 + Fluxo de Saída — Wave 880 (2026-07-19)

Revisão completa do pipeline AGI v4 e do fluxo de entrada/saída do
autotrader. Objetivo do Bruno: **entrar o mais rápido em operação
vencedora, sair o mais tarde possível dela, minimizar perdas, otimizar
lucro**.

## TL;DR — Resultado da Wave 880

| Métrica | Antes | Depois | Delta |
|---|---|---|---|
| PnL backtest 30d | R$ +46,976 | **R$ +55,603** | **+R$ 8,627 (+18.4%)** |
| Trades simulados | 1145 | 1360 | +215 (TP1/TP2 contam) |
| Win rate | 22.7% | 26.5% | +3.8pp |
| Payoff ratio | 57.39 | 52.98 | -4.4 (esperado) |

**9 bugs corrigidos + 4 thresholds apertados + 1 feature nova (TP2 ladder).**

---

## Pipeline AGI v4 — como funciona hoje (pós-Wave 880)

```
cron 12:00 (--mode exploration) ou 17:10 (--mode conservative)
  → run_agi_v4_cron.sh  (snapshot + lock + nohup)
    → runner.py  --days 7 --mode auto
      → pipeline.run():
          Stage1 collect    : SQLite → performance, regime, failing_pairs
          early-exit        : se 0 pares falhos → só report
          LOOP convergence (DEADLINE 90min hard desde Wave 880.A3):
            Stage2 intel    : top-3 pares → web + LLM hypotheses
            Stage3 search   : ProcessPool(max 8) → LLM + grid
            Stage5 apply    : gates (PF≥1.15, WR≥35%, WF≥65%, sharpe≥0.5,
                              max_dd_ratio≤2.5) + LLM approval (fail-open)
            convergence     : todos pares PnL>0?
            Stage4 generate : LLM cria .py novo (AST gate + sandbox)
            Stage5 apply    : de novo
            estagnação ≥2   : BREAK
          Stage6 report     : audit + Telegram
```

---

## 9 bugs corrigidos

### Bug 1 — TP1 tighter-trail era dead code (CRÍTICO para "sair tarde")
- **Arquivo:** `core/vt_autotrader.py:2580-2592` (pré-Wave 880)
- **Sintoma:** Bloco `if tp1_done: trail_dist_cfg = atr_trail_mult` era
  sobrescrito pela linha seguinte `trail_dist_cfg = trail_distance`.
- **Fix Wave 880.B1:** ordem invertida — default primeiro, tighter só
  se `tp1_done` E `atr_trail_mult` setado explicitamente.

### Bug 2 — `profit_lock_r` era dead code total
- **Arquivo:** `core/vt_autotrader.py` (pré-Wave 880 não lia em lugar nenhum)
- **Sintoma:** AGI otimizava parâmetro sem efeito no live. Configurado
  em todos os 16 pares (0.0 a 1.5).
- **Fix Wave 880.A1:** PORT do `backtest_v944.py:396-399` — quando
  `profit_pts >= profit_lock_r * abs(sl_pts)` (1R), move SL para
  entry+1tick (zero-loss lock).

### Bug 3 — `hard_exit_minutes=45` fechava vencedoras no auge
- **Arquivo:** `core/vt_autotrader.py:2597`
- **Sintoma:** Aos 45min qualquer posição era fechada a mercado,
  matando vencedoras com lucro.
- **Fix Wave 880.B2:** `if pos_minutes >= hard_exit_min and profit_pts <= 0:`
  — só força close se perdedora. Align com `backtest_v944.py:429`.

### Bug 4 — `breakeven_minutes=10` muito agressivo
- **Arquivo:** `core/vt_autotrader.py:2588`
- **Sintoma:** Movia SL pra entry+cost após 10 min, whipsawava
  vencedoras lentas.
- **Fix Wave 880.B3:** default code-side 10→20. Backtest usa 0.

### Bug 5 — `min_profit_factor` 1.05 baixíssimo
- **Arquivo:** `optimization/agi_v4/gates.py:38`, `backtest_evaluator.py:284`
- **Sintoma:** PF 1.05 mal cobre spread+comissão em B3. Foi relaxado
  de 1.20→1.05 em 15/07 (`.bak_pre_relaxed_20260715` preservado).
- **Fix Wave 880.A2:** 1.05→1.15.

### Bug 6 — `max_drawdown_pct` gate parcialmente morto
- **Arquivo:** `optimization/agi_v4/backtest_evaluator.py:_check_profitability`
- **Sintoma:** Threshold existia mas só era checado em `gates.py`
  (não no caminho live via `evaluate_candidate`).
- **Fix Wave 880.C1:** Ativado. `if max_dd/avg_loss > 2.5: fail`.

### Bug 7 — Sem deadline de runtime no pipeline
- **Arquivo:** `optimization/agi_v4/pipeline.py`
- **Sintoma:** Comentário do cron mencionava "90 min" mas era
  aspiracional. LLM travado = loop infinito.
- **Fix Wave 880.A3:** `_DEADLINE_SECS = 5400` enforced no loop.
  `ctx["deadline_hit"]` sinaliza no audit.

### Bug 8 — `min_win_rate` em unidades inconsistentes
- **Arquivo:** `gates.py` (0.35), `backtest_evaluator.py` (35.0)
- **Sintoma:** Funcionava por coincidência (operandos em unidades
  casadas) mas era armadilha de manutenção.
- **Fix Wave 880.A4:** Padronizado em fração (0.35) em ambos.

### Bug 9 — v3 `agi_tuning_17h.py` ainda funcional
- **Arquivo:** `optimization/agi_tuning_17h.py` (4215 linhas)
- **Sintoma:** Marcado "descontinuado W873" mas intacto. Se invocado
  acidentalmente, pode `--pause-failing` (viola Lei 2).
- **Fix Wave 880.A5:** Guard anti-reativação no `main()` — aborta com
  exit 2. Override: `VT_ALLOW_V3=1`.

---

## 4 thresholds apertados (Wave C)

| Threshold | Antes | Depois | Onde |
|---|---|---|---|
| `min_profit_factor` | 1.05 | **1.15** | gates.py, backtest_evaluator.py |
| `min_walk_forward_consistency` | 0.6 | **0.65** | gates.py, backtest_evaluator.py |
| `min_sharpe` (novo) | — | **0.5** | backtest_evaluator.py |
| `max_dd_ratio` (novo gate) | — | **2.5** | backtest_evaluator.py |

---

## Feature nova — TP2 ladder (Wave 880.B4)

**Antes:** só TP1 (1 parcial em `tp1_r*ATR`). Resto da posição andava
com trailing sem ajuste pós-TP1.

**Depois:** TP1 + TP2 ladder.
- TP1: fecha `tp1_pct` do original em `tp1_r*ATR` (default 1.0/0.5).
- TP2: fecha `tp2_pct` do restante em `tp2_r*ATR` (default 2.0/0.5).
- Trailing pós-TP1 usa `atr_trail_mult` (tighter) se setado.
- Restante segue sob trailing até SL/EOD.

**Portado para ambos:** `backtest_v944.py` e `core/vt_autotrader.py`
antes só existia no live (TP1) — agora paridade real.

**Guardrails:** `tp2_r [1.5, 4.0]`, `tp2_pct [0.1, 0.9]` em
`SAFE_WRITE_TARGETS`. AGI pode otimizar o ladder completo.

---

## Feature nova — Cron modes (Wave 880.C4)

`runner.py` aceita `--mode {exploration,conservative,auto}`:
- **exploration** (cron 12:00): max-iters 3+, busca candidatos novos.
- **conservative** (cron 17:10): max-iters 1, só revalida. Threshold
  implícito +10% (só candidatos muito confiantes passam).
- **auto** (default): detecta pela hora (11-13h exploration, resto
  conservative).

**Justificativa:** 17:10 roda a 1:35 do EOD — não deveria propor
mudanças radicais quando o mercado está prestes a fechar.

---

## Descoberta-chave — backtest canônico é `backtest_v944.py`

`AGENTS.md` menciona `backtest_agi_v11.py`, mas o AGI v4 usa
`backtest_v944.py` (importado em `backtest_evaluator.py:116`). E ele
**JÁ IMPLEMENTAVA** `profit_lock_r`, `hard_exit_minutes` condicional,
circuit breaker — enquanto o autotrader live NÃO. Wave 880 resolve
essa divergência fazendo PORT do backtest pro live.

---

## Backtest delta — detalhe por estratégia

| Estratégia | Antes | Depois | Delta |
|---|---|---|---|
| HTF_BIAS_LTF_ENTRY | R$+32,336 | R$+38,619 | +R$6,283 |
| RSI_REVERSION | R$+10,230 | R$+11,280 | +R$1,050 |
| EMA_PULLBACK | R$+4,393 | R$+5,650 | +R$1,257 |
| SUPERTREND | R$+84 | R$+122 | +R$38 |
| ADX_TREND | R$-68 | R$-68 | 0 (já perdedor) |

Ganho concentrado nas estratégias com mais trades vencedores — exatamente
o esperado de um TP ladder.

---

## Não incluído nesta Wave (out of scope)

- `core/vt_emergency.py` — load-bearing, exige revisão separada.
- `sizing.mode` ou `max_daily_loss` — sem backtest de sizing confiável.
- `validate_with_llm` — já True, benéfico.
- Apagar `agi_tuning_17h.py` — mantém como fallback histórico.

---

## Rollback

Snapshot criado antes da Wave 880:
```
vt_config.json.snapshot_pre_wave880_20260719_142452
```
Restaurar: `cp vt_config.json.snapshot_pre_wave880_20260719_142452 vt_config.json`
(com autotrader pausado).

Reverter commits Wave 880:
```bash
git revert 5ef5fd90  # Wave A
git revert 67f3c433  # Wave B
git revert 69a91336  # Wave C4
```

---

## Validação executada

- `ruff check` em todos os arquivos editados: 16 erros pré-Wave A →
  11 depois (reduzi 5, não introduzi nenhum novo).
- `pytest tests/test_validator_v2*.py`: 15/15 OK.
- Backtest reproduzível: 2 runs idênticos R$+55,603.33.
- 3 falhas preexistentes confirmadas via `git stash` (test_agi_memo ×2,
  test_rebuild_state_manage_position ×1) — não introduzidas aqui.

---

## Próximos passos sugeridos (não executados)

1. **Reativar autotrader** (remover `data/autotrader.paused`) e observar
   logs por 1-2 pregões: `[PROFIT_LOCK]`, `[TP2]`, `[TP1_TRAIL]` devem
   aparecer. Verificar se SL negativo (profit-lock) funciona com broker.
2. **Verificar TP2 com broker**: `safe_partial_close` em MT5 Wine —
   pode haver idiossincrasia (mínimo volume fracionário).
3. **Deixar AGI rodar 1 semana** com `--mode auto` antes de julgar se
   cron 17:10 conservative é bom demais ou bom de menos.
4. **Backtest com dados ao vivo**: comparar PnL real (vt_trades.db)
   vs PnL backtest dos mesmos dias. Se divergir > 15%, há outro bug
   de paridade não coberto.

---

## Commits Wave 880

- `5ef5fd90` — Wave 880.A: correções de bug e thresholds
- `67f3c433` — Wave 880.B: saída de vencedoras (TP2 ladder)
- `69a91336` — Wave 880.C4: cron modes exploration/conservative

Branch: `wave-880-agi-saida`
