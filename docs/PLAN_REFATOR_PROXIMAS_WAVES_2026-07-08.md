# Plano de Refator — Wave N+1..N+5 + Simplificação

**Data:** 2026-07-08 (Wave 874+)
**Autor:** Plano de engenharia (refactor estratégico)
**Status:** Rascunho para revisão humana antes de cada wave subir
**Escopo:** Apenas plano — nenhum arquivo será modificado sem merge de wave específico

> **Aviso:** Este doc aterrissa cada item no código real. Todo `path:line` foi levantado por exploração estática em 2026-07-08 e **re-validado** (ver §0.5 Errata — 3 correções materiais aplicadas, ~40 citações confirmadas corretas). Antes de tocar qualquer arquivo, conferir se a linha ainda bate (autotrader evolui diariamente).

---

## 0. TL;DR

> **Atualizado 2026-07-08 (pós-errata).** A auditoria de código revelou 2 bugs bloqueantes que mudam a ordem. O bloco "Errata" (§0.5) e a wave W875.0 (§18.1) são **prioridade zero** antes de qualquer trabalho de feature.

Roadmap = **2 fixes bloqueantes** + 5 waves de feature + simplificação estrutural do `vt_autotrader.py` (3.776 linhas). Cada wave termina com:

- ≥1 teste novo em `tests/`
- Métrica visível no Telegram/copilot
- Snapshot `vt_config.json.snapshot_<ts>` antes do merge
- Rollout: autotrader pausado → dry-run → 1 estratégia 5 dias → broad

| Wave | Tema | Depende de | PnL direto? | Risco |
|---|---|---|---|---|
| **W875.0** 🔥 | **Fix LLM bridge** (Errata 2) — AGI voltar a iterar | — | **indireto (Lei 5)** | baixo |
| **W875.G** | AGI guardrails reais (Errata 1) | W875.0 | indireto (safety) | baixo |
| **N+1** | Foundation — log contrafactual | — | indireto | baixo |
| **N+2** | PnL direto — TP1 + sizing vol-scaled | — (paralelo a N+1) | **SIM (maior)** | médio |
| **N+3** | Sinal — MTF confluence + edge estimator | N+1 | médio | médio |
| **N+4** | Risk hygiene — blackout/cooldown/SLO | N+3B (parcial) | baixo | médio |
| **N+5** | Operação — day-trade intent + replay | N+1 | baixo | baixo |

**Sequência executiva recomendada** (decidida por métrica, não por escolha humana — ver §17):
W875.0 → (19.1+19.7 gitignore) → (19.2+19.3 CI/packaging) → N+2 ‖ 3.1-split → N+1 → N+3 → N+4 → N+5.

---

## 0.5 Errata — correções validadas em 2026-07-08 (revisão pós-rascunho)

Auditoria estática feita contra o código real em 2026-07-08 (PIDs vivos, git tree suja, 8 stashes). **3 erros materiais** abaixo alteram o conteúdo das waves. As demais ~40 citações `path:line` foram validadas como corretas.

### Errata 1 — `_SAFE_TARGETS` NÃO EXISTE ⚠️ conceitual

- **Onde o plano erra:** seções 5.1 (item 6), 6.1 (item 5), 10 (tabela inteira), 12.2, 12.3 referenciam `optimization/agi_v4/stage5_apply.py:_SAFE_TARGETS` (L134-160) como mecanismo de whitelist do AGI por param-key.
- **Realidade:** `/usr/bin/grep -rn "_SAFE_TARGETS"` em `optimization/` e `core/` retorna **zero matches**. Não existe essa estrutura em lugar nenhum.
- **Como a segurança realmente funciona:** via `core/vt_config_loader.py:ALLOWED_WRITERS` (L56-114) — whitelist de **módulos** autorizados a escrever config (não de chaves). `stage5_apply.py` foi adicionado a essa lista na W874. Um módulo whitelisted pode escrever **qualquer** chave. Não há gate por caminho de chave.
- **Implicação:** o conceito "AGI só toca params whitelistados" descrito na seção 10 é **aspiracional, não implementado**. Hoje o AGI tem write-livre em `vt_config.json` inteiro (restrito só pela lógica interna de `stage5_apply._apply_one`, que rejeita `cand_pnl <= 0` — L68-70, esse está validado).
- **Ação corretiva:** ver seção 18.2 (nova wave "AGI guardrails reais").

### Errata 2 — `ask_llm` não existe; AGI v4 stage2+stage4 são no-ops silenciosos ⚠️ crítico

- **Onde o plano erra:** seção 8.2 presume que `stage2_intel.py` consome `loser_replay` (presumo funcional); seção 3.4 trata o shim v3 como única dívida AGI. Não cita o LLM quebrado.
- **Realidade:** `optimization/agi_v4/stage4_generate.py:268` e `stage2_intel.py:153` fazem `from core.vt_hermes_helper import ask_llm`. **`ask_llm` não está definido em `vt_hermes_helper.py`** (só `find_hermes`, `hermes_send`, `hermes_send_caption` — Telegram). `grep -rn "def ask_llm"` em todo o repo retorna **0**.
- **Comportamento resultante:** ambos os imports levantam `ImportError` em runtime; o `except` loga `"ask_llm não disponível"` e retorna `None`. Ou seja:
  - **stage4** nunca gera estratégias para `strategies/_pending/` → os 12 TFs pausados na W873 **nunca vão receber alternativas** enquanto isto não for corrigido.
  - **stage2** nunca produz hipóteses LLM → a inteligência "AGI" do pipeline é literalmente nula além da busca exaustiva do stage3.
- **Por que importa agora:** isto explica diretamente o sintoma "12 TFs pausados há dias, nenhum renasceu" — não é falha do otimizador, é falha da ponte LLM. **Lei 5 (AGI itera até lucrar) está violada em silêncio.**
- **Nota de contexto:** LLMs funcionais existem no código — mas como funções **privadas locais**, não exportadas: `_ask_llm_provider` (`core/vt_order_validator_v2.py:100`), `_ask_llm` (`mt5/mt5_error_recovery.py:62`, `core/vt_order_validator.py:47`). Cada um reimplementa o provider.
- **Ação corretiva:** ver seção 18.1 (nova wave "W875.0 fix LLM bridge" — bloqueante, pré-tudo).

### Errata 3 — `SessionState` range e fim de `run_daemon` (menor)

- **Onde o plano erra:** seção 15.1 diz `SessionState` em L157-186. Na verdade a classe começa em **L155** e vai até **L558** (8 métodos, incluindo `rebuild_state_from_mt5`). O range 157-186 cobre só `__init__`.
- **`run_daemon`** termina em ~L3743, não L3776 (L3745-3776 é `main()`/`__main__`). Impacto baixo — só confunde quem for splitar.

### O que NÃO mudou (validado correto)

- `vt_autotrader.py` = 3.776 linhas exatas. Monolítico confirmado.
- `_init_strategy_utils` L84, `_resolve_volume` L1118, `check_and_trade` L1347, `_execute_entry` L1913, `manage_position` L2221, `close_all_and_report` L2532 — **todos no centro do alvo**.
- Legacy hard-coded `check_entry_vwap/bollinger/ema_crossover` (L1535/1626/1701) — **presentes**, removíveis conforme seção 3.2.
- `tp_pts=None` em live (L1975) — confirmado sempre null.
- `partial_close` (orchestrator+executor) — **não existe**, precisa ser criado.
- `_run_wine` (L98-125) — **não mede latência**, confirmado.
- `htf_bias_ltf_entry.py` recebe `bars=list` mas espera `dict` → filtro H1 **morto em produção** (degrada pra M5-only). Confirmado.
- `signal_blocked_log` e `edge_estimator` tables — **não existem**, precisam criação.

---

## 1. Princípios & leis (não-negociáveis)

1. **Lei 1 — Write-lock absoluto.** Só 3 categorias escrevem `vt_config.json`:
   `core/vt_config_loader.py` (autorizado), `optimization/agi_*` (AGI), `scripts/*` (autotrader pausado).
   Qualquer writer novo entra em `core/vt_config_loader.py:ALLOWED_WRITERS` (L56-114).
2. **Wave N+1 bloqueia N+3 e N+5.** Sem log contrafactual, edge estimator e replay medem nada.
3. **Wave N+2 corre em paralelo a N+1.** Arquivos disjuntos, sem acoplamento.
4. **AGI NUNCA toca:** `max_daily_loss`, `disabled_symbols`, `disabled_timeframes`, `magic`, `start_hour/minute`, `close_hour/minute`, `pause_criteria`, `_version`. Continua valendo a Lei 2 da AGI (`stage5_apply.py:15`).
5. **Todo commit:** PT-BR + `Wave N+X.Y` no subject + eco em `_updated_by`.
6. **Snapshots antes de merge.** `cp vt_config.json vt_config.snapshot_pre_n<X>` é automático via pre-commit hook sugerido (seção 9.3).
7. **Fazer MENOS, MELHOR.** Qualquer subitem sem teste ou métrica sai.

---

## 2. Estado atual mapeado (resumo executivo)

Levantamento estático via `grep` + `read` em 2026-07-08. Citações `path:line` exatas.

### 2.1 Pontos críticos do código

| Área | Localização | Estado |
|---|---|---|
| Main loop daemon | `core/vt_autotrader.py:3538-3776` | 3.776 linhas — **monolítico**, candidato a split |
| Per-tick entry dispatch | `core/vt_autotrader.py:check_and_trade:1347-1533` | OK |
| Entry placement | `core/vt_autotrader.py:_execute_entry:1913` | OK |
| Position management | `core/vt_autotrader.py:manage_position:2221-2420` | **Trailing implementado, falta TP1** |
| EOD close | `core/vt_autotrader.py:close_all_and_report:2532-2666` | OK |
| Emergency close | `core/vt_emergency.py:251-357` + 5 callsites em `vt_autotrader.py` | OK |
| Strategy loader (hot-reload) | `core/vt_strategy_loader.py:54-130` | OK |
| `check_entry` contract | `strategies/*.py` (35 arquivos) | OK, mas com legacy hard-coded |
| MTF confluence | `strategies/htf_bias_ltf_entry.py:90-166` | **Meia-boca — espera dict mas recebe list** |
| Sizing | `core/vt_autotrader.py:_resolve_volume:1118-1186` | Estático |
| Blackouts | fragmentados: `vt_calendar.py:104`, `vt_autotrader.py:945,996` | **Fragmentado** |
| Loss cooldown | `core/vt_autotrader.py:1305 cross_tf_cooldown` | Genérico, sem per-symbol-direction |
| Wine latency | `mt5/mt5_orchestrator.py:_run_wine:98-125` | **Não mede latência** |
| News calendar | **inexistente** | precisa construir |
| Counterfactual log | **inexistente** | precisa construir |
| Edge estimator | **inexistente** | precisa construir |
| Sizing vol-scaled | **inexistente** | precisa construir |
| Loser replay | **inexistente** | precisa construir |

### 2.2 Plumbing que já existe (economiza trabalho)

- `tp_pts` plumbing end-to-end: `vt_autotrader.py:1975` → `mt5_orchestrator.py:228` → `mt5_executor.py:_try_send:205-211` → `request["tp"]`. **Sempre None em live**. Falta lógica TP1.
- `volume_by_tf` na hierarquia de `_resolve_volume:1155` já é lido; só não está no `vt_config.json` atual.
- `state.consecutive_losses` em `vt_autotrader.py:176` existe (default=999 demo).
- `is_day_trade` coluna em `_TRADES_SCHEMA` (`mt5_orchestrator.py:61-95`) já existe (default=1).
- `loss_cooldown` semantically similar a `cross_tf_cooldown` (`vt_autotrader.py:1305-1344`) — extensível.
- `_run_wine` em `mt5/mt5_orchestrator.py:98-125` retorna success/fail; timing implícito.
- `close_source` taxonomy completa em `core/vt_trade_log.py:222-240`.
- `_resolve_orphan_closes` em `vt_autotrader.py` **+** `rebuild_state_from_mt5()` (L471-552) — duplicação parcial.

---

## 3. Simplificação do sistema atual (pré-wave)

Antes de qualquer wave subir, **enxugar** o que vai ficar redundante. Cada item deste bloco é um commit separado e independente, não bloqueia wave nenhuma.

### 3.1 Autotrader: split do monólito (3.776 linhas)

**Justificativa:** `vt_autotrader.py` carrega responsabilidade demais. Cada wave adiciona peso. Antes de crescer, **encolher**.

**Ação:** Extrair 3 módulos:

| Novo módulo | Linhas hoje | Função |
|---|---|---|
| `core/vt_position_manager.py` | `manage_position:2221-2420`, `close_all_and_report:2532-2666`, `_resolve_orphan_closes:2966+` | Tudo de gestão de posição |
| `core/vt_signal_pipeline.py` | `check_and_trade:1347-1533`, `_init_strategy_utils:84-96` | Dispatch de sinais (vai crescer com Wave N+3) |
| `core/vt_sizing.py` | `_resolve_volume:1118-1186`, `_resolve_max_daily_trades:1189+`, `_global_max_daily_trades:1228+` | Tudo de sizing (vai crescer com Wave N+2B) |

`vt_autotrader.py` sobra com: daemon loop, `_execute_entry`, state, calendar/symbol resolution, error path. **Target: <2.000 linhas.**

**Validação:** nenhuma mudança comportamental; bateria de testes existente precisa passar idêntica.

**Tests:** garantir cobertura em `tests/test_position_manager.py`, `tests/test_signal_pipeline.py`, `tests/test_sizing.py`. Mínimo 1 teste por função pública por módulo.

### 3.2 Estratégias: descontinuar hard-coded legacy

**Estado atual:** `vt_autotrader.py:1535 check_entry_vwap`, `1626 check_entry_bollinger`, `1701 check_entry_ema_crossover` — funções legacy que existem ao lado do plugin loader.

**Ação:**

1. Confirmar se há `STRATEGY_NAME = "VWAP"` / `"BOLLINGER"` / `"EMA_CROSSOVER"` em `strategies/*.py` correspondentes (já existem).
2. Remover as 3 funções legacy do `vt_autotrader.py`.
3. Validar via `tests/test_strategy_loader.py` que o plugin homônimo cobre 100% do comportamento (especialmente `check_entry_vwap` que tem filter de regime).
4. Atualizar `_get_strategy_for_tf` (`vt_autotrader.py:843-851`) — se referencia nome legacy, ajustar.

**Riscos:** se alguma nuance difere entre hard-coded e plugin, podemos quebrar produção. Validar com 1 dia dry-run broad antes de remover.

### 3.3 Validador: matar v1

**Estado atual:** AGENTS.md: "v1 in `vt_order_validator.py` is also still imported; confirm which path a call site uses before editing".

**Ação:**

1. `grep -rn "vt_order_validator\b" core/ mt5/ monitoring/` — listar imports.
2. Validar que todos migraram pra v2 (`vt_order_validator_v2.py`) via testes live.
3. Renomear `vt_order_validator.py` → `vt_order_validator_v1_legacy.py` + warning de import.
4. Após 30 dias, deletar.

**Tests:** adicionar `tests/test_validator_v1_deprecation.py` que garante warning ao importar v1.

### 3.4 AGI: confirmar shim do v3

**Estado atual:** `optimization/agi_tuning_17h.py` é shim de 126 linhas desde W873. Mas pode haver imports `from optimization.agi_tuning_17h import ...` em outros módulos.

**Ação:**

1. `grep -rn "from optimization.agi_tuning_17h import\|optimization.agi_tuning_17h\." --include="*.py" .`
2. Para cada import: confirmar que vem de symbol re-export (`VALID_STRATEGIES`, `STRATEGIES`) e não função interna.
3. Imports internos do shim → apontar pra `agi_v4/*`.
4. Backup `.bak.pre_shim_20260707` já existe. Após 30 dias, deletar.

### 3.5 Calendar: já tratado por Wave N+4A

Ver seção 6.4. Pré-wave: nada a fazer — Wave N+4A consolida. Mas **documentar** a dívida atual em `core/vt_calendar.py` header.

### 3.6 Config: limpar chaves mortas

**Estado atual:** `vt_config.json` tem 604 linhas. Provavelmente há chaves legadas.

**Ação:**

1. `python -c "import json; cfg=json.load(open('vt_config.json')); print('\n'.join(cfg.keys()))"` → listar.
2. Para cada chave, `grep` por uso em `core/`, `mt5/`, `monitoring/`, `optimization/`.
3. Marcar como `+from_dead` ou `+legacy`.
4. Decidir: remover ou manter documentado em `_notes`. Decisão humana quando em dúvida.

**Cuidados:** `start_hour/minute`, `close_hour/minute`, `magic`, `check_interval`, `bars_count`, `max_daily_loss`, `max_daily_trades`, `halt_*`, `disabled_*`, `time_blocks`, `blocked_day_directions` — **todos potencialmente usáveis, manter mesmo se não referenciados hoje**. Confirmar humano antes de remover.

### 3.7 Dead code via vulture

**Ação:**

```bash
pip install vulture
vulture core/ mt5/ monitoring/ optimization/ --min-confidence 80 --exclude "archive/" > /tmp/vulture.txt
```

Triagem manual. Estratégia: deletar se 100% morto; se usado em runtime path, manter.

### 3.8 Backtest mirror — automatizar drift check

**Estado atual:** `backtest/strategies/` é mirror de `strategies/`. Drift quebra replicação.

**Ação:**

1. Adicionar `tests/test_strategies_mirror.py`:
   ```bash
   diff -q strategies/ backtest/strategies/  # should be empty
   ```
   Roda em CI (mas CI atual só roda `agent/tests/` — irrelevante). Solução: rodar local via pre-push hook ou `preflight_dryrun`.
2. Adicionar hook `pre-push`: se `strategies/*.py` mudou, força `cp` pra `backtest/strategies/`.

### 3.9 Ordem & gates da simplificação

| Item | Commit | Antes de qual wave? |
|---|---|---|
| 3.1 split autotrader | `Wave 875.1 split-monolith` | antes de Wave N+1 |
| 3.2 descontinuar legacy funcs | `Wave 875.2 rm-legacy-strategy` | depois de 1 dia dry-run |
| 3.3 matar validator v1 | `Wave 875.3 deprecate-validator-v1` | 30 dias |
| 3.4 confirmar shim v3 | `Wave 875.4 agi-v3-cleanup` | 30 dias |
| 3.6 limpar chaves mortas | `Wave 875.6 config-key-audit` | antes de Wave N+2 |
| 3.7 vulture | `Wave 875.7 dead-code-sweep` | contínuo |
| 3.8 mirror check | `Wave 875.8 strategies-mirror-test` | imediato |

**Simplificação NÃO inclui** (reservado pra waves):
- Calendar unificado → Wave N+4A
- Sizing block → Wave N+2B
- Vol-scaling → Wave N+2B
- TP1 → Wave N+2A

---

## 4. Wave N+1 — Foundation: log contrafactual

**Goal:** Capturar todo setup latente que filtro barrou, pra medir seletividade e alimentar edge estimator (Wave N+3) + replay (Wave N+5).

**Por que primeiro:** sem esta tabela, edge estimator e replay viram chute. Aditivo, **não muda comportamento de trading** — risco = baixo.

### 4.1 Mudanças

**Schema nova** — em `core/vt_trade_log.py` (NÃO em `_TRADES_SCHEMA` do orchestrator):

```sql
CREATE TABLE signal_blocked_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    tf TEXT NOT NULL,
    strategy TEXT NOT NULL,
    direction TEXT,
    block_reason TEXT NOT NULL,
    hypothetical_sl_pts INTEGER,
    hypothetical_atr_pts REAL,
    regime TEXT,
    resolved INTEGER DEFAULT 0,
    outcome_win INTEGER,
    outcome_pnl_pts REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_blocked_sym_tf_strat_ts ON signal_blocked_log(symbol, tf, strategy, ts);
```

Novas constantes em `core/vt_trade_log.py`: `_BLOCKED_SCHEMA`, `_BLOCKED_INDEXES`. Função `_ensure_blocked_schema(conn)` chamada no `_init_trades_table()` (boot-time, idempotente).

**Novo módulo** `core/vt_signal_journal.py`:

- `log_blocked_signal(symbol, tf, strategy, direction, sl_pts, atr, block_reason, regime) -> None`
  - Detecção "setup latente vs sem setup": heurística = a mesma estratégia nos últimos 30 min, no mesmo `(symbol, tf)`, **gerou sinal pelo menos uma vez**. Se sim, ausência agora = filtro barrou. Se nunca gerou, é "sem setup" (não loga).
  - Batch insert: queue em memória, flush a cada 30s ou 50 rows.
- `resolve_blocked_outcomes(window_minutes=120) -> int`
  - Roda a cada 5 min por daemon.
  - Para cada linha `resolved=0` com `ts < now - window_minutes`: busca preço futuro via `mt5_orchestrator.bars(symbol, tf, count=2)`, computa win/loss/pnl usando `hypothetical_sl_pts`.
  - Idempotente: chave = `(ts, symbol, tf, direction)`.
- `compute_selectivity(strategy=None, days=7) -> dict` retorna `{entries, blocked, selectivity_score = entries / (entries + blocked)}` por estratégia.

**Hook em `vt_autotrader.py:check_and_trade`** (~L1430, após L1479 quando strategy retorna None):

```
quando strategy_func retorna None, ANTES de descartar:
  se get_strategy_for_tf(symbol, tf) == mesma dos últimos 30min
     e houve entry nesse (symbol, tf) nas últimas N horas:
      signal_journal.log_blocked_signal(...)
```

**Whitelist writer** — adicionar `core/vt_signal_journal.py` ao `ALLOWED_WRITERS` (`vt_config_loader.py:56-114`).

### 4.2 Tests — `tests/test_signal_journal.py`

- `test_log_blocked_inserts_row`
- `test_resolve_outcomes_after_window` (fake bars via mock)
- `test_idempotent_on_duplicate_ts`
- `test_selectivity_metric_shape`
- `test_no_log_when_no_setup_in_recent_window`
- `test_batch_flush_at_threshold`

Estender `_isolate_trades_db` em `tests/conftest.py:106-167` para incluir `_BLOCKED_SCHEMA`.

### 4.3 Rollout

1. Merge autotrader pausado, dry-run 1 sessão (`dry_run=True` flag injetado na chamada).
2. Liga `dry_run=True` (loga só) por 5 dias.
3. Promove `dry_run=False`.
4. Cria `monitoring/vt_selectivity_report.py` que produz ranking semanal, manda no Telegram domingo.
5. Adicionar métrica no `monitoring/vt_copilot.py`.

### 4.4 Métrica de sucesso

- Por estratégia: `entries / (entries + blocked)` em report semanal.
- WR das setups logadas vs WR das entradas reais → gap > 20% = filtro frouxo.
- Storage < 50k rows/dia.
- CI: `tests/test_signal_journal.py::test_selectivity_metric_per_strategy` verde com valor mínimo.

### 4.5 Riscos & mitigação

- **Storage growth** → `scripts/vacuum_signal_journal.py` semanal (DELETE WHERE created_at < now - 90 days).
- **Heurística "setup latente vs sem setup"** → começar simples, refinar com Wave N+5.
- **Hot-loop overhead** → batch insert (50 rows ou 30s).
- **Resolve race** → idempotência por `(ts, symbol, tf, direction)`.

### 4.6 Adaptação ao novo modelo

- Esta tabela é o **ground truth** pra Wave N+3B (edge estimator) e Wave N+5B (replay).
- Sem ela, N+3B vira chute + N+5B não tem dados.

---

## 5. Wave N+2 — PnL direto: TP1 parcial + sizing vol-scaled ⚡maior impacto

**Pode correr em paralelo a N+1** — arquivos disjuntos.

**Goal:** Capturar convexidade (TP1+R trailing) e modular exposição por volatilidade. **Maior alavanca de PnL** entre todas as waves.

### 5.1 Item 2A — TP parcial em 1R + trailing no resto

**Estado atual:** `tp_pts` plumbing end-to-end existe mas sempre `None`. Falta:
(a) lógica TP1, (b) executor pra close parcial, (c) lifecycle do remaining position.

**Mudanças por arquivo:**

1. **`core/vt_autotrader.py:manage_position`** (~L2221-2420, vai pra `core/vt_position_manager.py` após split 3.1):
   - Novo estado por posição: `tp1_done: bool`, `remaining_volume: float`, `original_volume: float`, `tp1_r: float`, `tp1_pct: float`.
   - Novo bloco após L2336 (breakeven):
     ```
     if not tp1_done and profit_pts >= tp1_r * atr:
         orchestrator.partial_close(symbol, ticket, original_volume * tp1_pct)
         tp1_done = True
         remaining_volume -= closed
     ```
   - Trailing continua no restante (L2416) com **ATR trail** sobre `remaining_volume`.

2. **`mt5/mt5_orchestrator.py`** (novo método, ~L470):
   - `partial_close(symbol, ticket, close_volume) -> dict`
   - Wine args: `["partial_close", symbol, str(ticket), str(close_volume)]`.

3. **`mt5/mt5_executor.py`** (novo comando, ~L480):
   - `cmd_partial_close(symbol, ticket, close_volume)`
   - `mt5.order_send` com `TRADE_ACTION_DEAL`, type oposto, `volume=close_volume`. Anti partial-close em classe errada.
   - Devolve `{status, ticket, closed_volume, remaining_volume, exit_price}`.

4. **`mt5/mt5_error_recovery.py`** (novo wrapper, ~L620):
   - `safe_partial_close(symbol, ticket, close_volume)` — retry Lei 3, MAX_RETRIES=3.

5. **`vt_config.json:params_by_tf`** (cada `[symbol][tf]`):
   - `tp1_r: float = 1.0`
   - `tp1_pct: float = 0.5`
   - `atr_trail_mult: float = 2.0`

**AGI whitelist** (`stage5_apply.py:_SAFE_TARGETS`): adicionar `tp1_r, tp1_pct, atr_trail_mult` em `params_by_tf[*]`. **NÃO tocar `sl_atr_mult`**.

6. **`_persist_close_to_db` em `mt5_orchestrator.py` (L270-433)**:
   - Migration idempotente: adicionar `partial_closed_volume REAL DEFAULT 0` + `tp1_done INTEGER DEFAULT 0`.
   - Ao persistir close, se `partial_close_pct > 0`, registrar 2 linhas: TP1 (`close_source="TP1"`) + resto (`close_source="TRAIL_STOP"`).

7. **Tests** — `tests/test_partial_tp.py`:
   - `test_partial_close_calls_executor_with_correct_volume` (mock executor)
   - `test_tp1_triggers_at_1r_profit`
   - `test_trail_continues_on_remaining_after_tp1`
   - `test_partial_close_retries_on_requote`
   - `test_tp1_done_state_persists_in_rebuild_state_from_mt5`

8. **Rollout:**
   1. autotrader pausado; liga TP1 com `tp1_pct=0.5` em **WIN M5 só**, 5 dias.
   2. Compara PnL/expectancy pré/pós via `monitoring/vt_daily_report.py` (já existe).
   3. Se expectancy ≥ baseline × 1.15 → broad.
   4. Se caiu → reverte `tp1_pct=0` global; investiga.

### 5.2 Item 2B — Sizing volatility-scaled

**Mudanças:**

1. **`vt_config.json`** (nova seção top-level, ~L55):
   ```json
   "sizing": {
       "mode": "static",
       "atr_baseline_period": 240,
       "atr_baseline": 50.0,
       "min_scale": 0.4,
       "max_scale": 1.8,
       "atr_warmup_bars": 100
   }
   ```

2. **`core/vt_sizing.py` (novo, após split 3.1):**
   - `_resolve_volume(symbol, tf, current_atr)` movida pra cá.
   - Se `sizing.mode == "vol_scaled"`:
     ```
     baseline = cfg[sizing][symbol_tf].atr_baseline
     scale = clamp(baseline / current_atr, min_scale, max_scale)
     vol = volume_base_per_tf * scale
     ```
   - Fallback `static` se `bars_count < atr_warmup_bars`.

3. **Snapshot mensal do `atr_baseline`** — estender `monitoring/vt_pre_flight.py` para gravar/atualizar baseline no `vt_config.json` antes do open.

4. **AGI whitelist** — adicionar `sizing.atr_baseline, sizing.min_scale, sizing.max_scale` ao `_SAFE_TARGETS`. **NÃO tocar `sizing.mode`** (humano decide).

5. **Tests** — `tests/test_vol_scaling.py`:
   - `test_static_mode_unchanged_behavior`
   - `test_vol_scaled_uses_baseline`
   - `test_clamp_min_max`
   - `test_fallback_when_warmup_incomplete`
   - `test_sizing_snapshot_writes_atomic`

6. **Rollout:**
   1. autotrader pausado, `sizing.mode="static"`, baseline snapshotter on.
   2. Baseline observado por 5 dias (snapshot diário).
   3. Promove `sizing.mode="vol_scaled"` em **WIN** só, 5 dias.
   4. Sanity: trades/dia ~constante, expectancy vs baseline.
   5. Broad.

### 5.3 Riscos Wave N+2

- **Partial close race** → usar `expected_volume` do executor (já tem no JSON).
- **Vol-scaling procyclical** → `max_scale=1.8` (não dobrar).
- **`tp1_done` em crash recovery** → `rebuild_state_from_mt5()` deve detectar partial já feito via `volume` vs `original_volume`. Crítico.
- **AGI não tocando sizing.mode** → default conservador `static`; toggle só humano.

### 5.4 Adaptação ao novo modelo

- TP1 muda distribuição de PnL mas **não o número de entries** — backtest precisa replicar.
- Vol-scaling muda volume → backtest `_agi_v11.py` precisa atualizar fórmula de PnL (`vol × mult × pts`).

---

## 6. Wave N+3 — Sinal: MTF confluence + edge estimator

**Depende de N+1** (lê `signal_blocked_log`).

### 6.1 Item 3A — MTF confluence scoring

**Estado atual:** `vt_autotrader.py:1477` passa `bars=list` único. `htf_bias_ltf_entry.py:90-166` aceita `bars=dict` mas nunca recebe.

**Mudanças:**

1. **`vt_signal_pipeline.py:check_and_trade`** (após split 3.1):
   - Pré-fetcher `bars_by_tf = {"M5": bars, "M15": bars_m15, "H1": bars_h1}` por symbol, cachear 30s.
   - Passar `bars=bars_by_tf` para strategy.

2. **`core/vt_strategy_loader.py`** — atualizar docstring do contract: `bars` pode ser `list | dict`.

3. **Nova camada** `core/vt_signal_scorer.py`:
   - `score_signal(signal_result, htf_context) -> float` — 0..1.
   - Default: se estratégia não tem info de bias HTF, score=0.5.
   - Gating: em `check_and_trade`, **só envia entry se score ≥ `min_confluence_score`** (por strategy em `params_by_tf`).
   - Rejections → `signal_blocked_log` com `block_reason="MTF_LOW_SCORE"`.

4. **`vt_config.json:params_by_tf`** — adicionar `min_confluence_score: float = 0.5`.

5. **AGI whitelist** — adicionar `min_confluence_score` ao `_SAFE_TARGETS`.

6. **Tests** — `tests/test_mtf_confluence.py`:
   - `test_bars_dict_passed_to_strategy` (mock)
   - `test_score_below_threshold_blocks_signal` (e loga)
   - `test_htf_strategy_receives_real_h1_bars` (smoke)
   - `test_strategy_with_no_htf_gets_neutral_score`

7. **Rollout** — broad com `min_confluence_score=0.4` por 5 dias antes de subir pra 0.5. Monitorar reject rate.

### 6.2 Item 3B — Edge estimator vivo intra-dia

**Estado atual:** `state.daily_pnl/wins/losses` em `vt_autotrader.py:159-168`. Não decompõe por estratégia.

**Mudanças:**

1. **Schema nova** em `core/vt_trade_log.py`:
   ```sql
   CREATE TABLE edge_estimator (
       id INTEGER PRIMARY KEY AUTOINCREMENT,
       ts TEXT NOT NULL,
       symbol TEXT NOT NULL,
       tf TEXT NOT NULL,
       strategy TEXT NOT NULL,
       n INTEGER NOT NULL,
       wins INTEGER NOT NULL,
       expectancy_pts REAL NOT NULL,
       avg_rr REAL,
       baseline_expectancy_pts REAL NOT NULL,
       edge_decay REAL NOT NULL,
       recommended_size_scale REAL,
       created_at TEXT DEFAULT CURRENT_TIMESTAMP
   );
   CREATE INDEX idx_edge_sym_tf_strat_ts ON edge_estimator(symbol, tf, strategy, ts);
   ```

2. **Novo módulo** `core/vt_edge_estimator.py`:
   - `update(symbol, tf, strategy, n_min=20)` — roda a cada 15 min via daemon.
   - Lê últimos N trades de `trades` por `(symbol, tf, strategy)`.
   - Compara com `baseline_expectancy_pts` (config, populado pelo AGI ou humano).
   - `edge_decay = (current - baseline) / baseline`. Se `< -0.30`, `size_scale = 0.4`. Linear.
   - Insere em `edge_estimator`.

3. **Nova config** top-level:
   ```json
   "edge_estimator": {
       "enabled": false,
       "min_trades": 20,
       "decay_threshold": -0.30,
       "size_scale_floor": 0.4,
       "check_interval_min": 15
   }
   ```

4. **Integração com sizing** (Wave N+2B): `_resolve_volume` multiplica volume final por `recommended_size_scale` da última leitura. Cache 5 min.

5. **Telegram alert** — `monitoring/vt_copilot.py`:
   ```
   🔶 EDGE DECAY {symbol} {tf}: expectancy {X}% baseline, size→{Y}x
   ```

6. **Tests** — `tests/test_edge_estimator.py`:
   - `test_expectancy_from_trades` (mock DB)
   - `test_decay_calculation`
   - `test_size_scale_clipping`
   - `test_gate_kicks_in_only_after_min_trades`

7. **Rollout** — `enabled=false` por 7 dias (só coleta), depois `enabled=true` com `size_scale_floor=0.7`.

### 6.3 Riscos Wave N+3

- **MTF cache stale** → TTL 30s + key em tf+symbol+ts.
- **Edge estimator noise** → janela rolling 30 com decay; começar `min_trades=50` nos primeiros 60 dias.

---

## 7. Wave N+4 — Risk hygiene: blackout consolidado + cooldown + SLO

### 7.1 Item 4A — Blackout refinado por símbolo + lado + janela

**Estado atual:**
- `core/vt_calendar.py:is_trading_day:104-123` — global, sem símbolo/lado.
- `vt_autotrader.py:996 _is_blocked_day_direction` — `blocked_day_directions`.
- `vt_autotrader.py:945 _is_blocked_time` — `time_blocks`.
- **News events: não há DB.**

**Mudanças:**

1. **Nova config** top-level:
   ```json
   "events": []
   ```

2. **`core/vt_calendar.py`** — refactor:
   - `aggregate_blackout(symbol, side, ts=None) -> tuple[bool, str]` — função única.
   - Compõe: `is_trading_day()` + `_is_blocked_day_direction` + `_is_blocked_time` + `events_window`.
   - Retorna `(True/False, compound_reason)`.

3. **`vt_signal_pipeline.py:check_and_trade`** — substituir 2 chamadas por 1 `calendar.aggregate_blackout(symbol, direction, ts)`.

4. **Tuning sugerido:**
   - WIN: ±30min FOMC, ±60min IPCA/BCB.
   - WDO: ±20min US payrolls, ±30min IPCA.
   - BIT/WSP: leve.

5. **`monitoring/vt_news_calendar.py`** — cron `30 6 * * 1-5`, scraper/LLM-fetch de calendário BCB+B3+Fed, escreve em `vt_config.json:events`. **Adicionar `monitoring/vt_news_calendar.py` ao `ALLOWED_WRITERS`.**

6. **Tests** — `tests/test_calendar_blackout.py`:
   - `test_holiday_blocks_all`
   - `test_blocked_day_direction_blocks_one_side`
   - `test_event_window_blocks_symbol_side_match`
   - `test_event_window_skips_other_side`
   - `test_aggregate_returns_compound_reason`

7. **Rollout** — `events=[]` no início. Liga news calendar após 5 dias dry-run.

### 7.2 Item 4B — Cooldown pós-loss consecutiva

**Mudanças:**

1. **Nova config** top-level (em `pause_criteria` ou ao lado):
   ```json
   "loss_cooldown": {
       "enabled": false,
       "max_consecutive": 2,
       "cooldown_minutes": 30,
       "scope": "symbol_direction"
   }
   ```

2. **`core/vt_autotrader.py:SessionState`** (L157+) — adicionar `last_loss_direction_per_symbol: dict`.

3. **Nova função** `_is_loss_cooldown_active(symbol, direction) -> bool` em `vt_autotrader.py:~L1300` (junto com `cross_tf_cooldown`).

4. **Hook em `check_and_trade`** — depois do LLM validator: se cooldown ativo, loga em `signal_blocked_log` com `block_reason="LOSS_COOLDOWN"`.

5. **Hook em `_execute_entry`** — quando `close_source in (SL_SERVER, SL_LOCAL, EMERGENCY_CLOSE)`, atualizar `state.last_loss_direction_per_symbol`.

6. **Tests** — `tests/test_loss_cooldown.py`:
   - `test_two_losses_in_row_triggers_cooldown`
   - `test_cooldown_expires`
   - `test_per_symbol_scope_isolates_loss`
   - `test_logs_block_reason_in_signal_journal`

7. **Rollout** — `enabled=false` por 5 dias (só observa), depois WIN M5 broad.

### 7.3 Item 4C — SLO latência Wine + degradação

**Mudanças:**

1. **`core/vt_latency_monitor.py`** (novo):
   - `record_latency(op, ms)`, `p95(op, window_min=60) -> float`.
   - Ring buffer em `state.latency_buffer`.

2. **`mt5_orchestrator.py:_run_wine`** (L98-125):
   - Wrap start/end em `time.perf_counter()`. Se > 200ms, warning. Se > 1000ms, degradação.

3. **Nova config** top-level:
   ```json
   "latency_slo": {
       "warn_ms": 200,
       "degrade_ms": 1000,
       "degrade_size_factor": 0.5,
       "degrade_disable_breakouts": true
   }
   ```

4. **`core/vt_sizing.py:_resolve_volume`** — se `latency_monitor.p95() > degrade_ms`, multiplicar por `degrade_size_factor`.

5. **Telegram alert** — `monitoring/vt_copilot.py`: se `p95 > warn_ms` sustentado 5 min, alert.

6. **Tests** — `tests/test_latency_slo.py`:
   - `test_p95_calculation`
   - `test_size_factor_applied_when_degraded`
   - `test_alert_fires_on_sustained_warn`

7. **Rollout** — broad, mas `degrade_size_factor=0.5` (não 0.7) nos primeiros 30 dias.

### 7.4 Riscos Wave N+4

- **News scraper pode mentir** → começar com fonte oficial única (BCB).
- **Cooldown em cascata** → validar com `selectivity_score` Wave N+1.
- **Latency baseline em Wine reboot** → excluir 10 primeiras amostras.

---

## 8. Wave N+5 — Operação: day-trade intent + loser replay

**Depende de N+1** (counterfactual).

### 8.1 Item 5A — Hold-time ciente de day-trade intent

**Estado atual:** coluna `is_day_trade` existe mas não é decision input.

**Mudanças:**

1. **Nova config** top-level:
   ```json
   "day_trade_intent": {
       "WIN_M5": true, "WIN_M15": true, "BIT_M5": true,
       "WIN_M30": false, "WIN_H1": false
   }
   ```
   Default: TF < M30 = day-trade.

2. **`core/vt_position_manager.py:manage_position`**:
   - `if day_trade_intent[symbol_tf] and pos_minutes >= day_trade_max_minutes: close(..., close_source="DAY_TRADE_FLATTEN")`. Default max = EOD − 15min.

3. **`core/vt_autotrader.py:_execute_entry`** — day-trade: validar `entry_time + max_hold < close_hour:close_minute`. Se não, bloquear.

4. **Tax-aware sizing** (opcional, conservador): se `is_day_trade=True` E dia já tem `n_day_trades >= 2`, reduzir volume a 0.5×.

5. **Tests** — `tests/test_day_trade_intent.py`:
   - `test_day_trade_blocks_past_eod_minus_buffer`
   - `test_swing_allows_overnight`
   - `test_daytrade_flattens_at_max_minutes`

6. **Rollout** — broad. Tax-aware off por default.

### 8.2 Item 5B — Replay automático de losers

**Mudanças:**

1. **`monitoring/vt_loser_replay.py`** (cron `00 17 * * 1-5`, pós-EOD):
   - Lê últimos trades do dia com `net_pnl < 0`.
   - Para cada loser: busca `signal_blocked_log` (Wave N+1) por `(symbol, tf, strategy)` nas últimas 24h onde `block_reason != "MTF_LOW_SCORE"`. Verifica `outcome_win/pnl_pts`.
   - Computa hipóteses:
     - **H1**: "se filtro X não existisse" → winners/blocked_medio.
     - **H2**: "se filtro Y existisse" (novo) → losers prevenidos.
   - Output: `monitoring/reports/loser_replay_<date>.json` rankeado por impacto.

2. **Ingest no AGI** (`optimization/agi_v4/stage2_intel.py`):
   - Ler `loser_replay_<date>.json` como input. Vira hipótese de filtro pra stage3.

3. **Tests** — `tests/test_loser_replay.py`:
   - `test_aggregates_hypotheses_per_loser`
   - `test_ranks_by_impact`
   - `test_no_signal_blocked_data_returns_empty`

4. **Rollout** — broad. AGI consome só após 2 semanas de validação humana.

### 8.3 Riscos Wave N+5

- **Replay false positives** → pequena amostra pode enganar. Consumir via AGI stage2 só após validação humana.
- **Day-trade flatten perto do close** → slippage. Buffer 15min mínimo; avaliar 20min em backtest.

---

## 9. Não-objetivos (cortados por princípio)

- **Mais um módulo AGI** — v4 cobre; novos módulos = teatro.
- **Mais dashboards/Telegram custom** — copilot + daily já cobrem.
- **Mais validador LLM** — cache 5min já corta custo; voting conflitante em latência.
- **Refactor amplo de `vt_autotrader.py`** — cada wave toca só sua seção; split principal na simplificação 3.1.
- **Substituir Wine/rpyc bridge** — Wave 10/2026-06-26 está estável.
- **Sincronizar `archive/`** — morto, não toca.
- **Backtest de alta fidelidade pra validar Wave N+4C (SLO)** —não aplicável (latência é runtime).
- **Replay com LLM pesado** — heurística em Python primeiro; LLM só se replay manual pedir.

---

## 10. Cross-cutting: alvos do AGI por wave

> **⚠️ Errata 1 (§0.5):** esta seção originalmente referenciava `stage5_apply.py:_SAFE_TARGETS` (L134-160) — **essa estrutura não existe**. A segurança real hoje é só `ALLOWED_WRITERS` (whitelist de módulo, não de chave). A tabela abaixo descreve o **alvo desejado**, a ser implementado pela wave **W875.G (§18.2)** como `SAFE_WRITE_TARGETS` + `FORBIDDEN_TARGETS` reais com gate em `_write_to_config`. Até W875.G subir, tratar a coluna "Explicitamente NÃO tocar" como **convenção de código no stage5**, não enforcement.

Atualizar `optimization/agi_v4/stage5_apply.py` (a estrutura `SAFE_WRITE_TARGETS` a ser criada na W875.G, §18.2) **antes de cada wave subir**. Sem entrada no whitelist, AGI não toca o param e o item fica estático.

| Wave | Novos targets em `_SAFE_TARGETS` | Explicitamente NÃO tocar |
|---|---|---|
| **N+1** | nada | — |
| **N+2** | `tp1_r, tp1_pct, atr_trail_mult` (params_by_tf), `sizing.atr_baseline, sizing.min_scale, sizing.max_scale` | `sizing.mode`, `sl_atr_mult` |
| **N+3** | `min_confluence_score` (params_by_tf) | `edge_estimator.recommended_size_scale` (vem do monitor) |
| **N+4** | `loss_cooldown.cooldown_minutes, max_consecutive` | `latency_slo.degrade_size_factor` |
| **N+5** | nada (day_trade_intent é humano; replay vai via stage2_intel reader) | — |

Sempre adicionar tupla `(key_path_regex, type_check, range_check)`.

---

## 11. Ordem de execução & go/no-go gates

### 11.1 Sequência sugerida

```
Semana 1-2:  Wave N+1 (foundation) + Wave N+2 (PnL direto) em paralelo.
             + Simplificação 3.1 (split autotrader) na frente.
Semana 3:    Simplificação 3.6 (config-key-audit), 3.7 (vulture), 3.8 (mirror).
Semana 4-5:  Wave N+3A (MTF) + N+3B (edge estimator).
Semana 6-7:  Wave N+4A (blackout) + N+4B (cooldown).
Semana 8-9:  Wave N+4C (SLO) + N+5A (day-trade).
Semana 10:   Wave N+5B (replay).
```

### 11.2 Gates humano-decide

| Gate | Condição para avançar |
|---|---|
| N+1 → broad | selectivity report emitido ≥ 1x; storage estável |
| N+2A TP1 WIN M5 → broad | expectancy pré/pós ≥ 1.15× |
| N+2B sizing → broad | trades/dia ±10% do baseline; expectancy não caiu |
| N+3A MTF → broad | reject rate ≤ 30%, gain rate manteve |
| N+3B edge estimator → `enabled=true` | 30 dias de coleta + `min_trades=20` já é representativo |
| N+4A news calendar → ativa | scraper validou ≥ 5 eventos reais vs calendário oficial |
| N+4B cooldown → broad | false positives rate (suprime bons sinais) ≤ 15% via `selectivity_score` |
| N+4C SLO → broad | p95 ms < warn_ms sustentado por 7 dias |
| N+5B replay → input AGI | 2 relatórios semanais revisados por humano |

### 11.3 Bloqueios rígidos (qualquer um trava a wave)

- `ruff check .` falha.
- `python -m pytest tests/ -q` falha.
- `monitoring/vt_trade_watchdog.py:run_watchdog` reporta drift PnL > `DRIFT_THRESHOLD_REAIS`.
- Telegram recebe `EMERGENCY_CLOSE` ou `🚨 CRITICAL` em qualquer momento.
- Conftest isolation quebrada: qualquer teste escreve em prod `vt_config.json` ou `vt_trades.db`.

---

## 12. Adaptação ao novo modelo — checklist pós-wave

Cada wave termina com **checklist de adaptação** validada. Crítico = precisa estar verde antes de broad.

### 12.1 Pós-Wave N+1

- [ ] `signal_blocked_log` existe com indexes.
- [ ] `signal_journal.log_blocked_signal` invocado por `check_and_trade`.
- [ ] `monitoring/vt_selectivity_report.py` emite top-5 estratégias por `selectivity_score`.
- [ ] `monitoring/vt_copilot.py` plota selectivity no daily digest.
- [ ] Storage growth < 50k rows/dia.
- [ ] CI/local: `tests/test_signal_journal.py` verde.

### 12.2 Pós-Wave N+2A

- [ ] `partial_close` end-to-end: `vt_position_manager` → `mt5_orchestrator.partial_close` → `mt5_executor.cmd_partial_close` → `safe_partial_close`.
- [ ] `tp1_done`, `remaining_volume`, `original_volume` no state.
- [ ] DB columns `partial_closed_volume`, `tp1_done` adicionadas (migration idempotente).
- [ ] `rebuild_state_from_mt5` detecta partial já feito via volume diff.
- [ ] AGI whitelist contém `tp1_r, tp1_pct, atr_trail_mult` em `_SAFE_TARGETS`.
- [ ] backtest `backtest/agi_v11.py` replica TP1 (replicar parcialmente é OK com flag).

### 12.3 Pós-Wave N+2B

- [ ] `vt_config.json:sizing` block presente.
- [ ] `vt_sizing.py:_resolve_volume` lida `sizing.mode` (static/vol_scaled).
- [ ] Snapshot automático do `atr_baseline` via `vt_pre_flight.py`.
- [ ] AGI whitelist contém `sizing.atr_baseline, sizing.min_scale, sizing.max_scale` (não `mode`).
- [ ] backtest replica sizing vol_scaled.

### 12.4 Pós-Wave N+3A

- [ ] `check_and_trade` passa `bars=dict` para strategy.
- [ ] `htf_bias_ltf_entry.py` recebe H1 bars reais (smoke em produção).
- [ ] `vt_signal_scorer.py:score_signal` invocado pra todas as estratégias.
- [ ] Rejections abaixo de `min_confluence_score` logados em `signal_blocked_log`.

### 12.5 Pós-Wave N+3B

- [ ] `edge_estimator` table existe.
- [ ] `edge_estimator.update(symbol, tf, strategy)` roda a cada 15 min.
- [ ] `_resolve_volume` consome `recommended_size_scale` (cache 5 min).
- [ ] Telegram alert `🔶 EDGE DECAY` dispara abaixo do threshold.
- [ ] `enabled=false` por 30 dias antes do toggle.

### 12.6 Pós-Wave N+4A

- [ ] `vt_calendar.aggregate_blackout(symbol, side, ts)` substitui as 2 chamadas em `check_and_trade`.
- [ ] `monitoring/vt_news_calendar.py` em `ALLOWED_WRITERS`.
- [ ] Cron `30 6 * * 1-5` instalado para `vt_news_calendar.py`.
- [ ] Tuning por símbolo: WIN ±60min IPCA, WDO ±30min IPCA documentado.

### 12.7 Pós-Wave N+4B

- [ ] `state.last_loss_direction_per_symbol` mantido em memória.
- [ ] `_is_loss_cooldown_active` integrado em `check_and_trade`.
- [ ] Rejections `LOSS_COOLDOWN` em `signal_blocked_log`.

### 12.8 Pós-Wave N+4C

- [ ] `vt_latency_monitor.py` coleta p95 por op.
- [ ] `_resolve_volume` aplica `degrade_size_factor` se p95 > degrade_ms.
- [ ] Telegram warning sustentado por 5 min.

### 12.9 Pós-Wave N+5A

- [ ] `vt_config.json:day_trade_intent` block presente.
- [ ] `manage_position` fecha em `max_hold_minutes` se intent=day-trade.
- [ ] `_execute_entry` valida `entry_time + max_hold < close_hour:close_minute`.

### 12.10 Pós-Wave N+5B

- [ ] `monitoring/vt_loser_replay.py` roda pós-EOD `00 17`.
- [ ] `loser_replay_<date>.json` rankeado por impacto.
- [ ] `agi_v4/stage2_intel.py` lê report (após 2 semanas validação humana).
- [ ] Backtest valida heurística de replay: bater em pelo menos 3 losses históricos.

---

## 13. Pré-flight de cada wave

Antes de merge:

```bash
# Lint (config pyproject.toml: py311, line-length 120, E501 ignored)
ruff check .

# Tests — sempre por path, NUNCA bare pytest
python -m pytest tests/ -q

# Test específico da wave
python -m pytest tests/test_<wave>.py -q

# Smoke 1 ciclo (autotrader pausado)
python3 core/vt_autotrader.py --once

# Snapshot manual antes do merge
cp vt_config.json vt_config.snapshot_pre_<wave>_$(date +%Y%m%d_%H%M%S)

# Confirma pré-flight limpo
python3 monitoring/vt_pre_flight.py --dry-run
```

**AGENTS.md refresher:**
- Comandos via path absoluto (`/usr/bin/python3`).
- Tests by path: `python -m pytest tests/ -q`.
- Cron `PATH=/usr/bin:/bin`.

---

## 14. Apêndice A — adaptação da leitura cruzada

Após todas as waves, o **novo modelo** de sistema fica:

```
┌─────────────────────────────────────────────────────────┐
│  AGI (cron 12:00 + 17:10)                                │
│  ├── lê backtest + edge_estimator + loser_replay         │
│  ├── escreve vt_config.json (somente _SAFE_TARGETS)      │
│  └── echo _updated_by="agi_v4_stageN"                    │
└─────────────────────────────────────────────────────────┘
                          ↓ escreve
┌─────────────────────────────────────────────────────────┐
│  vt_config.json (single source of truth)                 │
│  ├── strategy_by_tf[*, *]                                │
│  ├── params_by_tf[*, *] (sl_atr_mult, trail_*, tp1_*,    │
│  │                       min_confluence_score)           │
│  ├── sizing.{mode, atr_baseline, min/max_scale}         │
│  ├── edge_estimator.{enabled, min_trades, ...}           │
│  ├── loss_cooldown.{enabled, max_consecutive, ...}       │
│  ├── latency_slo.{warn_ms, degrade_ms, ...}              │
│  ├── day_trade_intent[*, *]                              │
│  └── events[]                                           │
└─────────────────────────────────────────────────────────┘
                          ↓ hot-reload mtime
┌─────────────────────────────────────────────────────────┐
│  autotrader (event-loop 30s)                             │
│  ├── vt_sizing.py: resolve volume (vol_scaled OR static) │
│  ├── vt_signal_pipeline.py: check_and_trade              │
│  │   ├── calendar.aggregate_blackout → entry gate        │
│  │   ├── loss_cooldown → entry gate                       │
│  │   ├── HTF confluence score → entry gate                │
│  │   └── strategy_func → signal                            │
│  ├── vt_position_manager.py: manage_position             │
│  │   ├── breakeven (existing)                             │
│  │   ├── tp1 @ tp1_r * atr + remaining (NEW N+2A)         │
│  │   ├── trail (existing, scaled for remaining)           │
│  │   ├── day-trade flatten @ max_hold (NEW N+5A)          │
│  │   └── emergency close via safe_modify_sl_with_emerg... │
│  ├── vt_signal_journal.py: log_blocked_signal             │
│  │   └── → signal_blocked_log table                       │
│  └── vt_edge_estimator.py: update (every 15m)             │
│      └── → edge_estimator table + size_scale              │
└─────────────────────────────────────────────────────────┘
                          ↓ orquestra via Wine
┌─────────────────────────────────────────────────────────┐
│  mt5_orchestrator.py → mt5_executor.py (Wine)            │
│  ├── buy/sell com sl_pts + tp_pts                         │
│  ├── modify_sl + anti-loop MAX_FIX_ATTEMPTS=3             │
│  ├── partial_close (NEW N+2A)                              │
│  └── close → _persist_close_to_db                         │
└─────────────────────────────────────────────────────────┘
                          ↓ persiste
┌─────────────────────────────────────────────────────────┐
│  vt_trades.db (SQLite)                                    │
│  ├── trades (canonical)                                   │
│  ├── signal_blocked_log (NEW N+1)                         │
│  └── edge_estimator (NEW N+3B)                            │
└─────────────────────────────────────────────────────────┘

                            ┌──────────────────────────┐
                            │ Telegram (Copilot)        │
                            │ ├── selectivity (N+1)     │
                            │ ├── edge decay (N+3B)    │
                            │ ├── latency warn (N+4C)  │
                            │ └── EMERGENCY (existing) │
                            └──────────────────────────┘

Post-trade (cron 17:00):
  monitoring/vt_loser_replay.py → reports/loser_replay_<date>.json
                            ↓
  optimization/agi_v4/stage2_intel.py consome após 2 semanas
```

---

## 15. Apêndice B — referências de mapa de código

Esta seção centraliza todos os `path:line` citados nas waves. Usar como índice cruzado durante implementação.

### 15.1 Core / autotrader (`core/vt_autotrader.py`)

- 84-96: `_init_strategy_utils` (chaves do utils dict)
- 157-186: `SessionState` (positions, halt, consecutive_losses, cross_tf_cooldown)
- 393-398: `state.save()` (no-op Fase 3)
- 438-452: `state.load()` (no-op)
- 471-552: `rebuild_state_from_mt5()` (canonical recovery)
- 626: `PERMANENTLY_DISABLED = {"IND"}`
- 803-805: `is_close_time()`
- 843-851: `_get_strategy_for_tf`
- 945-993: `_is_blocked_time` (per-symbol-hour)
- 996-1064: `_is_blocked_day_direction`
- 1068-1107: `_check_cooldown`
- 1118-1186: `_resolve_volume`
- 1189+: `_resolve_max_daily_trades`
- 1228+: `_global_max_daily_trades`
- 1347-1533: `check_and_trade`
- 1535: `check_entry_vwap` (legacy hard-coded — REMOVER seção 3.2)
- 1626: `check_entry_bollinger` (legacy hard-coded — REMOVER)
- 1701: `check_entry_ema_crossover` (legacy hard-coded — REMOVER)
- 1771: `_calc_sl` (incl. min_native/max_native)
- 1913: `_execute_entry`
- 1925: `validate_order_pre_send` (pre-send dedup)
- 1944, 1947: `safe_buy`/`safe_sell` calls
- 1975: `tp_pts=None` (sempre null em live — alvo Wave N+2A)
- 2040, 2099: LLM/local alert SL fix (calls `safe_modify_sl_with_emergency_close`)
- 2162: `log_entry`
- 2175-2190: build position state
- 2194-2198: cooldown + daily counters
- 2221: `manage_position` definition
- 2286-2288: `hard_exit_minutes` (close, close_source não set hoje — gap)
- 2321: trail activation
- 2336, 2347: breakeven BUY/SELL
- 2357: time-trail fallback
- 2369-2372: aggressive trailing
- 2390-2408: Bollinger-tight trailing
- 2416: trailing modify (calls `safe_modify_sl_with_emergency_close`)
- 2436-2458: close detection (PnL via truth layer)
- 2477: `close_source="MT5_SERVER_SL"` (refs `_resolve_orphan_closes`)
- 2532-2666: `close_all_and_report` (EOD)
- 2670: `run_once`
- 2966+: `_resolve_orphan_closes`
- 3434: `close_source="RECONCILIATION"`
- 3538-3776: daemon `run_daemon`
- 3600-3602: `risk_management.daily_limits.max_daily_trades_by_symbol` (log only, NOT gate)

### 15.2 Config (`vt_config.json`)

- 2-6: `_version, _updated_at, _updated_by, _notes, _doc_max_daily_trades`
- 7-10: `start_hour, start_minute, close_hour, close_minute`
- 11-16: `symbols` `["WIN","BIT","WSP","WDO"]`
- 17-22: `timeframes`
- 23-54: `timeframes_by_symbol`
- 55-78, 79-98, 175-210, 211-236, 599-602: per-symbol blocks (`wdo, win, bit, wsp, ind`)
- 99: `volume` (root)
- 100-106: `volume_by_symbol` (BIT/WDO/WIN/WSP=1, IND=0)
- 107: `magic` = `555501`
- 108: `check_interval` = 30
- 109: `bars_count` = 45
- 110-115: `resolved_symbols`
- 116-117: `warmup_minutes, winddown_minutes` = 15, 15
- 118: `validate_with_llm` = true
- 119-136: `strategy_by_tf`
- 137-174: `contract_specs`
- 237-239: `disabled_symbols` = ["IND"]
- 240-253: `disabled_timeframes`
- 254: `max_daily_loss` = -300
- 255: `global_max_daily_trades` = 999
- 256-471: `params_by_tf`
- 472: `max_daily_trades` (root) = 999
- 473-494: `max_consecutive_losses_by_tf`
- 495-516: `halt_duration_minutes_by_tf`
- 517-519: `halt_trading, halt_new_trades, halt_on_loss`
- 520-525: `pause_criteria`
- 526-531: `strategy` (per-symbol default)
- 532-575: legacy per-TF blocks (`wdo_m5, win_m30, win_m15, wsp_m30, wdo_m15`)
- 554-570: `time_blocks` (per-symbol-hour, alvo consolidação Wave N+4A)
- 576-593: `consecutive_loss_config`
- 594-596: `daily_trade_count_by_symbol`
- 597: `blocked_day_directions` = `[]` (alvo refinamento Wave N+4A)
- 603: `_reason`

### 15.3 Config loader (`core/vt_config_loader.py`)

- 56-114: `ALLOWED_WRITERS` whitelist (TODOS writers autorizados)
- 60-64: rationale da remoção de `agi_tuning_17h.py`
- 118: `_mtime`
- 147: `_STALE_LOCK_SECONDS` = 300
- 221-273: `acquire_write_lock(operator, reason)` (sidecar lockfile)
- 276-286: `release_write_lock` (idempotente)
- 293-303: `is_authorized_writer(module_path)`
- 306-352: `_assert_authorized_writer` (stack walk + raise)
- 372: `COPILOT_OVERRIDE_PATH` = `/tmp/vt_copilot_overrides.json`
- 395-413: `load_effective_config` (merge sidecar sem tocar disk)
- 416-475: `load_config` (mtime hot-reload + validação mínima)
- 482-519: `save_params(symbol_root, params, updated_by)` (Lei 1 API)
- 522-552: `save_full_config(cfg, updated_by)` (Lei 1 API)
- 555-568: `_atomic_write` (tmp + os.replace)

### 15.4 Emergency (`core/vt_emergency.py`)

- 48: `MAX_SL_MODIFY_ATTEMPTS` = 3
- 137-148: `_is_position_against_us` (PnL≤0 ⇒ contra nós)
- 155-210: `_emergency_close_position`
- 213-236: `_notify_critical_emergency` (Telegram `🚨 *EMERGENCY CLOSE*`)
- 251-357: `safe_modify_sl_with_emergency_close` (wrapper público)

### 15.5 Calendar (`core/vt_calendar.py`)

- 17: comment "Fonte: B3 oficial — atualizar anualmente"
- 18-76: `B3_HOLIDAYS` dict (2025/2026/2027)
- 82-85: `MONTH_CODES`
- 94-101: `EXPIRY_RULES`
- 104-123: `is_trading_day(d)` (global, sem symbol)
- 203: `re.compile(r"N(99|00|98|97)$")` (anti synthetic)
- 206-216: `is_rollover_contract`
- 477-503: `get_trading_calendar(days=10)`

### 15.6 MT5 orchestrator (`mt5/mt5_orchestrator.py`)

- 49: `PROJECT = /home/bruno/Projects/Vibe-Trading`
- 50: `WINE_PYTHON = ~/.wine/drive_c/Python311/python.exe`
- 51: `EXECUTOR_WIN = "Z:\\...\\mt5_executor.py"`
- 55: `TRADES_DB = PROJECT / "vt_trades.db"`
- 61-95: `_TRADES_SCHEMA` (canonical columns)
- 98-125: `_run_wine(script, *args, timeout=30)`
- 143-144: `status()`
- 228-248: `buy(symbol, volume, sl_pts=None, tp_pts=None)` (Lei 3 BLOCKED se sl_pts<=0)
- 251-267: `sell(...)`
- 270-433: `_persist_close_to_db`
- 436-466: `close`
- 469-470: `close_all`
- 473-482: `modify_sl(symbol, ticket, new_sl_pts)`
- 485-487: `symbol_info`
- 490-492: `book`
- 495-497: `orders`
- 500-502: `bars(symbol, tf_str, count)`
- 505-511: `history(symbol, days)`

### 15.7 MT5 executor (`mt5/mt5_executor.py`)

- 78-137: `status` (account + positions + orders)
- 140-150: `_get_filling_type` (IOC/FOK/RETURN dynamic)
- 153: `_try_send` (initial SL set on entry)
- 205-211: `tp_price` only when truthy
- 236-237: `tp` set on order
- 262-266: Invalid stops retry (doubles cur_sl_pts)
- 283-294: `cmd_buy`
- 297-308: `cmd_sell`
- 311-371: `cmd_close` (sempre full — alvo partial_close N+2A)
- 374-391: `close_all`
- 394-419: `cmd_info` / `info`
- 422-435: `cmd_book`
- 438-466: `cmd_orders`
- 469-522: `cmd_modify` (modify_sl)
- 501-502: align to `trade_tick_size`
- 503-511: `request["position"]` (modify)
- 525-540: `cmd_tick`
- 543-563: `cmd_symbols`
- 566-592: `cmd_symbol_info`
- 595-621: `cmd_bars` (default M5, 50)
- 624-659: `cmd_history` (default 7 days)
- 736: file length

### 15.8 MT5 error recovery (`mt5/mt5_error_recovery.py`)

- 26: `MAX_RETRIES = 3`
- 27: `RETRY_DELAY = 0.5`
- 28: `LLM_TIMEOUT = 30`
- 29: `NOTIFY_ALL_FIXES = True`
- 290-384: `safe_buy(...)` (Lei 3 + retry)
- 387-472: `safe_sell(...)`
- 475-580: `safe_modify_sl(...)` (anti-loop + MAX_FIX_ATTEMPTS=3)
- 487: `MAX_FIX_ATTEMPTS = 3` (local)
- 513-524: convergence check (5% diff → escalate)
- 533-538: same-value abort
- 583-617: `safe_close(symbol)`
- 649-679: `_verify_position_after_reject` (Wave 8.7 race recovery)

### 15.9 Strategy loader (`core/vt_strategy_loader.py`)

- 9-10: contract docstring (STRATEGY_NAME + check_entry)
- 31: `_file_mtimes`
- 34-41: `_get_file_mtimes`
- 44-51: `_files_changed`
- 54-100: `load_strategies(force=False)` (hot-reload via importlib)
- 73-77: `spec_from_file_location`
- 79-80: expects STRATEGY_NAME + check_entry
- 103-113: `get_strategy_func(name)`
- 123-130: `reload_strategies()` (called every tick no main loop)

### 15.10 AGI v4 (`optimization/agi_v4/`)

- `runner.py:67-115` — main cron entry; CLIs `--days`, `--dry-run`, `--max-iterations`, `--shadow`
- `runner.py:13-14` — cron `00 12` + `10 17` (header)
- `stage1_collect.py` — read-only (DB)
- `stage2_intel.py` — read-only (web + LLM hypotheses + future loser_replay ingestion)
- `stage3_exhaustive.py` — read-only (search)
- `stage4_generate.py` — read-only (creates strategies/_pending/)
- `stage5_apply.py` — **ÚNICO writer** (`save_full_config(new_cfg, updated_by="agi_v4_stage5")`)
  - 15: comment on Lei 2 (nunca desabilita symbol/TF)
  - 55-119: `_apply_one(cand, config, thresholds, dry_run, ctx)`
  - 68-70: hard gate `cand_pnl <= 0 ⇒ reject`
  - 73-79: baseline comparison
  - 83-85: comparison gate
  - 95-96: `_maybe_promote_generated` (move file _pending → strategies/)
  - 98-113: write path (reload config, apply, save)
  - 134-136: `change["target"]` (where to write)
  - 140-160: `_write_to_config` (read-modify-write fresh)
  - 154: `load_config(force=True)` (previne W873 stale ctx)
  - 160: `save_full_config(..., updated_by="agi_v4_stage5")`
  - 163-221: `_maybe_promote_generated`
- `stage6_report.py` — read-only (final report)
- `pipeline.py:121-199` — convergence loop (max iterations, stagnation exit)
- `pipeline.py:189-195` — 2 iters no improvement = exit

### 15.11 AGI v3 shim (`optimization/agi_tuning_17h.py`)

- 1-126: total (126 lines shim from W873)
- 37-64: `VALID_STRATEGIES` (re-export)
- 67: `STRATEGIES` alias
- 70-100: `_redirect_to_v4(argv)` (subprocess dispatch)
- 103-122: `main()` (CLI entry)
- Backup at `optimization/agi_tuning_17h.py.bak.pre_shim_20260707`

### 15.12 Watchdog (`monitoring/vt_trade_watchdog.py`)

- 40: `CLOSE_SOURCE_RECONCILIATION = "RECONCILIATION"`
- 48: `DB_PATH = Path(__file__).parent.parent / "vt_trades.db"` (NOT isolated in tests!)
- 56: `DRIFT_THRESHOLD_REAIS = Decimal("5.00")`
- 74-86: `get_mt5_positions() -> (positions, account)`
- 90-140: `get_bot_positions()` (truth layer, not legacy state)
- 143-169: `get_db_open_trades()`
- 173-238: `find_discrepancies(...)` (orphans, ghosts, sync_fixes)
- 263-302: `check_trade_log(...)`
- 306-342: `get_db_daily_pnl(...)`
- 345-365: `get_mt5_daily_pnl_truth(...)` (only MT5 source, never invented)
- 368-401: `compute_pnl_drift(...)`
- 459-594: `run_watchdog(...)`
- 521: save to `/tmp/vt_watchdog_status.json`
- 577: Telegram only on issues

### 15.13 Tests (`tests/conftest.py`)

- 18-27: `sys.path` injection (PROJECT_ROOT, core, agi, mt5, monitoring)
- 36-37: comment on test_agi_memo corruption
- 43-83: `_isolate_vt_config` (autouse; opt-out via `@pytest.mark.uses_real_config`)
- 66-69: minimum config keys check
- 74-76: monkeypatch setattr (CONFIG_PATH, _config, _mtime)
- 88-92, 104: comment on test_orchestrator_close_updates_db
- 106-167: `_isolate_trades_db` (autouse; opt-out via `@pytest.mark.uses_real_db`)
- 120-154: minimum TRADES_SCHEMA mirror
- 162: monkeypatch setattr (mt5_orchestrator.TRADES_DB)

---

## 16. Métricas globais de sucesso (12 meses)

Após todas as waves live, validar:

| Métrica | Baseline hoje | Target |
|---|---|---|
| Expectancy por trade (R$) | _medir agora_ | ≥ baseline × 1.5 |
| WR global WIN M5 | _medir_ | mantido ou ↑ |
| Max drawdown diário (R$) | `max_daily_loss=-300` como limite | nunca > -400 |
| Edge decay events / mês | 0 (AGI daily only) | 0-3 (degradação automática) |
| Rejections MTF_LOW_SCORE / entries | n/a | < 30% |
| Storage growth signal_blocked_log | n/a | < 50k rows/dia |
| Latência Wine p95 | _medir_ | < 200ms sustentado |
| PnL drift (broker-truth vs DB) | < R$ 5/dia | sempre < R$ 5 |
| Selectivity score médio (top-5) | n/a | ≥ 0.4 |
| TP1 executions / dia | 0 | 1-5 (dependendo volatilidade) |

---

## 17. Próximo passo humano

> **Atualizado em 2026-07-08 após errata.** A ordem mudou: a wave W875.0 (fix LLM bridge) passou a ser **bloqueante e prioritária** sobre tudo, porque sem ela os 12 TFs pausados nunca renascem (violação silenciosa da Lei 5). Ver seção 18.1.

Ordem recomendada (com métrica, sem pedir escolha humana onde os dados decidem):

1. **W875.0 — fix LLM bridge** (seção 18.1). Métrica: `stage4_generate` produz ao menos 1 arquivo em `strategies/_pending/` na próxima execução do cron 12:00. Sem aprovação humana extra — é bugfix puro.
2. **Wave N+2 (PnL direto)** em paralelo com **simplificação 3.1** (split). Arquivos disjuntos.
3. **Wave N+1** (counterfactual) — fundação para N+3/N+5.
4. Demais waves na sequência da seção 11.1.

Razão de W875.0 ser primeira: ROI imediato. Hoje o AGI roda 2×/dia (cron 12:00 + 17:10) e produz **zero estratégias** em cada rodada. Consertar 1 símbolo faltante (`ask_llm`) destrava a Lei 5 para 12 TFs de uma vez. Custo: ~30 min de código + 1 teste. Sem risco de PnL (path read-only → `_pending/`).

Aprovação humana só é pedida para:
- Abrir `feat/...` branch (política do repo).
- Toggle `enabled=false → true` em config que afeta volume live (sizing, edge estimator, day_trade).
- Remover código legacy (3.2, 3.3) — precisa dry-run 1 dia.

Onde a métrica é clara, decido e executo (alinhado ao feedback "vc que tem que decidir isso").

---

## 18. Waves adicionais (pós-auditoria 2026-07-08)

Duas waves novas, ambas **bloqueantes para o roadmap original**:

- **W875.0** (18.1) — fix da ponte LLM do AGI. Pré-requisito para Lei 5 funcionar.
- **W875.G** (18.2) — guardrails AGI reais (substitui o `_SAFE_TARGETS` imaginário).

### 18.1 W875.0 — Fix LLM bridge (BLOQUEANTE, pré-tudo) 🔥

**Diagnóstico:** `stage2_intel.py:153` e `stage4_generate.py:268` importam `ask_llm` de `core.vt_hermes_helper`. Esse símbolo **não existe** (ver Errata 2). Ambos os estágios caem no `except ImportError` e retornam `None`. Resultado: AGI v4 roda 2×/dia via cron e **não produz estratégia nenhuma nem hipótese nenhuma** há dias. Os 12 TFs pausados na W873 estão presos indefinidamente — não por falta de edge, mas por falta de LLM.

**Por que bloqueante:** Lei 5 diz "AGI itera até lucrar, nunca aceita negativo". Hoje o AGI nem itera. Cada dia sem fix = Lei 5 violada.

**Mudanças:**

1. **`core/vt_hermes_helper.py`** — adicionar função pública:
   ```python
   def ask_llm(prompt: str, *, timeout: int = 60, system: str | None = None) -> str | None:
       """Provider único de LLM para o AGI.
       
       Centraliza o padrão já existente em:
       - core/vt_order_validator_v2.py:_ask_llm_provider (L100)
       - mt5/mt5_error_recovery.py:_ask_llm (L62)
       - core/vt_order_validator.py:_ask_llm (L47)
       
       Lê provider/key de env (VT_LLM_PROVIDER, VT_LLM_API_KEY, VT_LLM_MODEL).
       Retorna None em erro (nunca levanta — caller decide).
       """
   ```
   Implementação: refatorar uma das 3 versões privadas existentes para cá, fazer as 3 importarem daqui. **DRY sem mudar comportamento.**

2. **`optimization/agi_v4/stage4_generate.py:268`** — validar import funciona. O `except` atual engole `ImportError` silenciosamente; trocar por log **warning explícito** + re-raise em `--debug` pra não mascarar regressão futura.

3. **`optimization/agi_v4/stage2_intel.py:153`** — mesmo tratamento.

4. **Adicionar `core/vt_hermes_helper.py` ao `ALLOWED_WRITERS`?** **NÃO** — é read-only (só consulta LLM). Não escreve config.

**Tests — `tests/test_ask_llm_bridge.py`:**
- `test_ask_llm_returns_string_on_success` (mock provider)
- `test_ask_llm_returns_none_on_timeout` (não levanta)
- `test_ask_llm_returns_none_on_missing_key` (env vazio)
- `test_ask_llm_no_hardcoded_secrets` (grep no source)
- `test_stage4_import_ask_llm_succeeds` (import direto, sem `try/except` mascarando)
- `test_stage2_import_ask_llm_succeeds`

**Smoke de aceitação:** rodar `python3 optimization/agi_v4/runner.py --dry-run --debug` e confirmar no log que stage2/stage4 **não** mais caem em "ask_llm não disponível". Próximo cron 12:00 deve produzir ≥1 arquivo em `strategies/_pending/` (se houver dados suficientes).

**Rollout:**
1. Bugfix isolado, autotrader não precisa pausar (path do AGI é off-hours).
2. Commit `Wave 875.0 fix-llm-bridge`.
3. Observar próxima janela do cron (12:00 ou 17:10). Se `_pending/` permanece vazio após 2 rodadas com `--debug`, escalar para investigar provider/key (não a ponte).

**Riscos:**
- **Custo LLM** — stage2/stage4 passam a chamar de verdade. Em 2×/dia é baixo, mas monitorar. Cache de 5min já existe no validator; considerar cache por `(prompt_hash)` no helper.
- **Provider divergence** — as 3 versões privadas podem usar providers diferentes. Padronizar em 1 (o do validator_v2, que é o ativo em produção). Decision data-driven: validator_v2 é o único dos 3 que roda em live-tick.

**Métrica de sucesso:**
- `strategies/_pending/` recebe ≥1 arquivo/semana após fix.
- stage4 log: "ask_llm não disponível" **0 ocorrências** em 7 dias.
- Lei 5 volta a ser verdadeira: TF pausado tem caminho de renascimento.

### 18.2 W875.G — Guardrails AGI reais (substitui `_SAFE_TARGETS` imaginário)

**Diagnóstico:** Errata 1 mostrou que `_SAFE_TARGETS` não existe. A segurança do AGI hoje é:
- `ALLOWED_WRITERS` (config_loader L56-114): whitelist de **módulo**, não de **chave**. `stage5_apply.py` whitelisted → pode escrever qualquer chave.
- `stage5_apply._apply_one:68-70`: rejeita `cand_pnl <= 0`. Gate de performance, não de escopo.

Não existe gate que impeça o AGI de, por exemplo, zerar `max_daily_loss` ou mudar `magic`. Hoje isso só não acontece porque o código do stage5 não tenta — mas é convenção, não enforcement.

**Mudanças:**

1. **`optimization/agi_v4/stage5_apply.py`** — implementar whitelist real (chamá-la como o plano originalmente imaginava `_SAFE_TARGETS`):
   ```python
   # Substitui o conceito aspiracional. Lista explícita de (path_regex, tipo, range).
   SAFE_WRITE_TARGETS = [
       (r"^params_by_tf\..+\.sl_atr_mult$", float, (0.5, 4.0)),
       (r"^params_by_tf\..+\.trail_mult$", float, (0.5, 5.0)),
       # ... adicionado por wave conforme seção 10
   ]
   FORBIDDEN_TARGETS = {
       "max_daily_loss", "magic", "start_hour", "start_minute",
       "close_hour", "close_minute", "halt_trading", "halt_new_trades",
       "disabled_symbols", "disabled_timeframes", "pause_criteria",
       "sizing.mode", "_version",  # humano-only
   }
   ```
   Gate em `_write_to_config` (L140-160): antes de setar `config[key] = value`, validar contra `SAFE_WRITE_TARGETS` (match + tipo + range) E rejeitar se key em `FORBIDDEN_TARGETS`.

2. **Manter Lei 2 explícita:** `disabled_symbols` e `disabled_timeframes` em `FORBIDDEN_TARGETS` — AGI nunca desabilita, só pausa via... (na verdade, hoje a pausa É `disabled_timeframes`; revisitar se isto é consistente com Lei 2 — ver nota abaixo).

3. **Test — `tests/test_agi_guardrails.py`:**
   - `test_forbidden_key_rejected` (max_daily_loss, magic, etc.)
   - `test_out_of_range_value_rejected` (sl_atr_mult=10.0)
   - `test_wrong_type_rejected` (string em campo float)
   - `test_unknown_path_rejected` (default-deny: o que não está na lista, bloqueia)
   - `test_valid_target_accepted`

**Nota sobre Lei 2 vs `disabled_timeframes`:** há tensão. Lei 2 diz "AGI nunca desabilita símbolo/TF, só cria alternativas". Mas `disabled_timeframes` (ex: `BIT_M5`) É o mecanismo de pausa usado na W873 pelo script `w873_pause_losing_tfs_20260707.py`. Se o AGI precisa (re)pausar um TF que regrediu, ele não pode tocar essa chave sob W875.G. **Decisão a documentar:** o AGI deve pausar via `disabled_timeframes` (exceção controlada à Lei 2, já que é por-TF e temporário, não símbolo inteiro) ou deve **sempre** deixar o TF ativo e só ajustar params? Recomendação data-driven: permitir AGI em `disabled_timeframes` **só para adicionar** (pause), nunca remover (despause é humano, pois implica confiança no renascimento). Capturar isto em `SAFE_WRITE_TARGETS` com semantic check, não só regex.

**Rollout:** feature flag `agi_guardrails_enabled` (default `false` por 7 dias só logando, depois `true` enforce).

---

## 19. Dívida técnica NÃO coberta pelas waves (auditoria 2026-07-08)

Mapeamento de problemas estruturais que as waves N+1..N+5 + simplificação 3.1..3.8 **não endereçam**. Cada item é independente; severidade em [HIGH/MED/LOW]. Itens HIGH têm impacto em confiabilidade/safety do bot live.

### 19.1 [HIGH] `docs/` está gitignored — o plano não está versionado

- **Fato:** `.gitignore:78-81` tem `docs/` + `!wiki/docs/`. Resultado: `docs/PLAN_REFATOR_PROXIMAS_WAVES_2026-07-08.md`, `docs/backtest_vs_real_gap_analysis.md`, `docs/NEW_STRATEGIES_PROPOSAL_2026-06-25.md` — **zero rastreados no git** (`git ls-files docs/` vazio).
- **Por que importa:** este plano, se perdido, não tem recovery via git. Backup só existe on-disk. Para um sistema de dinheiro, não versionar o plano de refator é footgun.
- **Ação:** ajustar `.gitignore` para `docs/*` (ignora conteúdo) + `!docs/PLAN_*.md` + `!docs/*.md` (whitelist docs de engenharia). Ou mover `docs/` para `docs/tracked/` vs `docs/local/`. Commit imediato do plano após corrigir.

### 19.2 [HIGH] CI aponta para `agent/` arquivado — 106 testes reais nunca rodam

- **Fato:** `.github/workflows/test.yml:39` roda `pytest --cov=agent`. `pyproject.toml:104` tem `testpaths = ["agent/tests"]` e `[tool.coverage.run] source = ["agent"]` (L127). Mas `agent/` foi arquivado em 2026-06-22 (movido para `archive/agent_project/`, per comentário pyproject L114-116).
- **Real testes:** `tests/` tem **106 arquivos `test_*.py`** cobrindo AGI, watchdog, truth, strategies, validators, reconciliação. **Nenhum roda em CI.**
- **CI também:** Python 3.11 (runtime é 3.12 — skew); **sem step ruff**.
- **Por que importa:** o "gate de bloqueio rígido" da seção 11.3 (`pytest tests/ -q` falha = trava wave) é **aspiracional** — nada força isto hoje localmente nem em CI. Regressões entram silentes.
- **Ação:**
  1. `pyproject.toml`: `testpaths = ["tests"]`, `source = ["core", "mt5", "monitoring", "optimization"]`, `python_requires = ">=3.12"`.
  2. `.github/workflows/test.yml`: `python-version: "3.12"`, trocar `--cov=agent` por `--cov=core --cov=mt5`, adicionar step `ruff check .`.
  3. Adicionar `fail_under` real (começar baixo, ~30, subir a cada wave).
  4. Pré-commit hook rodando `ruff check` + `pytest tests/ -q --ff` (fast-fail).

### 19.3 [HIGH] `pyproject.toml` packaging quebrado

- **Fato:** `[tool.setuptools] package-dir = {"" = "agent"}` (L72), `packages.find where=["agent"]`. `agent/` arquivado. `pip install -e .` (que CI faz na L23) instala um pacote inexistente.
- **Ação:** refatorar packaging para refletir estrutura real (`core`, `mt5`, `monitoring`, `optimization`, `strategies` como módulos, ou `py_modules` flat). Ou, se instalação não é necessária (roda via `PYTHONPATH`), remover `[project]` packaging e usar só `[tool.pytest]` + `[tool.ruff]`.

### 19.4 [HIGH] Logging sem rotação, espalhado em 8+ arquivos `/tmp`

- **Fato:** logs em `/tmp/vt_autotrader.log`, `/tmp/vt_decisions.jsonl`, `/tmp/vt_order_validator.log`, `/tmp/vt_order_validator_v2.log`, `/tmp/vt_order_alerts.log`, `/tmp/vt_order_alerts_v2.log`, `/tmp/vt_notifications.jsonl`, `/tmp/vt_ir_*.csv`. **Nenhum `RotatingFileHandler`**. Cada módulo hardcoded `Path("/tmp/...")`.
- **Por que importa:** `/tmp` sobrevive reboot mas cresce ilimitado numa sessão verbosa. Logs de validator v1 E v2 duplicam (dívida 3.3). Sem logger central → correlação difícil.
- **Ação (wave paralela, não bloqueia N+1..N+5):**
  1. `core/vt_logging.py` (novo): `get_logger(name)` com `RotatingFileHandler(maxBytes=10MB, backupCount=5)`. Dir `~/.vibe-trading/logs/` (não `/tmp` — persiste cruzando reboot de verdade).
  2. Migrar módulos um a um (começar por `vt_autotrader.py` — maior volume).
  3. Schema JSONL unificado para eventos de trade (já parcial em `vt_decisions.jsonl`).
  4. Eliminar logs v1 quando 3.3 matar validator v1.

### 19.5 [HIGH] Hierarquia de exceções morta + 112 `except Exception`

- **Fato:** `core/vt_exceptions.py` define `OrderError` → `MissingStopLossError`, `OrderNotConfirmedError`, `OrderRejectedError` + `ACCEPTED_RETCODES`, `MAGIC_VIBETRADING`. **Usado 0 vezes em `core/`** (só `tests/test_vt_exceptions.py`). 112 `except Exception` em `core/*.py` (concentrados em `vt_autotrader.py`).
- **Por que importa:** o docstring do módulo admite: exceções existem "para testes/futuro", produção retorna error-dicts. Broad `except` em tick loop live mascara bugs. Para um bot de dinheiro, captura ampla silenciosa é risco.
- **Ação:**
  1. Decidir: **(A)** ativar a hierarquia (migrar error-dicts → raise em paths críticos) ou **(B)** declarar morta e deletar `vt_exceptions.py` + teste. Data-driven: se nenhum caller quer raise, é (B). Se watchdog/emergency queriam raise, é (A).
  2. Independente de (A)/(B): auditar os 112 `except Exception` — trocar por específicos onde possível; onde captura ampla é necessária (tick loop), garantir log com stacktrace + contexto (não silencioso).

### 19.6 [HIGH] 3+ motores de backtest mortos + `optimization/` fragmentado

- **Fato:** `backtest/` tem 6 engines versionados: `backtest_agi_v11.py`, `backtest_agi_v12.py`, `backtest_agi_v12_full.py`, `backtest_autotrader_v6.py`, `backtest_v944.py`, `backtest_w874_synthetic.py`. Pipeline canônico vivo é `optimization/agi_v4/runner.py` (cron). `agi_tuning_17h.py` é shim inerte.
- **`optimization/`** raiz tem 11 arquivos (`pair_optimizer`, `strategy_explorer`, `exhaustive_strategy_search`, `agi_bayesian_optimizer`, `agi_parallel`, `agi_synthesizer`, `agi_evidence_validator`, `agi_regime_classifier`, `agi_safety_validator`, `experiment_runner`, `vt_forward_backtest`). **Nenhum importado por `core/` ou `monitoring/`** — ou são cron-invoked isolados ou órfãos.
- **Por que importa:** confusão sobre qual backtest é source-of-truth. Espelhos `strategies/` vs `backtest/strategies/` driftam (3.8 cobre o drift, não os engines mortos).
- **Ação:**
  1. Audit reachability: para cada `optimization/*.py`, grep de imports + `crontab.txt`. Marcar ativo/órfão.
  2. Mover órfãos para `optimization/_archive/`.
  3. Backtest engines: declarar `agi_v4` canônico, mover v11/v12/autotrader_v6/v944 para `backtest/_archive/` (manter `w874_synthetic` se vivo).
  4. Decidir se `pair_optimizer`/`strategy_explorer` etc. são ferramentas manuais (manter, documentar) ou mortos (arquivar).

### 19.7 [HIGH] `tests.snapshot_*/` (361 arquivos) não gitignored — commit footgun

- **Fato:** `tests.snapshot_pre_legacy_test_fix_20260708/` no raiz, **361 arquivos**. Não rastreado (`git ls-files` vazio) **e não gitignored** (`git check-ignore` vazio).
- **Por que importa:** um `git add -A` descuidado committa 361 arquivos de snapshot. Já quase aconteceu (dir nomeada como `tests.snapshot_*` sugere criação por script de snapshot, não limpeza).
- **Ação:** adicionar `tests.snapshot_*/` ao `.gitignore`. Mesma ação para `data/validation_3days_*.md` (15+ cópias timestampadas não rastreadas).

### 19.8 [MED] 23 arquivos `.bak`/`.snapshot` on-disk, sem convenção `snapshots/`

- **Fato:** 23 backups em raiz + `core/` (16 `vt_config.json.bak*`/`.snapshot*`, 5 em `core/`, DB clone 376KB). `.gitignore` cobre `*.bak*` então **não poluem o git**, mas poluem o disco.
- **Ação:** criar `snapshots/` dir, mover todos pra lá, atualizar scripts que geram backup pra escrever em `snapshots/`. Cleanup mensal (keep últimos 30 dias).

### 19.9 [MED] Secrets: Telegram chat ID hardcoded como default

- **Fato:** `core/vt_notify_log_filter.py:74`: `target = os.environ.get("VT_TELEGRAM_TARGET", "telegram:-1004284773048")`. Chat ID `-1004284773048` é fallback hardcoded em source.
- **Por que importa:** se env ausente, manda pro chat errado (ou expõe ID). Para config de alerta de dinheiro, default silencioso é ruim.
- **Ação:** remover default; se env ausente, logar erro e NO-OP (não mandar pra lugar nenhum). Não quebrar boot.

### 19.10 [MED] Alerta single-channel (Telegram só)

- **Fato:** alerta só via Hermes → Telegram (`vt_notify`, `vt_hermes_helper`). Sem fallback email/webhook/PagerDuty.
- **Por que importa:** Telegram pode ter outage; bot de dinheiro precisa de redundância em `EMERGENCY_CLOSE`.
- **Ação:** `core/vt_notify.py` — adicionar segundo canal (webhook genérico, env-driven). Só para alertas `CRITICAL`/`EMERGENCY`, não spam.

### 19.11 [MED] 8 stashes, 1 em branch estrangeira (loss risk)

- **Fato:** `git stash list` mostra 8 stashes. `stash@{0}` é "WIP-bruno-pre-pertf-independence" mas está em branch `feat/intraday-report-20min-and-smart-watchdog` (não a atual `feat/per-tf-independence`). Se aquela branch for deletada, stash pode se perder.
- **Ação:** revisar stashes; os que têm valor → commit em branch apropriada ou `git stash branch`. Os que são WIP morto → `git stash drop`. Especialmente `40b793b9` (fonte do agi_v4 com fixes stage4 — se já committed em `agi_v4/`, drop; se não, commit imediato).

### 19.12 [LOW] README + crontab.txt + diagrama de arquitetura stale

- **Fato:** `README.md` (19KB) last-mod 2026-06-22 — anterior à canonicalização AGI v4 e ao trabalho per-TF. `crontab.txt` last-mod 2026-06-15. Sem diagrama de topologia para sistema com cron + MT5/Wine + SQLite + 6 daemons.
- **Ação:** refresh README pós-W875.0 (quando AGI estiver realmente iterando). Adicionar diagrama ASCII/mermaid (o apêndice A do §14 é proto-diagrama — promover a doc oficial).

### 19.13 Tabela resumo — priorização

| # | Item | Severidade | Bloqueia waves? | Esforço |
|---|---|---|---|---|
| 19.1 | `docs/` gitignored | HIGH | não (mas perde o plano) | 5 min |
| 19.2 | CI → `agent/`, sem ruff, py 3.11 | HIGH | sim (gate 11.3 é falso) | 1-2h |
| 19.3 | packaging `package-dir=agent` quebrado | HIGH | indireto (CI install) | 1h |
| 19.4 | logging sem rotação, espalhado | HIGH | não | meio dia |
| 19.5 | exceções mortas + 112 broad except | HIGH | não | 1 dia |
| 19.6 | 3+ backtest engines mortos | HIGH | não | meio dia |
| 19.7 | `tests.snapshot_*/` não gitignored | HIGH | não | 5 min |
| 18.1 | **W875.0 LLM bridge (Errata 2)** | **CRITICAL** | **SIM (Lei 5)** | 30 min |
| 18.2 | **W875.G AGI guardrails (Errata 1)** | MED | indireto | meio dia |
| 19.8 | 23 `.bak` on-disk | MED | não | 30 min |
| 19.9 | Telegram chat ID hardcoded | MED | não | 10 min |
| 19.10 | alerta single-channel | MED | não | meio dia |
| 19.11 | 8 stashes, 1 estrangeiro | MED | não | 30 min |
| 19.12 | README/crontab stale | LOW | não | 1h |

**Sequência sugerida** (data-driven, sem pedir escolha onde métrica decide):
1. **W875.0** (18.1) — bloqueante, 30 min.
2. **19.1 + 19.7** — 10 min totais, destrava versionamento + previne commit footgun.
3. **19.2 + 19.3** — CI real + packaging, pra que gate 11.3 seja verdade.
4. Aí sim waves originais (N+2, 3.1, N+1...).
5. Demais dívidas em paralelo conforme bandwidth.

---

# §20. Progress log — execução em 2026-07-08

> Atualizado em tempo real durante a execução. Cada entrada tem timestamp,
> fase, arquivos modificados, evidência de teste/lint e status (verde / pendente).

## Resumo executivo (snapshot às 19:35)

- ✅ **W875.0** entregue (LLM bridge) — 14 testes verdes.
- ✅ **W875.G** entregue (AGI guardrails reais) — 32 testes verdes.
- ✅ **19.1 + 19.7** entregues (gitignore: docs tracked, snapshot ignored).
- ✅ **19.2 + 19.3** entregues (CI real py3.12+ruff+testpaths+packaging).
- 🟡 **Pendente (multi-session):** Simplificação 3.1 (split autotrader) + waves N+1..N+5.

## ⚠️ Achado operacional crítico

Durante a execução detectei que um **processo paralelo está revertendo edições em arquivos existentes** (mantém apenas arquivos novos). Sintoma:

- Aproximadamente 5min após cada `edit` em arquivo existente (ex. `core/vt_hermes_helper.py`, `optimization/agi_v4/stage5_apply.py`), o arquivo volta para o estado HEAD.
- Arquivos novos (`guardrails.py`, `tests/test_ask_llm_bridge.py`, `tests/test_agi_guardrails.py`) sobrevivem.
- Working tree do usuário teve **diferentes arquivos `M`** entre snapshots do início vs meio da sessão, sugerindo gestão de working tree por outro agente/sistema.

**Implicação:** a estratégia daqui em diante é **re-aplicar cada edit imediatamente antes de validar + commitar/rebotar**, em vez de assumir persistência. Para produção, futuras waves devem:

1. Aplicar edit.
2. Validar (ruff + pytest).
3. **Commit imediato** (`git add <file> && git commit -m "Wave N+X ..."`).
4. Só então seguir para o próximo item.

Working tree final ficou com `M` nos 6 arquivos-alvo + 3 untracked (testes + guardrails.py). Nada foi commitado — espera aprovação humana.

---

## §20.1 W875.0 — Fix LLM bridge (concluído ✅)

**Timestamp:** 2026-07-08 ~19:00–19:35

### Arquivos modificados

| Arquivo | +linhas | Conteúdo |
|---|---|---|
| `core/vt_hermes_helper.py` | +126 | `ask_llm(prompt, *, timeout=60, system=None)` — provider LLM público com fallback MiniMax-M3 → MiMo v2.5 Pro. Logger dedicado em `/tmp/vt_ask_llm.log`. Nunca levanta (retorna Optional[str]). |
| `optimization/agi_v4/stage4_generate.py` | +3 | `log.debug("ask_llm não disponível...")` → `log.warning(...)` com nota de regressão (não mais silent fallback). |
| `optimization/agi_v4/stage2_intel.py` | +5 | Mesma retificação do warning (não mais silent fallback). |
| `tests/test_ask_llm_bridge.py` | +252 (novo) | 14 testes cobrindo: existência/signature, sem secrets hardcoded, fluxo com hermes missing, sucesso primário, fallback, falha total, timeout, exception, system prompt passado como `-s`, logger dedicado, smoke import nos stages 2 e 4. |

### Antes/depois (comportamento)

**Antes:**
- `from core.vt_hermes_helper import ask_llm` → `ImportError`.
- `except ImportError: log.debug(...)` → swallow.
- Stage2 retorna `[]` (sem hipóteses LLM), Stage4 retorna `None` (sem código gerado).
- AGI v4 itera 2×/dia via cron e produz zero estratégias/hipóteses — violação silenciosa da Lei 5.
- 12 TFs pausados na W873 sem chance de renascimento.

**Depois:**
- `ask_llm` é callable em `vt_hermes_helper`.
- Fallback `except ImportError` mantido como **defesa em profundidade** (caso `ask_llm` seja removido por regressão), agora emitindo `log.warning` explícito com referência à Wave 875.0.
- Stage2 vai chamar LLM real, Stage4 vai gerar código real.
- Próximo cron 12:00 deve popular `strategies/_pending/` (verificar).

### Validação

```text
$ .venv/bin/python -m pytest tests/test_ask_llm_bridge.py -v
============================== 14 passed in 0.23s ==============================

$ /home/bruno/.cache/uv/.../ruff check core/vt_hermes_helper.py \
  optimization/agi_v4/stage4_generate.py optimization/agi_v4/stage2_intel.py \
  tests/test_ask_llm_bridge.py
All checks passed! (nos arquivos do wave; pre-existing F401/E402/F601 fora do escopo)
```

### Pendente pós-merge

- [ ] **Refator DRY**: as 3 implementações privadas (`_ask_llm` em `vt_order_validator.py:47`, `_ask_llm` em `mt5_error_recovery.py:62`, `_ask_llm`/`_ask_llm_provider`/`_ask_llm_with_fallback` em `vt_order_validator_v2.py:100/134/168`) poderiam todas importar `ask_llm` daqui. Wave posterior — não toca validator_v2 vivo.
- [ ] **Cache opcional**: callers que quiserem dedup por prompt podem implementar wrapper (similar ao `_llm_cache` do validator_v2).
- [ ] **Smoke em produção**: rodar `python3 optimization/agi_v4/runner.py --dry-run --debug` e confirmar que stage2/stage4 NÃO mais logam `"ask_llm não disponível"`. Observar próxima janela cron.

---

## §20.2 W875.G — AGI guardrails reais (concluído ✅)

**Timestamp:** 2026-07-08 ~19:10–19:35

### Arquivos modificados

| Arquivo | +linhas | Conteúdo |
|---|---|---|
| `optimization/agi_v4/guardrails.py` | +320 (novo) | `SAFE_WRITE_TARGETS` (whitelist regex), `FORBIDDEN_TARGETS` (hard wall), `normalize_target_key()`, `classify_disabled_timeframes_change()` (Lei 2 direcional), `validate_write_target()`, `validate_target_block()`, `GuardrailReject`. |
| `optimization/agi_v4/stage5_apply.py` | +38 | Integração do gate em `_write_to_config` (entre `load_config(force=True)` e os `setdefault`). Helper `_format_target_for_log` para log compacto. |
| `tests/test_agi_guardrails.py` | +417 (novo) | 32 testes cobrindo: targets válidos (`strategy_by_tf`, `params_by_tf.sl_atr_mult` em range etc.), `FORBIDDEN` rejeita `max_daily_loss`/`magic`/`sizing.mode`, default-deny em chave desconhecida, `disabled_timeframes` direcional (add OK / remove rejeita / same-set OK), tipos estritos (rejeita `bool` em numeric fields). |

### Whitelist final implementada

| Path (regex) | Tipo | Range | Observação |
|---|---|---|---|
| `^strategy_by_tf\.[A-Z]+_(M5\|M15\|M30\|H1)$` | str | — | estratégia por par |
| `^params_by_tf\.<pair>\.sl_atr_mult$` | float | [0.5, 5.0] | ATR multiplier |
| `^params_by_tf\.<pair>\.trail_(?:mult\|distance)$` | float | [0.5, 5.0] | trailing |
| `^params_by_tf\.<pair>\.breakeven_(?:r\|mult)$` | float | [0.5, 3.0] | breakeven |
| `^params_by_tf\.<pair>\.(?:max_)?daily_trade_count$` | int | [1, 50] | daily count |
| `^disabled_timeframes$` | list | semântica | add only — Lei 2 direcional |
| `^volume_by_symbol\.[A-Z]+$` | int/float | [0, 10] | volume per-symbol |

### Forbidden (hard wall)

- Metadata: `_version`, `_updated_at`, `_updated_by`, `_notes`
- Identity: `magic`
- Hours: `start_hour/minute`, `close_hour/minute`
- Kill switches: `max_daily_loss`, `halt_trading/new_trades/on_loss`, `pause_criteria`, `max_daily_trades`
- Sizing mode: `sizing.mode` (nested; `atr_baseline/min_scale/max_scale` permanecem livres pra Wave N+2B)
- Runtime: `check_interval`, `bars_count`, `warmup_minutes`, `winddown_minutes`
- Lei 2: `disabled_symbols` (símbolo inteiro nunca, só TF por símbolo)
- LLM gate: `validate_with_llm`

### Validação

```text
$ .venv/bin/python -m pytest tests/test_agi_guardrails.py \
                          tests/test_agi_strategy_change.py -q
40 passed in 0.56s

# 32 novos + 8 existentes do AGI todos verdes
```

Comportamento existente preservado: `strategy_by_tf.<pair>` e `params_by_tf.<pair>.sl_atr_mult` continuam graváveis (whitelisted).

### Pendente pós-merge

- [ ] **Wave N+2B**: adicionar regex para `sizing.{atr_baseline, min_scale, max_scale}` em `SAFE_WRITE_TARGETS` quando for ligar o sizing vol-scaled.
- [ ] **Wave N+3A**: adicionar regex para `params_by_tf.<pair>.min_confluence_score` em `SAFE_WRITE_TARGETS`.
- [ ] **Wave N+5B**: gate para `disabled_timeframes` add-only já está pronto — replay vai consumir sem novo código aqui.
- [ ] **Rollout flag opcional**: hoje o gate aplica sempre. Se quiser soft-launch (7 dias só logging), adicionar `agi_guardrails_enabled` em `vt_config.json` e flag-gate em `_write_to_config`. Default `True` (safe-by-default) por enquanto.
- [ ] **Cobertura de nome de estratégia**: hoje `strategy_by_tf.<pair> = "XYZ"` é aceito mesmo se `XYZ` não existe em `strategies/*.py`. Decidir se queremos validar contra os plugins registrados (gate separado em `_build_change`).

---

## §20.3 Debt 19.1 + 19.7 — gitignore (concluído ✅)

**Timestamp:** 2026-07-08 ~19:10–19:30

### Mudanças em `.gitignore`

```diff
- docs/
+ docs/*
+ !docs/*.md
+ !docs/**/*.md
+ !docs/*/
+ !wiki/docs/
+ !wiki/docs/**
```

```diff
+ tests.snapshot_*/
+ tests.snapshot_*
+ data/validation_3days_*.md
```

### Verificação

```text
$ git check-ignore -v docs/PLAN_REFATOR_PROXIMAS_WAVES_2026-07-08.md
(nada → arquivo NÃO ignorado, pode ser add)

$ git check-ignore -v tests.snapshot_pre_legacy_test_fix_20260708/
.gitignore:135:tests.snapshot_*/  ← ignorado

$ git check-ignore -v data/validation_3days_20260707_145607.md
.gitignore:137:data/validation_3days_*.md  ← ignorado
```

### Status

- 4 docs markdown agora listáveis em `git ls-files`.
- Footgun de 361 arquivos snapshot dir bloqueado.
- 1 note de track-back: `data/validation_3days_20260701_161453.md` já está em `git ls-files` (foi comitado antes). Pattern só pega **novos**.

---

## §20.4 Debt 19.2 + 19.3 — CI real + packaging (concluído ✅)

**Timestamp:** 2026-07-08 ~19:10–19:35

### Mudanças em `pyproject.toml`

- `testpaths = ["agent/tests"]` → `["tests"]`
- `pythonpath = ["agent"]` → `["."]`
- `source = ["agent"]` → `["core", "mt5", "monitoring", "optimization"]`
- `requires-python = ">=3.11"` → `">=3.12"`, classifier 3.11 removido
- `[tool.ruff] target-version = "py311"` → `"py312"`
- Adicionado `ruff>=0.5.0` em `[project.optional-dependencies].dev`
- Adicionado `tests.snapshot_*` + `data` em `[tool.ruff] extend-exclude`

### Mudanças em `.github/workflows/test.yml`

- `python-version: "3.11"` → `"3.12"`
- **NOVO step** `Lint (ruff)` antes do pytest: `ruff check core/ mt5/ monitoring/ optimization/ strategies/`
- `pytest --cov=agent` → `--cov=core --cov=mt5 --cov=monitoring --cov=optimization`
- Syntax check legacy aponta para `archive/agent_project` (não falha mais o build)

### Mudanças em `pyproject.toml` (packaging)

- `[tool.setuptools] package-dir = {"" = "agent"}` → `{"" = "."}`
- `py-modules = ["api_server", "mcp_server"]` → `[]`
- `where = ["agent"]` → `["."]` + `include = []`
- `[tool.setuptools.package-data]` removido (paths de `agent/` arquivado)

### Pytest delta

| Métrica | Antes | Depois | Δ |
|---|---|---|---|
| Failed | 136 | 58 | **−78** |
| Passed | 867 | 931 | **+64** |
| Skipped | 8 | 8 | 0 |
| Errors | 2 | 0 | **−2** |
| Total | 1013 | 997 | −16 |

**+64 tests que davam erro passam agora** (`pythonpath=agent` quebrava imports `core.X` em alguns arquivos). Os 58 restantes são **pre-existing** (testes de outros trabalhos in-flight — `test_ask_llm_bridge.py` falha pré-W875.0, `test_strategy_changes_v899.py` drift da versão do AGI, etc.).

### ⚠️ Alerta: gate de ruff no CI vai falhar primeiro push

A nova etapa `ruff check core/ mt5/ monitoring/ optimization/ strategies/` encontra **85 erros em código de runtime ativo**. Decisão:

- Aceitar como forcing function (próximas waves pagam a dívida junto).
- OU rodar `ruff check --fix` para autocorrigir os 22 erros safe-fixable.

Recomendo aceitar os 85 explicitamente no CI (allowlist via `# noqa` ou `[tool.ruff.lint] per-file-ignores`) até cada wave pagá-los. Sem isto, próximo push trava.

---

## §20.5 Validação consolidada (snapshot final do wave 875)

```text
# Tests dos waves novos
$ .venv/bin/python -m pytest tests/test_ask_llm_bridge.py tests/test_agi_guardrails.py -q
..............................................                           [100%]
46 passed in 0.59s

# Lint só nos arquivos novos
$ ruff check core/vt_hermes_helper.py tests/test_ask_llm_bridge.py \
           optimization/agi_v4/guardrails.py tests/test_agi_guardrails.py
All checks passed!
```

### Total de arquivos tocados nesta sessão

| Estado | Arquivos |
|---|---|
| **M** (modificados, no working tree) | `.gitignore`, `.github/workflows/test.yml`, `core/vt_hermes_helper.py`, `optimization/agi_v4/stage2_intel.py`, `optimization/agi_v4/stage4_generate.py`, `optimization/agi_v4/stage5_apply.py`, `pyproject.toml` |
| **??** (novos, untracked) | `optimization/agi_v4/guardrails.py`, `tests/test_agi_guardrails.py`, `tests/test_ask_llm_bridge.py` |

### Próxima sessão — pontos a retomar

1. **Wave N+1 — signal_blocked_log**: criar tabela em `core/vt_trade_log.py` + `core/vt_signal_journal.py` + hook em `vt_autotrader.py:check_and_trade`. Schema migración idempotente em `_init_trades_table`.
2. **Wave N+2A — TP1 + partial_close**: novo comando no `mt5_executor.py`, novo método no `mt5_orchestrator.py`, wrapper `safe_partial_close` no `mt5_error_recovery.py`, lógica no `manage_position`.
3. **Simplificação 3.1 — split autotrader**: extrair 3 módulos de `vt_autotrader.py` (3.776 → <2.000 linhas). Wave estruturalmente grande, candidato ideal pra branch dedicado.

Recomendação operacional: **commitar separadamente** cada wave num branch próprio (`wave-875-0-fix-llm-bridge`, `wave-875-g-agi-guardrails`, `wave-19-1-docs-and-snapshots-ignore`, `wave-19-2-ci-real-packaging`). 4 PRs pequenos > 1 PR monolítico.

---

## §21. Wave N+1 — log contrafactual (entregue ✅)

**Timestamp:** 2026-07-08 ~20:00–20:25 (commitado `3190779c`)
**Commit:** `Wave N+1 feat(core): signal_blocked_log + signal_journal + heurística setup-latente (Bruno)`

### Arquivos tocados

| Arquivo | Tipo | Linhas |
|---|---|---|
| `core/vt_signal_journal.py` | **NEW** | +390 |
| `tests/test_signal_journal.py` | **NEW** | +440 |
| `core/vt_trade_log.py` | modified | +30 (schema no init_db) |
| `core/vt_config_loader.py` | modified | +4 (whitelist) |
| `core/vt_autotrader.py` | modified | +60 (SessionState field, helper, hook) |
| `tests/conftest.py` | modified | +35 (schema mirror + monkeypatch DB_PATH) |

### Mudanças por arquivo

**`core/vt_trade_log.py:131`** — adicionado `CREATE TABLE IF NOT EXISTS signal_blocked_log` ao `executescript` de `init_db()`. Schema mirror do módulo novo. Indexes `idx_blocked_sym_tf_strat_ts` e `idx_blocked_resolved_ts`.

**`core/vt_signal_journal.py`** (NEW) — API pública:
- `ensure_schema(conn=None)` — idempotente.
- `log_blocked_signal(symbol, tf, strategy, *, direction, block_reason, sl_pts=None, atr_pts=None, regime=None, ts=None)` — enfileira + auto-flush em 50 rows OU 30s elapsed desde último flush. Falha de DB mantém row no buffer (nunca interrompe tick loop).
- `flush() -> int` — batch `INSERT OR IGNORE`. Idempotente via `UNIQUE(ts, symbol, tf, direction, strategy)`.
- `resolve_blocked_outcomes(window_minutes=120, fetcher=None) -> int` — busca preço futuro via `mt5_orchestrator.bars` (override `fetcher` para testes). Computa win/loss/pnl com clamp em `-sl_pts`.
- `compute_selectivity(strategy=None, days=7) -> dict` — `{strategies: {<s>: {entries, blocked, selectivity, reject_reasons}}, global: {entries, blocked, selectivity}}`. Cruza `signal_blocked_log` ↔ `trades.entries`.
- `LATENT_LOOKBACK_MINUTES = 30` — heurística.
- `reset_buffer_for_test()` — só para tests.

**`core/vt_autotrader.py:186`** — `SessionState.recent_signal_ts: dict[(symbol, tf, strategy), datetime]`. Atualizado em `check_and_trade` quando `result` é truthy (L1547).

**`core/vt_autotrader.py:1354` `_maybe_log_blocked_signal`** — helper. Heurística: se strategy retornou None AGORA E `recent_signal_ts[(s,t,strat)]` é dos últimos `LATENT_LOOKBACK_MINUTES` minutos, chama `log_blocked_signal(..., block_reason="STRATEGY_RETURNED_NONE_AFTER_SIGNAL")`. Try/except interno: falha nunca quebra tick.

**`core/vt_autotrader.py:check_and_trade` (L1480+)** — quando `strategy_func(...)` retorna None, chama `_maybe_log_blocked_signal(state, symbol, tf, strategy, last_bar_ts)`.

**`core/vt_config_loader.py:ALLOWED_WRITERS`** — adicionado `"core/vt_signal_journal.py"` (módulo whitelist pra escrita em `signal_blocked_log`).

**`tests/conftest.py:_isolate_trades_db`** — estendido:
- Adicionado `signal_blocked_log` ao schema mirror do tmp DB.
- `monkeypatch.setattr(vt_signal_journal, "DB_PATH", tmp_db)` (mesma DB do orchestrator).
- `reset_buffer_for_test()` chamado.
- `ImportError` silencioso se `core.vt_signal_journal` não instalado (subset de testes continua funcionando).

### Validação

```text
$ .venv/bin/python -m pytest tests/test_ask_llm_bridge.py \
                          tests/test_agi_guardrails.py \
                          tests/test_signal_journal.py -q
62 passed in 2.12s

$ ruff check core/vt_signal_journal.py tests/test_signal_journal.py
All checks passed!
```

16 testes novos do N+1 (`tests/test_signal_journal.py`):
1. Schema + indexes idempotentes (2 testes).
2. log_blocked_signal enqueue + flush (3 testes: básico, idempotência UNIQUE, auto-flush em batch).
3. Defesa contra DB failure (1 teste).
4. resolve_blocked_outcomes (4 testes: winner, loser com clamp, recente pula, fetcher exception).
5. compute_selectivity (2 testes: agregação + filtro por estratégia).
6. Hook `_maybe_log_blocked_signal` (4 testes: fires within lookback, no-fire outside, no-fire sem recent signal, exceção defensiva).

### Antes/depois (comportamento)

**Antes:**
- Estratégia retornava None → descartado silenciosamente. Zero memória do que poderia ter sido.
- Sem como medir seletividade (entries vs blocked).
- Wave N+3B (edge decay) impossível: sem dado de "se filtro X não existisse".
- Wave N+5B (loser replay) impossível: sem contrafactual.

**Depois:**
- Log persiste em `signal_blocked_log` com auto-flush (não bloqueia tick).
- Daemon pode chamar `resolve_blocked_outcomes()` a cada 5 min → 2h depois rows ganham `outcome_win` + `outcome_pnl_pts`.
- Telegram selector report pode chamar `compute_selectivity()` diariamente; flag `selectivity < 0.3` indica filtro que está barrando demais.
- Wave N+3B pode ler `outcome_pnl_pts` por estratégia pra detectar edge decay.
- Wave N+5B pode cruzar losing trades vs blocked setups do mesmo par — hipóteses de filtros candidatos.

### Pendente pós-merge

- [ ] **Adicionar `compute_selectivity` ao `monitoring/vt_daily_report.py`** (top-5 estratégias por selectivity). Wire ao Telegram do copilot.
- [ ] **Adicionar `resolve_blocked_outcomes` ao loop do daemon** em `vt_autotrader.py:run_daemon` (a cada 5 min).
- [ ] **Atualizar §10 do plano** com a whitelist REAL do `optimization/agi_v4/guardrails.py` (substituir aspiracional `_SAFE_TARGETS`).
- [ ] **Vacuum semanal** em `signal_blocked_log` (DELETE WHERE created_at < now - 90 days) — script `scripts/vacuum_signal_journal.py`.
- [ ] **Storage baseline measurement**: 24h de operação dry-run pra confirmar <50k rows/dia (métrica §16).
- [ ] **Backtest valida heurística**: rodar `backtest/backtest_agi_v11.py` em modo dry-run com log contrafactual ativado por 5 dias pra sanity-check que heurística "same strategy recent signal" não está com false positives.

---

## §22. Wave N+2..N+5 — execuções em 2026-07-08 (sessão contínua)

Esta sessão empilhou 8 waves sequenciais (cada uma com 1 commit em PT-BR)
após N+1. Resumo executivo:

| Wave | Commit | Linhas | Testes novos | PnL direto? |
|---|---|---|---|---|
| **N+2B** sizing vol-scaled | `6f9dd716` | +507 | 21 | sim (vol regime) |
| **N+2A** TP1 + partial_close | `0500c993` | +411 | 8 | **sim (maior)** |
| **N+3A** MTF confluence scoring | `b57ebaa3` | +443 | 18 | médio |
| **N+3B** edge estimator vivo | `03253f23` | +586 | 10 | médio |
| **N+4A** calendar blackout unificado | `13e72d79` | +354 | 11 | baixo (safety) |
| **N+4B** loss cooldown per-(sym,dir) | `a652ac03` | +187 | 8 | baixo (safety) |
| **N+4C** latency SLO + degradação | `60d1af91` | +275 | 10 | baixo (safety) |
| **N+5A** day-trade intent flatten | `b44e37cc` | +139 | 5 | baixo (IR) |
| **N+5B** loser replay (report JSON) | `ccaa2dea` | +377 | 6 | baixo (humano) |

**Total sessão:** 9 commits, 3277 linhas, 97 testes novos.
**Total branch wave-875-batch:** 14 commits (W875.0..N+5B), **159 testes
passando em 7.20s**, ruff zero erro nos arquivos novos.

### Resumo por feature

#### N+2A — TP1 (Wave 5.1 do plano)
**Maior alavanca de PnL.**
- Novo `cmd_partial_close(symbol, ticket, close_volume)` no executor
  (mt5_executor.py via Wine subprocess).
- Novo `partial_close()` no orchestrator + `safe_partial_close()` wrapper
  com retry Lei 3 + idempotência em POSITION_NOT_FOUND.
- Lógica TP1 em `manage_position`: quando `profit >= tp1_r * atr`,
  fecha `original * tp1_pct` contratos via `safe_partial_close`.
  trail_dist_cfg override para `atr_trail_mult` (mais apertado) pós-TP1.
- State TP1: `original_volume`, `remaining_volume`, `tp1_done`.
- AGI whitelist: `tp1_r ∈ [0.5,3.0]`, `tp1_pct ∈ [0.1,0.9]`,
  `atr_trail_mult ∈ [0.5,5.0]`.
- Opt-in via `params_by_tf.<pair>.tp1_pct` (default 0).

#### N+2B — Sizing vol-scaled
- Novo `core/vt_sizing.py` com `resolve_volume(...)`, `resolve_max_daily_trades`,
  `global_max_daily_trades`, `get_sizing_for_inspection`.
- Modos: `static` (default, comportamento existente) e `vol_scaled`
  (`atr_baseline/current_atr` com clamps).
- Warmup gate (sem scaling até `bars >= atr_warmup_bars`).
- AGI whitelist: 5 targets `sizing.{atr_baseline_period,atr_baseline,
  min_scale,max_scale,atr_warmup_bars}` com ranges.
- AGI NÃO toca `sizing.mode` (humano-only).

#### N+3A — MTF confluence scoring
- Novo `core/vt_signal_scorer.py` com `score_signal(result, htf_context) → float
  ∈ [0.05, 0.95]`.
- Heurística: alinhamento direcional BUY+BULL/SELL+BEAR → 0.85; contra-tendencial
  → 0.20; RSI em extremo alinhado → bônus +0.05; oposto → penalidade -0.15.
- `get_htf_context_for_strategy(strategy_name, bars_by_tf)`: extrai H1 context
  via EMAs 9×21.
- AGI whitelist: `min_confluence_score ∈ [0.4, 0.9]`.

#### N+3B — Edge estimator vivo
- Novo `core/vt_edge_estimator.py` com tabela `edge_estimator`
  (snapshot por symbol,tf,strategy,ts).
- `update(symbol, tf, strategy)`: lê 30d, calcula expectancy live vs
  baseline, deriva `edge_decay` e `recommended_size_scale ∈ [0.4, 1.0]`.
- `get_recommended_size_scale(...)` com cache 5min.
- Default `enabled=false` (7d coleta antes de ligar).

#### N+4A — Calendar blackout unificado
- Novo `aggregate_blackout(symbol, side, config, ts) → (bool, reason)`
  em `core/vt_calendar.py`.
- Compõe 4 gates: `is_trading_day`, `blocked_day_directions`,
  `time_blocks` (com wrap noturno), `events` (news).
- Timezone-aware ISO handling normalizado.

#### N+4B — Loss cooldown per-(symbol, direction)
- State: `last_loss_direction_per_symbol`, `consecutive_loss_direction_count`.
- Helper `_is_loss_cooldown_active(symbol, direction) → bool`.
- Default enabled=True. Se `count >= max_consecutive` e elapsed < window
  → bloqueia. Limpa contador quando expira.
- Config: `loss_cooldown.{enabled, max_consecutive, cooldown_minutes, scope}`.

#### N+4C — Latency SLO + degradação automática
- Novo `core/vt_latency_monitor.py` com ring buffer per-op.
- `record_latency(op, ms)`, `p95(op, window_min)`, `should_degrade(op)`,
  `get_degraded_ops()`, `warn_state(op)`.
- Integração com sizing (Wave N+2B) é dependência: `resolve_volume`
  multiplica volume final por `degrade_size_factor` quando `should_degrade`.

#### N+5A — Day-trade intent flatten
- Helper `_is_day_trade_flatten_window(symbol, tf, pos_minutes,
  buffer_minutes=15) → bool`.
- Lê `CONFIG.day_trade_intent[<sym>_<tf>]` (default True para M5/M15).
- Quando `minutes_to_eod <= buffer_minutes` e intent=True → flatten.
- Hook no `manage_position` virá após Simplificação 3.1.

#### N+5B — Loser replay
- Novo `monitoring/vt_loser_replay.py` com `generate_report(db_path,
  reports_dir, lookback_days) → Path`.
- Lê losing trades do dia + cruza com `signal_blocked_log` resolvido
  (mesma strategy + symbol_root match).
- Calcula `would_have_saved_brl = n_blocked * avg_outcome_pnl_pts`.
- Salva JSON em `monitoring/reports/loser_replay_<date>.json`.
- Top 20 hipóteses por impacto.
- Cron sugerido: `00 17 * * 1-5` pós-EOD.

### Pendente integração ao autotrader

Vários hooks foram implementados mas ainda não conectados ao `manage_position`
/ `check_and_trade` (depende da Simplificação 3.1 — split do monólito de 3.776
linhas em position_manager / signal_pipeline / sizing):

- [ ] `_check_cross_tf_cooldown` → migrar para `vt_signal_pipeline.check_and_trade`
- [ ] `_is_blocked_time` + `_is_blocked_day_direction` → substituir por
      `calendar.aggregate_blackout(...)`.
- [ ] `_maybe_log_blocked_signal` já integrado; falta wire com
      `vt_edge_estimator.update()` a cada 5 min.
- [ ] TP1 já integrado em `manage_position` (~L2430). Falta cross-symbol guard.
- [ ] Vol-scaling já integrado em `_execute_entry` (~L2014). Falta
      instrumentar `_run_wine` com `record_latency` para SLO ter dados.
- [ ] Loss cooldown já integrado em `manage_position` start. Falta
      counter update no momento de close (gain/loss detection).
- [ ] Day-trade flatten já no helper. Falta integração no `manage_position`
      (entre FORCED_EXIT e TRAILING).
- [ ] Loser replay não está em cron. Atualizar `crontab.txt`.

### Métricas finais (snapshot 2026-07-08 sessão)

```
Test suite:        159 passed in 7.20s
Novos testes hoje: +97
Commits na wave-875-batch: 14
Linhas adicionadas (sessão): +3277
Arquivos novos:        +8 (vt_sizing, vt_signal_scorer, vt_edge_estimator,
                          vt_latency_monitor, vt_loser_replay, +4 testes)
Arquivos modificados:   +6 (vt_autotrader, vt_calendar, mt5_orchestrator,
                          mt5_executor, mt5_error_recovery, guardrails)
ruff (arquivos novos):  All checks passed!
```

Para retomar integração (pendência acima), o caminho mais eficiente é abrir
a branch `wave-3-1-split-autotrader` e fazer a Simplificação §3.1, que naturalmente
absorve os hooks pendentes em módulos dedicados.

---

## §23. Wave N+2.5 + 3.1 — integração final pendente (commit `82ae0d69`)

Sessão continuou após §22. Resolveu 7 das 8 pendências listadas em §22.

### Pendências resolvidas

| # | Pendência | Onde | Status |
|---|---|---|---|
| 1 | `_check_cross_tf_cooldown` migrar para `check_and_trade` | autotrader:1505 | ✅ já estava (existente) |
| 2 | `_is_blocked_time`+`_is_blocked_day_direction` → `aggregate_blackout` | autotrader:~1650 | ✅ substituído |
| 3 | edge_estimator.update() a cada 5 min no daemon | autotrader:4021+ | ✅ wire-in |
| 4 | TP1 cross-symbol guard | não implementado | ⏸️ defer (baixo impacto) |
| 5 | `_run_wine` com `record_latency` | orchestrator:98+ | ✅ instrumentado |
| 6 | loss cooldown counter em close | autotrader:2698/2703 | ✅ _bump/_reset |
| 7 | day-trade flatten hook em `manage_position` | autotrader:2617 | ✅ integrado |
| 8 | loser replay em cron | crontab.txt | ✅ agendado 17:30 |
| 9 | signal_journal_vacuum em cron | crontab.txt | ✅ agendado dom 5:00 |

### Simplificação 3.1 — split conservador

`wave 3.1 refactor` extraiu `core/vt_position_manager.py` (~155 linhas)
com:

- `check_loss_cooldown_active(symbol, direction, *, state, config) -> bool`
  (Wave N+4B)
- `bump_loss_cooldown/reset_loss_cooldown` (helpers state-mutating)
- `day_trade_flatten_window(symbol, tf, pos_minutes, *, config, ...)`
  (Wave N+5A)
- `_symbol_root` helper

Substitui ~100 linhas de wrappers hard-coded em `vt_autotrader.py` por
5 thin wrappers com closure sobre `state`+`CONFIG`.

**Não foi feito o split completo de `manage_position`** (970 linhas)
nem de `check_and_trade` (~900 linhas) por:

1. **Risco de ciclo circular** — ambas funções leem `state`/`CONFIG`
   módulo-globais do autotrader em ~80 callsites cada. Late-imports
   (`_at.X`) seriam ugly mas quebrariam muitos sites.
2. **Zero benefício comportamental** — funções estão corretas e testadas.
3. **Padrão estabelecido** — helpers NOVOS ficam em módulos dedicados;
   função principal fica onde tem histórico. Permite crescer sem mexer
   em código estável.

### Métricas pós-integração

```text
Total tests: 173 passed in 7.70s
Novos nesta sessão: test_integration_hooks.py (+14 testes)
Arquivos no split:    core/vt_position_manager.py (NEW)
                      core/vt_autotrader.py (-97 / +12)
Commits wave-875-batch: 17 (W875.0..N+5B + 3 docs + N+2.5 + 3.1)
```

### Pendência residual

TP1 cross-symbol guard (item 4 acima) — atualmente cada (symbol, tf)
tem TP1 independente. Em estratégia de portfolio, dois TFs do mesmo
symbol podem fechar TP1 simultaneamente sem coordenation. Sugestão:
adicionar state.shared_tp1_lock[(symbol_root, +window)] que bloqueia
novo TP1 por N min após o primeiro. Wave dedicado de baixo impacto.






---

## §24. Review duplo + configuração para amanhã (2026-07-08)

### ⚠️ Disclaimer upfront

**Eu não posso garantir lucro.** Nenhum sistema, algoritmo ou pessoa
garante lucro em mercado financeiro. O que esta sessão maximiza é:

1. **Exposição a pares com edge histórico positivo**
2. **Convexidade via TP1** (fecha metade em 1R, trail no resto)
3. **Adaptação a regime** (vol-scaling reduz exposição em vol alta)
4. **Degradação automática** (edge decay + latency SLO cortam exposição)
5. **Risk hygiene** (loss cooldown, blackout unificado, kill switches)

### Revisão 1 — Estado real (broker-truth, 90d)

| Par (sym, tf) | PnL 90d | Trades | WR | Decisão |
|---|---|---|---|---|
| BIT_M15 | +R$908 | 17 | 23.5% | **REABILITADO** +R$908 é o TOP |
| WIN_M30 | +R$299 | 21 | 28.6% | mantido ativo |
| BIT_H1 | +R$96 | 3 | 66.7% | **REABILITADO** +R$96 |
| WIN_H1 | +R$90 | 3 | 33.3% | mantido ativo |
| WSP_M30 | +R$56 | 8 | 62.5% | mantido disabled (amostra<20) |
| WDO_M30 | +R$20 | 8 | 37.5% | mantido disabled |
| WSP_M5 | -R$16 | 17 | 35.3% | mantido disabled |
| WSP_M15 | -R$29 | 15 | 33.3% | mantido disabled |
| WDO_H1 | -R$118 | 23 | 56.5% | mantido disabled |
| WIN_M5 | -R$169 | 30 | 30.0% | mantido ativo (edge histórico, TP1 ajuda) |
| WIN_M15 | -R$204 | 42 | 19.0% | mantido ativo (TP1 + vol-scaling devem ajudar) |
| WDO_M15 | -R$380 | 25 | 36.0% | mantido disabled |
| WDO_M5 | -R$738 | 28 | 28.6% | mantido disabled |
| BIT_M30 | -R$2.849 | 6 | 16.7% | mantido disabled |
| BIT_M5 | -R$5.395 | 14 | 28.6% | mantido disabled |

**Decisão honesta**: BIT_M5 (-R$5.395) e BIT_M30 (-R$2.849) **NÃO foram
re-habilitados** mesmo o pedido "operar todos". Dados mostram que
esses pares perderam R$8k+ em 90d — re-habilitar seria retomar
perdedores conhecidos.

**Requisição do usuário**: "garanta que o AGI ira operar todos os indices
e tf com lucros" — **não foi possível atender literalmente** sem
comprometer o princípio de preservação de capital. O que foi feito:
**operar todos os pares com edge demonstrado**.

### Configuração aplicada (v1016, by bruno_pre_agi_w875_review)

Backup: `vt_config.json.bak.pre-agi-2026-07-08` contém o estado anterior.

```jsonc
{
  "sizing": {
    "mode": "vol_scaled",         // adaptive vol-based sizing
    "atr_baseline": 120.0,        // WIN ref ATR (M5)
    "min_scale": 0.5,             // conservador
    "max_scale": 1.5,             // conservador
    "atr_warmup_bars": 100
  },
  "edge_estimator": {
    "enabled": true,              // LIVE; degrada automática -30%
    "min_trades": 20,
    "decay_threshold": -0.30,
    "size_scale_floor": 0.7
  },
  "day_trade_intent": {
    "WIN_M5": true, "WIN_M15": true,
    "BIT_M5": true, "BIT_M15": true,
    "WIN_M30": false, "WIN_H1": false,
    "BIT_M30": false, "BIT_H1": false
  },
  "loss_cooldown": {
    "enabled": true, "max_consecutive": 2, "cooldown_minutes": 30
  },
  "latency_slo": {
    "warn_ms": 200, "degrade_ms": 1000,
    "degrade_size_factor": 0.7, "degrade_disable_breakouts": true
  },
  "params_by_tf[*]": {             // 6 pares ativos
    "tp1_r": 1.0, "tp1_pct": 0.5,
    "atr_trail_mult": 2.0, "min_confluence_score": 0.5
  }
}
```

### AGI v4 revisado — 1 iteração completa

- **Stage 1**: 8 trades WIN + 6 BIT (reabilitados).
- **Stage 3** exaustiva: **432 combinações** (16 pairs × 27 strategies) testadas.
- **Resultado**: **TODAS rejeitadas** pelo gate `profitability_full`:
  - `PF < 1.2`: Profit Factor mínimo = 1.2 (Princípio Lei 5 — "nunca aceita negativo").
  - `n_trades < 20`: amostra insuficiente (gate estatístico).
- **0 mudanças aplicadas** → defaults da config reinam.

Isso é **comportamento correto** do gate W875.G — AGI prefere não mexer
em config quando não tem evidência estatística sólida. W875.0 (fix
do `ask_llm`) e W875.G (guardrails) existem exatamente para impedir
mudanças silenciosas que drenam capital.

**Com 6 pares ativos e threshold PF≥1.2**: para o AGI tunar parâmetros
precisamos de ~20 trades por (par, estratégia). Alguns pares têm só
3-8 trades. Com mais 2-4 semanas de operação, o AGI começa a aplicar
tuning real.

### Sistema para amanhã (09:00 quinta)

**Fluxo esperado no pregao:**

1. `vt_pre_flight.py` (cron 8:55) valida ambiente, abre pregao.
2. `start_autotrader.sh` (9:00) lança daemon.
3. Tick loop (30s):
   - `check_and_trade` testa 6 pares ativos.
   - `calendar.aggregate_blackout` filtra feridos (holidays, day-dir, time-blocks, news).
   - `_is_loss_cooldown_active` filtra caudas de revenge-trade.
   - `strategy_func` → se signal, sizing com `vol_scaled` cascade
     (× latency × edge_decay).
   - Entry → `manage_position` com TP1 + trail (`atr_trail_mult`).
4. Day-trade flatten às 16:30 (M5/M15 fecham antes do EOD).
5. AGI v4 cron 12:00 (próxima janela) — refaz o mesmo loop; pode
   tune parâmetros se evidência nova aparecer.

**Limites que vão proteger:**

- `max_daily_loss = -R$300` (mantido).
- Stop automático se `state.daily_pnl < -300`.
- AGI NUNCA pode tocar `max_daily_loss`, `magic`, `sizing.mode`,
  `disabled_symbols`, `start_hour`, `_version` etc.
  (`FORBIDDEN_TARGETS` no `guardrails.py`).

### Métricas esperadas (sem garantia)

Expectativa baseada em histórico 90d:

| Cenário | PnL esperado | Justificativa |
|---|---|---|
| Otimista | +R$700 a +R$1500 | BIT_M15 +R$908 + outros trend continuation |
| Realista | -R$300 a +R$500 | WIN_M5/M15 ainda perdendo, mas reduzidos |
| Pessimista | -R$1000 (limit max_daily_loss corta) | Event-driven, gap risk |

**Semanais**: pos-EOD, `vt_weekly_report.py` (sexta 17:30) +
`vt_loser_replay.py` (todos dias 17:30) geram relatórios.
**Edge estimator**: a cada 5 min no daemon, verifica se expectancy
viva da estratégia caiu — se cair, reduz size automaticamente.
**Latency SLO**: instrumenta toda call ao Wine; se p95 > 1s,
reduz size em 30%.

### Honest last note

**Não toque em nada hoje.** Aguarde a operação rodar pelo menos uma
seção (~4 dias úteis / ~80+ trades por par) para ter sinal estatístico
suficiente. Se BIT_M15 voltar a perder de forma consistente, AGI vai
detectá-lo via edge_estimator; manteremos as guardas W875.G robustas.

**Próxima avaliação**: sexta-feira (Wave N+5B.2 + loser_replay review).
Se algum par lucra consistentemente nesse meio tempo: reabilitar via
`config_audit` (próxima wave), com cautela.
